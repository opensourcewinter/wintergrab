"""``wintergrab benchmark``: how fast WINTERGRAB runs on this machine, measured on this machine.

Nothing leaves the machine. A synthetic shop is served from 127.0.0.1 by a server in a process of its
own: listing pages of 20 product cards linking to product pages, which have a price, a stock line,
specifications and, on every other one, JSON-LD. Each scenario runs in a fresh Python process, so its
CPU time and memory are its own:

=========  ======================================================================================
startup    seconds to ``import wintergrab``, and to run ``wintergrab --version``
crawl      a Spider crawling the shop: pages/s, items/s, request latency, CPU, peak memory
parse      parsing a listing page and reading its 20 cards with CSS: pages/s
extract    the ``product`` template's extraction of product pages: pages/s
data       normalizing and validating product records with a schema: records/s
dedupe     canonical URLs (URLs/s) and near-duplicate fingerprints of pages (pages/s)
browser    (``--browser``) product pages rendered in Chromium, one after another, against the
           same pages over HTTP: pages/s, and how many times slower
=========  ======================================================================================

::

    report = run_benchmark(pages=100, items=1000)
    report["results"]["crawl"]["pages_per_s"]

Numbers depend on the machine, and on what else it is doing: compare runs of one machine, and see
``benchmarks/`` in the repository for the comparison with other crawlers and its method.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

__all__ = ["SCENARIOS", "run_benchmark", "serve_shop"]

#: Every scenario, in the order they run; ``browser`` only when asked for.
SCENARIOS = ("startup", "crawl", "parse", "extract", "data", "dedupe", "browser")
CARDS = 20
_WORDS = str.split(
    "arctic frosted polar winter snowy glacial alpine crisp nordic icy boreal misty silver cozy woolen "
    "fleece thermal rugged classic compact parka mitten beanie scarf boots sled thermos blanket jacket "
    "gloves lantern snowshoe goggles skis kettle stove tent sweater socks backpack headlamp"
)


# ---------------------------------------------------------------------------------------------- #
# the shop
# ---------------------------------------------------------------------------------------------- #
def _name(j: int) -> str:
    rnd = random.Random(j * 7919 + 1)
    return f"{rnd.choice(_WORDS).title()} {rnd.choice(_WORDS).title()} {j}"


def _price(j: int) -> str:
    rnd = random.Random(j * 104729 + 3)
    return f"{rnd.randint(5, 900)}.{rnd.randint(0, 99):02d}"


def _text(rnd: random.Random, words: int) -> str:
    return " ".join(rnd.choice(_WORDS) for _ in range(words)).capitalize() + "."


def _chrome(title: str, pages: int, body: str, head: str = "") -> bytes:
    menu = "".join(f'<li><a href="/page/{(k * 37) % pages}">Category {k}</a></li>' for k in range(24))
    footer = "".join(f'<li><a href="/help/{k}">Help {k}</a></li>' for k in range(12))
    return (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>{title}</title>{head}</head><body>'
        f'<header><a href="/page/0">Winter Shop</a><nav><ul>{menu}</ul></nav></header>{body}'
        f"<footer><ul>{footer}</ul><p>&copy; Winter Shop</p></footer></body></html>"
    ).encode()


def listing_page(i: int, pages: int, items: int) -> bytes:
    """Listing page ``i``: 20 product cards, and links to the next pages (and a few further on)."""
    rnd = random.Random(i)
    cards = "".join(
        f'<div class="card"><h3><a class="item" href="/item/{j}">{_name(j)}</a></h3>'
        f'<p class="price">${_price(j)}</p><p>{_text(rnd, 20)}</p></div>'
        for j in ((i * CARDS + k) % items for k in range(CARDS))
    )
    ahead = sorted({p for p in (*range(i + 1, i + 6), *range(5 * i + 1, 5 * i + 6)) if p < pages})
    pager = "".join(f'<a class="next" href="/page/{p}">{p}</a>' for p in ahead)
    return _chrome(
        f"Winter gear, page {i}", pages, f'<main><h1>Winter gear</h1>{cards}<nav class="pager">{pager}</nav></main>'
    )


def product_page(j: int, pages: int) -> bytes:
    """Product page ``j``: its name, price, stock, specifications; JSON-LD on every other one."""
    rnd = random.Random(1_000_003 + j)
    specs = "".join(f"<tr><th>Spec {k}</th><td>{rnd.choice(_WORDS)} {rnd.randint(1, 999)}</td></tr>" for k in range(10))
    head = ""
    if j % 2 == 0:
        data = {"@context": "https://schema.org", "@type": "Product", "name": _name(j), "sku": f"SKU-{j}",
                "offers": {"@type": "Offer", "price": _price(j), "priceCurrency": "USD",
                           "availability": "https://schema.org/InStock"}}  # fmt: skip
        head = f'<script type="application/ld+json">{json.dumps(data)}</script>'
    body = (
        f'<main><ol class="breadcrumb"><li><a href="/page/0">Home</a></li><li>{_name(j)}</li></ol>'
        f'<h1>{_name(j)}</h1><div class="buy"><span class="price">${_price(j)}</span>'
        f'<span class="stock">In stock</span><button>Add to cart</button></div>'
        f'<div class="description"><p>{_text(rnd, 60)}</p></div><table class="specs">{specs}</table></main>'
    )
    return _chrome(f"{_name(j)} | Winter Shop", pages, body, head)


def _http(status: str, body: bytes, kind: str = "text/html; charset=utf-8") -> bytes:
    return (
        f"HTTP/1.1 {status}\r\nContent-Type: {kind}\r\nContent-Length: {len(body)}\r\nConnection: keep-alive\r\n\r\n"
    ).encode() + body


def build_shop(pages: int, items: int) -> dict[bytes, bytes]:
    """Every response of the shop, rendered: its path -> the whole HTTP response."""
    site = {f"/page/{i}".encode(): _http("200 OK", listing_page(i, pages, items)) for i in range(pages)}
    site.update({f"/item/{j}".encode(): _http("200 OK", product_page(j, pages)) for j in range(items)})
    site[b"/robots.txt"] = _http("200 OK", b"User-agent: *\nAllow: /\n", "text/plain")
    return site


class _Shop(asyncio.Protocol):
    """HTTP/1.1 with keep-alive over rendered responses: a dictionary look-up per request."""

    def __init__(self, site: dict[bytes, bytes], latency: float) -> None:
        self.site, self.latency, self.buffer = site, latency, b""
        self.transport: Any = None

    def connection_made(self, transport: Any) -> None:
        self.transport = transport

    def data_received(self, data: bytes) -> None:
        self.buffer += data
        while b"\r\n\r\n" in self.buffer:
            head, _, self.buffer = self.buffer.partition(b"\r\n\r\n")
            parts = head.split(b"\r\n", 1)[0].split(b" ")
            path = parts[1].split(b"?", 1)[0] if len(parts) > 1 else b"/"
            answer = self.site.get(path) or _http("404 Not Found", b"<html><body>Not found</body></html>")
            if self.latency:
                asyncio.get_running_loop().call_later(self.latency, self._send, answer)
            else:
                self.transport.write(answer)

    def _send(self, answer: bytes) -> None:
        if not self.transport.is_closing():
            self.transport.write(answer)


def serve_shop(pages: int, items: int, *, latency: float = 0.0, port: int = 0) -> None:
    """Serve the shop from 127.0.0.1 until stopped, printing its address first (``latency``: seconds per
    response)."""
    site = build_shop(pages, items)

    async def main() -> None:
        loop = asyncio.get_running_loop()
        server = await loop.create_server(lambda: _Shop(site, latency), "127.0.0.1", port, backlog=1024)
        address = server.sockets[0].getsockname()
        print(f"http://127.0.0.1:{address[1]}", flush=True)
        async with server:
            await server.serve_forever()

    asyncio.run(main())


# ---------------------------------------------------------------------------------------------- #
# the scenarios (each in a process of its own)
# ---------------------------------------------------------------------------------------------- #
def _cpu_seconds() -> float:
    return time.process_time()  # user and system CPU of this process, on every platform


def _peak_rss_mb() -> float | None:
    """The process's peak memory, where the platform says (not on Windows)."""
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / 1024 / (1024 if sys.platform == "darwin" else 1), 1)  # macOS says bytes, Linux KB


def _rate(fn: Callable[[], int], repeat: int = 3) -> tuple[float, float]:
    """``(units per second, CPU seconds)``: the median of ``repeat`` runs of ``fn`` (which returns its units)."""
    rates, cpu0 = [], _cpu_seconds()
    for _ in range(repeat):
        start = time.perf_counter()
        units = fn()
        rates.append(units / max(time.perf_counter() - start, 1e-9))
    return round(statistics.median(rates), 1), round(_cpu_seconds() - cpu0, 3)


def _scenario_crawl(url: str, concurrency: int, **_: Any) -> dict[str, Any]:
    from urllib.parse import urlsplit

    from . import Spider

    class Shop(Spider):
        name = "benchmark"
        autothrottle = False  # the most it can do; a real site gets AutoThrottle
        download_delay = 0.0
        keep_items = False
        log_level = None
        cache = None

        def __init__(self, **options: Any) -> None:
            super().__init__(**options)
            self.item_count = self.bad_items = 0

        def parse(self, response: Any) -> Any:
            for href in response.css("a.item::attr(href)").getall():
                yield response.follow(href, callback=self.parse_item)
            for href in response.css("nav.pager a::attr(href)").getall():
                yield response.follow(href, callback=self.parse)

        def parse_item(self, response: Any) -> Any:
            yield {"name": response.css("h1::text").get(), "price": response.css(".price::text").get()}

        def process_item(self, item: Any) -> Any:
            self.item_count += 1
            self.bad_items += not (item["name"] and str(item["price"] or "").startswith("$"))
            return item

    start_url = url.rstrip("/") + "/page/0"
    spider = Shop(start_urls=[start_url], allowed_domains=[urlsplit(start_url).hostname or ""],
                  concurrency=concurrency, concurrency_per_domain=concurrency)  # fmt: skip
    cpu0, wall0 = _cpu_seconds(), time.perf_counter()
    result = spider.run()
    wall = time.perf_counter() - wall0
    metrics = result.metrics
    pages = int(result.stats.get("pages", 0))
    latency = metrics.get("latency") or {}
    return {
        "pages": pages,
        "items": spider.item_count,
        "bad_items": spider.bad_items,
        "errors": int(metrics.get("errors", 0)) + int(metrics.get("failed", 0)),
        "concurrency": concurrency,
        "wall_s": round(wall, 3),
        "pages_per_s": round(pages / wall, 1) if wall else 0.0,
        "items_per_s": round(spider.item_count / wall, 1) if wall else 0.0,
        "latency_p50_ms": round(1000 * float(latency.get("p50", 0)), 1),
        "latency_p90_ms": round(1000 * float(latency.get("p90", 0)), 1),
        "megabytes": round(int(result.stats.get("bytes", 0)) / 1e6, 1),
        "cpu_s": round(_cpu_seconds() - cpu0, 3),
        "peak_rss_mb": _peak_rss_mb(),
    }


def _scenario_parse(rounds: int, **_: Any) -> dict[str, Any]:
    from .fetchers.response import Response

    bodies = [listing_page(i, 100, 1000) for i in range(10)]

    def run() -> int:
        for n in range(rounds):
            page = Response(
                f"https://shop.example/page/{n}", body=bodies[n % 10], headers={"content-type": "text/html"}
            )
            for card in page.css("div.card"):
                card.css("a.item::attr(href)").get(), card.css(".price::text").get()
        return rounds

    rate, cpu = _rate(run)
    return {"pages_per_s": rate, "page_kb": round(sum(map(len, bodies)) / len(bodies) / 1000, 1), "rounds": rounds,
            "cpu_s": cpu, "peak_rss_mb": _peak_rss_mb()}  # fmt: skip


def _scenario_extract(rounds: int, **_: Any) -> dict[str, Any]:
    from .extraction import Extractor
    from .extraction.templates import template
    from .fetchers.response import Response

    extractor = Extractor(template("product"))
    bodies = [product_page(j, 100) for j in range(10)]

    def run() -> int:
        for n in range(rounds):
            extractor.extract(Response(f"https://shop.example/item/{n}", body=bodies[n % 10],
                                       headers={"content-type": "text/html"}))  # fmt: skip
        return rounds

    fields = len(template("product").fields)
    rate, cpu = _rate(run)
    return {"pages_per_s": rate, "fields": fields, "rounds": rounds, "cpu_s": cpu, "peak_rss_mb": _peak_rss_mb()}


def _scenario_data(rounds: int, **_: Any) -> dict[str, Any]:
    from .data import Normalize, Pipeline, Schema, Validate

    schema = Schema.from_dict({"name": "product", "fields": {
        "name": {"type": "string", "required": True}, "price": {"type": "money", "required": True},
        "rating": {"type": "rating", "best": 10}, "availability": "availability", "weight": "quantity",
        "released": "date", "url": "url"}})  # fmt: skip
    rnd = random.Random(7)
    count = max(rounds * 10, 100)
    records = [
        {"name": _name(i), "price": rnd.choice(["${:,}.99", "{:,},00 €", "£{:,}"]).format(rnd.randint(5, 99_999)),
         "rating": f"{rnd.randint(1, 9)}.{rnd.randint(0, 9)} out of 10",
         "availability": rnd.choice(["In stock", "Only 3 left", "Out of stock"]), "weight": f"{rnd.randint(50, 2000)} g",
         "released": f"2026-0{rnd.randint(1, 9)}-1{rnd.randint(0, 9)}", "url": f"https://shop.example/p/{i}?utm_source=x"}
        for i in range(count)
    ]  # fmt: skip

    def run() -> int:
        Pipeline([Normalize(schema), Validate(schema, on_error="keep")]).run([dict(r) for r in records])
        return count

    rate, cpu = _rate(run)
    return {"records_per_s": rate, "records": count, "fields": len(schema.fields), "cpu_s": cpu,
            "peak_rss_mb": _peak_rss_mb()}  # fmt: skip


def _scenario_dedupe(rounds: int, **_: Any) -> dict[str, Any]:
    from .data.similarity import shingles, simhash
    from .fetchers.response import Response
    from .urls import URLNormalizer

    pages = max(rounds * 10, 200)
    # each page as a crawl meets it: host case, default port, parameter order, tracking parameters, fragments
    urls = [
        variant
        for i in range(pages)
        for variant in (
            f"https://shop.example/p/{i}?a=1&b=2",
            f"https://Shop.Example:443/p/{i}?b=2&a=1",
            f"https://shop.example/p/x/../{i}?a=1&b=2&utm_source=mail",
            f"https://shop.example/p/{i}?a=1&b=2#reviews",
            f"HTTPS://shop.example/p/{i}?a=1&b=2&utm_campaign=spring",
        )
    ]
    texts = [Response("https://shop.example/", body=product_page(j, 100), headers={"content-type": "text/html"})
             .selector.css("main").get() or "" for j in range(10)]  # fmt: skip
    distinct: set[str] = set()

    def canonical() -> int:
        normalizer = URLNormalizer()  # a fresh one, its cache empty: every URL is read once per run
        distinct.clear()
        distinct.update(normalizer(u) for u in urls)
        return len(urls)

    def fingerprints() -> int:
        for n in range(rounds):
            simhash(shingles(texts[n % 10]))
        return rounds

    url_rate, cpu1 = _rate(canonical)
    page_rate, cpu2 = _rate(fingerprints)
    return {"urls_per_s": url_rate, "urls": len(urls), "unique_urls": len(distinct), "simhash_pages_per_s": page_rate,
            "cpu_s": round(cpu1 + cpu2, 3), "peak_rss_mb": _peak_rss_mb()}  # fmt: skip


def _scenario_browser(url: str, rounds: int, **_: Any) -> dict[str, Any]:
    from .fetchers.browser import BrowserFetcher
    from .fetchers.http import Fetcher

    count = max(5, min(rounds, 30))
    targets = [f"{url.rstrip('/')}/item/{j}" for j in range(count)]
    with Fetcher() as http:
        http.get(targets[0])  # a warm connection, as in a crawl
        start = time.perf_counter()
        for target in targets:
            http.get(target)
        http_s = (time.perf_counter() - start) / count
    cpu0 = _cpu_seconds()
    with BrowserFetcher() as browser:
        browser.get(targets[0])  # the browser started, as in a crawl
        start = time.perf_counter()
        for target in targets:
            browser.get(target)
        browser_s = (time.perf_counter() - start) / count
    return {"pages_per_s": round(1 / browser_s, 1), "http_pages_per_s": round(1 / http_s, 1),
            "times_slower": round(browser_s / http_s, 1), "pages": count, "cpu_s": round(_cpu_seconds() - cpu0, 3),
            "peak_rss_mb": _peak_rss_mb()}  # fmt: skip


_RUNNERS: dict[str, Callable[..., dict[str, Any]]] = {
    "crawl": _scenario_crawl,
    "parse": _scenario_parse,
    "extract": _scenario_extract,
    "data": _scenario_data,
    "dedupe": _scenario_dedupe,
    "browser": _scenario_browser,
}


# ---------------------------------------------------------------------------------------------- #
# running them
# ---------------------------------------------------------------------------------------------- #
def _startup(runs: int = 5) -> dict[str, Any]:
    """Median seconds for Python alone, ``import wintergrab``, and ``wintergrab --version``, each run ``runs`` times."""

    def median_seconds(command: list[str]) -> float:
        times = []
        for _ in range(runs):
            start = time.perf_counter()
            subprocess.run(command, check=True, capture_output=True, timeout=120)
            times.append(time.perf_counter() - start)
        return statistics.median(times)

    bare = median_seconds([sys.executable, "-c", "pass"])
    imported = median_seconds([sys.executable, "-c", "import wintergrab"])
    cli = median_seconds([sys.executable, "-m", "wintergrab", "--version"])
    return {"python_ms": round(bare * 1000), "import_ms": round((imported - bare) * 1000),
            "cli_version_ms": round(cli * 1000)}  # fmt: skip


def run_benchmark(
    *,
    scenarios: Sequence[str] = SCENARIOS[:-1],
    pages: int = 100,
    items: int = 1000,
    latency: float = 0.0,
    concurrency: int = 32,
    rounds: int = 200,
    startup_runs: int = 5,
    on_result: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run the scenarios (see the module docs) and return ``{"environment": {...}, "results": {name: {...}}}``.

    ``pages`` and ``items`` size the shop the crawl reads (``pages + items`` pages); ``latency`` (seconds)
    delays each of its responses, as a network would; ``rounds`` is how many pages (or tens of records)
    the other scenarios read. ``on_result`` is called with each scenario's result as it finishes.
    """
    from . import __version__

    unknown = [s for s in scenarios if s not in SCENARIOS]
    if unknown:
        raise ValueError(f"unknown scenario(s) {', '.join(unknown)}; known: {', '.join(SCENARIOS)}")
    report: dict[str, Any] = {
        "environment": {
            "wintergrab": __version__,
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": f"{platform.system()} {platform.machine()}",
            "cpus": os.cpu_count(),
            "shop": {"pages": pages, "items": items, "latency_ms": round(latency * 1000, 1)},
        },
        "results": {},
    }
    server = None
    try:
        for name in scenarios:
            if name == "startup":
                result = _startup(startup_runs)
            else:
                if name in ("crawl", "browser") and server is None:
                    server = subprocess.Popen(
                        [sys.executable, "-m", "wintergrab.bench", "serve", "--pages", str(pages), "--items",
                         str(items), "--latency", str(latency)],
                        stdout=subprocess.PIPE, text=True,
                    )  # fmt: skip
                    assert server.stdout is not None
                    url = server.stdout.readline().strip()
                    if not url.startswith("http"):
                        raise RuntimeError("the benchmark's shop did not start")
                    report["environment"]["url"] = url
                result = _in_process_of_its_own(name, report["environment"].get("url", ""), concurrency, rounds)
            report["results"][name] = result
            if on_result is not None:
                on_result(name, result)
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=10)
    return report


def _in_process_of_its_own(name: str, url: str, concurrency: int, rounds: int) -> dict[str, Any]:
    command = [sys.executable, "-m", "wintergrab.bench", "scenario", name, "--url", url,
               "--concurrency", str(concurrency), "--rounds", str(rounds)]  # fmt: skip
    done = subprocess.run(command, capture_output=True, text=True, timeout=3600)
    lines = [line for line in done.stdout.splitlines() if line.startswith("RESULT ")]
    if done.returncode != 0 or not lines:
        detail = (done.stderr.strip().splitlines() or ["no result"])[-1]
        return {"error": detail}
    result: dict[str, Any] = json.loads(lines[-1][len("RESULT ") :])
    return result


def describe(report: dict[str, Any]) -> str:
    """The report as a table."""
    env = report["environment"]
    shop = env["shop"]
    lines = [
        f"wintergrab {env['wintergrab']} on {env['system']}, {env['implementation']} {env['python']}, "
        f"{env['cpus']} CPUs; shop: {shop['pages']} listing + {shop['items']} product pages"
        + (f", {shop['latency_ms']} ms latency" if shop["latency_ms"] else ""),
        "",
    ]
    rows: list[tuple[str, str, str]] = []
    for name, r in report["results"].items():
        if "error" in r:
            rows.append((name, "failed", r["error"]))
            continue
        if name == "startup":
            rows += [(name, "import wintergrab", f"{r['import_ms']} ms (Python alone: {r['python_ms']} ms)"),
                     (name, "wintergrab --version", f"{r['cli_version_ms']} ms")]  # fmt: skip
        elif name == "crawl":
            rows += [
                (name, "pages/s", f"{r['pages_per_s']:,} ({r['pages']:,} pages, {r['items']:,} items, "
                                  f"{r['errors']} errors, concurrency {r['concurrency']})"),
                (name, "items/s", f"{r['items_per_s']:,}"),
                (name, "latency p50 / p90", f"{r['latency_p50_ms']} / {r['latency_p90_ms']} ms"),
                (name, "CPU / peak memory", f"{r['cpu_s']} s / {_mb(r['peak_rss_mb'])} ({r['megabytes']} MB downloaded)"),
            ]  # fmt: skip
        elif name == "parse":
            rows.append((name, "pages/s", f"{r['pages_per_s']:,} ({r['page_kb']} KB pages, 20 cards each)"))
        elif name == "extract":
            rows.append((name, "pages/s", f"{r['pages_per_s']:,} (the product template's {r['fields']} fields)"))
        elif name == "data":
            rows.append((name, "records/s", f"{r['records_per_s']:,} (normalized and validated, {r['fields']} fields)"))
        elif name == "dedupe":
            rows += [(name, "URLs/s", f"{r['urls_per_s']:,} (made canonical: {r['urls']:,} URLs, {r['unique_urls']:,} pages)"),
                     (name, "pages/s", f"{r['simhash_pages_per_s']:,} (near-duplicate fingerprints)")]  # fmt: skip
        elif name == "browser":
            rows.append((name, "pages/s", f"{r['pages_per_s']:,} ({r['times_slower']}x slower than HTTP from this "
                                          f"machine: {r['http_pages_per_s']:,} pages/s, one page at a time)"))  # fmt: skip
    width = [max(len(row[i]) for row in rows) for i in range(2)] if rows else [0, 0]
    lines += [f"{a.ljust(width[0])}  {b.ljust(width[1])}  {c}" for a, b, c in rows]
    return "\n".join(lines)


def _mb(value: float | None) -> str:
    return "unknown" if value is None else f"{value} MB"


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m wintergrab.bench serve|scenario``: the shop, and one scenario (used by :func:`run_benchmark`)."""
    parser = argparse.ArgumentParser(prog="python -m wintergrab.bench")
    sub = parser.add_subparsers(dest="action", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--pages", type=int, default=100)
    serve.add_argument("--items", type=int, default=1000)
    serve.add_argument("--latency", type=float, default=0.0)
    serve.add_argument("--port", type=int, default=0)
    scenario = sub.add_parser("scenario")
    scenario.add_argument("name", choices=list(_RUNNERS))
    scenario.add_argument("--url", default="")
    scenario.add_argument("--concurrency", type=int, default=32)
    scenario.add_argument("--rounds", type=int, default=200)
    args = parser.parse_args(argv)
    if args.action == "serve":
        serve_shop(args.pages, args.items, latency=args.latency, port=args.port)
        return 0
    result = _RUNNERS[args.name](url=args.url, concurrency=args.concurrency, rounds=args.rounds)
    print("RESULT " + json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
