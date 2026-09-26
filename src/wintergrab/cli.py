"""Command line interface: ``wintergrab get``, ``crawl``, ``data``, ``shell`` and ``doctor``."""

from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.util
import inspect
import io
import json
import logging
import os
import sys
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .errors import ConfigurationError, FetchError, WintergrabError, describe
from .fetchers import AsyncBrowserFetcher, AsyncFetcher, Response
from .parser import Selector
from .proxy import ProxyRotator
from .redact import redact_url
from .spider import Spider
from .spider.exporters import dumps, to_dict
from .utils import configure_logging, ensure_scheme, host_of

FORMATS = ("md", "text", "html", "json", "jsonl")
_EXT_FORMATS = {
    ".md": "md",
    ".markdown": "md",
    ".txt": "text",
    ".html": "html",
    ".htm": "html",
    ".json": "json",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".csv": "csv",
}

EPILOG_GET = """examples:
  wintergrab get https://quotes.toscrape.com                      # page as Markdown
  wintergrab get https://quotes.toscrape.com --css ".quote .text::text"
  wintergrab get https://quotes.toscrape.com -o page.html
  wintergrab get https://quotes.toscrape.com --each .quote \\
      --field text=.text::text --field author=.author::text -o quotes.csv
  wintergrab get https://example.com --browser --wait-for "#app" --screenshot shot.png
"""

EPILOG_CRAWL = """examples:
  wintergrab crawl my_spider.py -o items.jsonl --crawl-dir .crawl/my_spider
  wintergrab crawl my_spider.py:BooksSpider -s max_pages=50 -s concurrency=4
  wintergrab crawl https://books.toscrape.com --follow ".pager a" --follow "h3 a" \\
      --each "article.product_page" --field title=h1::text --field price=.price_color::text \\
      -o books.jsonl --max-pages 200
  wintergrab crawl https://shop.example --sitemap https://shop.example/sitemap.xml --follow a \\
      --profile site.json -o pages.jsonl         # also the site's sections, dead ends, orphans

Press Ctrl+C once to pause (state is saved when --crawl-dir is set); run the
same command again to resume. Press Ctrl+C twice to force quit.
"""

EPILOG_EXTRACT = """examples:
  wintergrab extract https://books.toscrape.com/ --schema product -o books.jsonl
  wintergrab extract https://shop.example/ --schema product.schema.json --max-pages 500 --provenance
  wintergrab extract https://jobs.example/ --schema job --follow "a.job-link" -o jobs.csv

A page without the schema's required field gives no record, so crawling a whole site keeps only the
pages that hold one. --follow limits the crawl to the links that lead to them.
"""

EPILOG_BENCHMARK = """examples:
  wintergrab benchmark                          # everything but the browser, about 12 seconds
  wintergrab benchmark --browser                # and pages rendered in Chromium
  wintergrab benchmark --scenario crawl --pages 1000 --items 10000 --concurrency 64
  wintergrab benchmark --scenario outputs --store "postgresql://crawler@db/shop?table=bench"   # and a database
  wintergrab benchmark --latency 50 --json -o bench.json   # as over a network: 50 ms per response

Everything runs on this machine: a synthetic shop served from 127.0.0.1, and each scenario in a
process of its own. Compare runs on one machine; see docs/benchmarks.md for the method.
"""

EPILOG_SEARCH = """examples:
  wintergrab search "budget laptop" "laptop under 500" -o serp.jsonl        # Brave: BRAVE_SEARCH_API_KEY
  wintergrab search "budget laptop" --provider searxng --endpoint https://searx.example
  wintergrab search --report serp.jsonl --domain shop.example --before last-week.jsonl
  wintergrab search "budget laptop" -o history.jsonl --append     # each week: --report shows the history
  wintergrab search "günstiger laptop" --param country=de --param search_lang=de   # where, in what language

Only search APIs are asked, with your own access; search engines' result pages are not fetched.
Requests go one at a time, a second apart. See docs/search.md.
"""

EPILOG_INSPECT = """examples:
  wintergrab inspect https://shop.example                 # 30 pages, robots.txt and sitemaps
  wintergrab inspect https://shop.example --pages 100 -o shop.profile.json
  wintergrab inspect https://shop.example --depth 4 --show 20   # more of the section tree
  wintergrab inspect https://app.example --browser        # render pages, record their XHR/fetch calls
"""

EPILOG_GOAL = """examples:
  wintergrab goal "Find all laptops under $1000 on shop.example with name, price and rating"
  wintergrab goal "jobs posted in the last 30 days with title, company and salary" --site jobs.example
  wintergrab goal "articles from news.example published in 2025" --plan-only --save-plan news.plan.json
  wintergrab goal --plan news.plan.json --yes -o articles.jsonl   # run a saved (maybe edited) plan

The request is read by rules (entities, fields, conditions such as "under $1000",
"rated 4 or more", "in the last 30 days", "in stock"); the plan shows how it was
understood, what will be fetched, and what it will cost, before anything big runs.
"""

EPILOG_GENERATE = """examples:
  wintergrab generate "books with title, price and rating" --site books.example -o scrapers/books
  wintergrab generate "products with name, price and sku on shop.example" -o scrapers/shop \\
      --model openai:MODEL           # a model finds what the pages do not publish, once
  wintergrab goal --plan scrapers/books/plan.json --yes -o books.jsonl   # run what was accepted
  wintergrab test scrapers/books/fixtures                                # its tests, any time

Steps: plan (survey the site), generate (selectors learned from record pages and
checked on each), lint, test (the pages as extraction tests), sample crawl (pages
beyond those), validate (quality, agreement with the goal's own extraction),
benchmark, then accept or reject. The exit status is 0 when it is accepted.
"""

EPILOG_HISTORY = """examples:
  wintergrab crawl https://shop.example --history shop.history -o items.jsonl   # record a run
  wintergrab history shop.history                     # the runs, and what changed in the last one
  wintergrab history shop.history --compare 3 7       # between two runs
  wintergrab history shop.history --url https://shop.example/p/1   # one page over time
  wintergrab history shop.history --due               # pages that have probably changed by now
  wintergrab crawl https://shop.example --history shop.history --skip-fresh   # fetch only those
"""

EPILOG_DATA = """examples:
  wintergrab data infer items.jsonl -o product.schema.json       # guess a schema from records
  wintergrab data validate product.schema.json items.jsonl -o clean.jsonl --rejects rejects.jsonl
  wintergrab data run pipeline.yaml items.jsonl -o clean.csv
  wintergrab data quality items.jsonl --schema product.schema.json --save quality.json
  wintergrab data quality items.jsonl --baseline quality.json    # exit status 1 if quality collapsed
  wintergrab data entities companies.jsonl --field name --attribute website --attribute phone -o entities.jsonl
  wintergrab data entities products.jsonl --field brand --kind brand --annotate products.resolved.jsonl
  wintergrab data graph job=jobs.jsonl company=companies.jsonl -o graph.graphml   # the things they name, linked
  wintergrab data analyze articles.jsonl --field body -o analyzed.jsonl   # language, keywords, length
  wintergrab data places jobs.jsonl --country US --by region --stats salary  # where they are, grouped
  wintergrab data places shops.jsonl --near 52.52,13.405 --within 10 -o near-berlin.jsonl
  wintergrab data commit prices/ today.jsonl --key url -m "daily run"   # save a version
  wintergrab data diff prices/@previous prices/@latest                    # what changed since the last one
  wintergrab data diff yesterday.jsonl today.jsonl --key sku -o changes.jsonl

Inputs are JSON Lines, JSON or CSV files ("-" reads JSON Lines from stdin).
"""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _parse_pairs(values: Sequence[str] | None, sep: str, what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in values or ():
        if sep not in raw:
            raise SystemExit(f"error: {what} must look like NAME{sep}VALUE, got {raw!r}")
        key, value = raw.split(sep, 1)
        out[key.strip()] = value.strip()
    return out


def _proxies(args: argparse.Namespace) -> ProxyRotator | None:
    proxies = list(args.proxy or [])
    if getattr(args, "proxy_file", None):
        proxies.extend(Path(args.proxy_file).read_text(encoding="utf-8").splitlines())
    proxies = [p for p in proxies if p.strip() and not p.strip().startswith("#")]
    return ProxyRotator(proxies) if proxies else None


def _format_for(args: argparse.Namespace, default: str) -> str:
    if args.format:
        return str(args.format)
    if args.output:
        return _EXT_FORMATS.get(Path(args.output).suffix.lower(), default)
    return default


def _fields(args: argparse.Namespace) -> dict[str, str]:
    return _parse_pairs(args.field, "=", "--field")


def _emit(text: str, output: str | None, append: bool = False) -> None:
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a" if append else "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
        sys.stdout.flush()


def _write_rows(rows: list[dict[str, Any]], fmt: str, output: str | None, *, single: bool = False) -> None:
    if fmt == "json":
        data: Any = to_dict(rows[0]) if single and len(rows) == 1 else [to_dict(r) for r in rows]
        text = json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n"
    elif fmt == "csv":
        buf = io.StringIO()
        columns = list(dict.fromkeys(k for r in rows for k in r))
        # "\n": _emit's text-mode file or console adds the platform's line ending
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    k: v
                    if isinstance(v, (str, int, float, bool)) or v is None
                    else json.dumps(v, ensure_ascii=False, default=str)
                    for k, v in row.items()
                }
            )
        text = buf.getvalue()
    else:
        text = "".join(dumps(r) + "\n" for r in rows)
    _emit(text, output)


def _maybe_json(captured: Any) -> Any:
    try:
        return captured.json()
    except ValueError:
        return captured.text[:2000]


def _value(sel: Selector, fmt: str) -> str:
    if not sel.is_element:
        return sel.get() or ""
    if fmt == "html":
        return sel.html
    if fmt == "md":
        return sel.markdown().strip()
    return sel.text


def _cache_options(args: argparse.Namespace) -> dict[str, Any]:
    """``--cache [DIR]`` / ``--cache-mode`` / ``--offline`` as fetcher keyword arguments."""
    mode = "offline" if getattr(args, "offline", False) else getattr(args, "cache_mode", None)
    location: Any = getattr(args, "cache_dir", None) or getattr(args, "cache", False)
    if not location and mode:
        location = True
    if not location:
        return {}
    return {"cache": location, "cache_mode": mode}


def _load_schema(path: str) -> Any:
    from .parser.autoextract import LearnedSchema

    return LearnedSchema.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- #
# get
# --------------------------------------------------------------------------- #
async def _fetch_all(args: argparse.Namespace, urls: list[str]) -> list[Response | Exception]:
    headers = _parse_pairs(args.header, ":", "--header")
    cookies = _parse_pairs(args.cookie, "=", "--cookie")
    rotator = _proxies(args)
    fetcher: Any
    cache = _cache_options(args)
    if args.browser or args.capture:
        fetcher = AsyncBrowserFetcher(
            headless=not args.headful,
            proxies=rotator,
            timeout=args.timeout,
            retries=args.retries,
            cookies=cookies or None,
            extra_headers=headers or None,
            resource_filter=True if args.block_trackers else None,
            network_policy="public" if args.public_only else None,
            **cache,
        )
        options: dict[str, Any] = {"wait_for": args.wait_for, "wait": args.wait, "scroll": args.scroll}
        if args.screenshot or getattr(args, "vision", False):
            options["screenshot"] = args.screenshot or True
        if getattr(args, "layout", False) or getattr(args, "visual_tables", False):
            options["layout"] = True
        if getattr(args, "steps", None):
            options["actions"] = args.steps
            if args.downloads:
                options["downloads"] = args.downloads
        if args.capture or getattr(args, "sources", False):  # --sources: the page's API calls are sources too
            options["capture"] = args.capture_filter or True
            options.setdefault("wait_until", "networkidle")
        if getattr(args, "auto_browser", False):
            options.setdefault("wait_until", "networkidle")  # the page's own requests fill it in
    else:
        impersonate = None if args.impersonate in ("none", "") else args.impersonate
        fetcher = AsyncFetcher(
            impersonate=impersonate,
            proxies=rotator,
            timeout=args.timeout,
            retries=args.retries,
            verify=not args.insecure,
            headers=headers,
            cookies=cookies,
            network_policy="public" if args.public_only else None,
            **cache,
        )
        options = {}

    async def one(url: str) -> Response | Exception:
        try:
            return await fetcher.get(url, **options)
        except Exception as exc:  # reported per URL
            return exc

    try:
        return list(await asyncio.gather(*(one(u) for u in urls)))
    finally:
        await fetcher.aclose()


def _render_where_needed(args: argparse.Namespace, urls: list[str], results: list[Any]) -> list[Any]:
    """``--auto-browser``: fetch again in a browser the pages whose HTML is not enough."""
    from .fetchers.blocking import looks_blocked
    from .fetchers.strategy import needs_javascript

    again: list[int] = []
    for i, result in enumerate(results):
        if not isinstance(result, Response) or not 200 <= result.status < 300 or looks_blocked(result):
            continue
        missing = [css for css in args.render_if_missing or () if not result.css(css)]
        reason = f"nothing matches {missing[0]!r}" if missing else needs_javascript(result)
        if reason:
            again.append(i)
            if args.verbose >= 0:
                print(f"{result.url}: fetching it again in a browser ({reason})", file=sys.stderr)
    if not again:
        return results
    args.browser = True
    rendered = asyncio.run(_fetch_all(args, [urls[i] for i in again]))
    for i, result in zip(again, rendered, strict=True):
        results[i] = result
    return results


_LISTING_TYPES = frozenset({"listing", "category", "search", "archive", "directory"})


def _is_listing(page: Response, schema_name: str) -> bool:
    """Whether ``page`` looks like a list of records (a category, search results...), for a schema of one record."""
    if schema_name in _LISTING_TYPES:
        return False  # the schema describes listing pages themselves
    from .intel import classify_page

    kind = classify_page(page)
    return kind.type in _LISTING_TYPES and kind.confidence >= 0.4


def cmd_get(args: argparse.Namespace) -> int:
    urls = [ensure_scheme(u) for u in args.urls]
    if args.capture_filter:
        args.capture = True
    if args.vision and not (args.extract and args.model):
        print("error: --vision shows the page's screenshot to a model: it needs --extract and --model", file=sys.stderr)
        return 2
    if args.layout or args.visual_tables or args.vision:
        args.browser = True  # what a page looks like needs a browser to draw it
    args.steps = []
    try:
        from .fetchers.actions import load_actions, parse_actions

        if args.actions:
            args.steps += load_actions(args.actions)
        args.steps += parse_actions(args.do or [])
    except (WintergrabError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.steps:
        args.browser = True  # actions are done in a browser
    fields = _fields(args)
    if fields and not args.each:
        args.each = ["html"]  # one record for the whole page
    examples = _parse_pairs(args.learn, "=", "--learn")
    schema = _load_schema(args.schema) if args.schema else None
    extractor = _extractor(args)
    # Modes that print one JSON document per page instead of page content.
    json_modes = [
        m for m in ("structured", "json_data", "tables", "visual_tables", "next", "capture") if getattr(args, m)
    ]
    selecting = bool(args.css or args.xpath)
    records = bool(args.each or args.auto or examples or schema or extractor)
    if json_modes:
        args.format = args.format or ("jsonl" if len(urls) > 1 else "json")
    default_fmt = "jsonl" if records else ("text" if selecting or args.sources else "md")
    fmt = _format_for(args, default_fmt)
    if fmt == "csv" and not records:
        raise SystemExit("error: CSV output needs --each/--field")

    if args.render_if_missing:
        args.auto_browser = True
    results = asyncio.run(_fetch_all(args, urls))
    if args.auto_browser and not (args.browser or args.capture):
        results = _render_where_needed(args, urls, results)
    failures = 0
    rows: list[dict[str, Any]] = []
    chunks: list[str] = []
    for url, result in zip(urls, results, strict=True):
        if isinstance(result, Exception):
            failures += 1
            detail = str(result) if isinstance(result, FetchError) else f"{url}: {describe(result)}"
            print(f"error: {detail}", file=sys.stderr)
            continue
        page = result
        if args.verbose >= 0:
            print(f"{page.status} {page.url} ({len(page.body):,} bytes, {page.elapsed:.2f}s)", file=sys.stderr)
            for done in page.actions:
                print(f"  {'ok' if done['ok'] else '--'} {done['step']}: {done['detail']}", file=sys.stderr)
        if page.status >= 400:
            failures += 1
        multi = len(urls) > 1
        if args.sources:
            from .intel.sources import data_sources

            inventory = data_sources(page, recorded=bool(args.browser or args.capture))
            if fmt in ("json", "jsonl"):
                rows.append(inventory.to_dict())
            else:
                chunks.append(inventory.describe() + "\n")
            continue
        if json_modes:
            doc: dict[str, Any] = {"url": page.url}
            if args.structured:
                doc["structured"] = page.structured_data()
            if args.json_data:
                doc["embedded_json"] = page.embedded_json()
            if args.tables:
                doc["tables"] = page.tables()
            if args.visual_tables:
                from .extraction.visual import layout_tables

                doc["visual_tables"] = (
                    [{**table.to_dict(), "records": table.records()} for table in layout_tables(page.layout)]
                    if page.layout is not None
                    else []
                )
            if args.next:
                doc["next_page"] = page.next_page()
            if args.capture:
                doc["captured"] = [
                    {"url": c.url, "method": c.method, "status": c.status, "json": _maybe_json(c)}
                    for c in page.captured
                ]
            rows.append(doc)
            continue
        if extractor is not None:
            listing = args.all or args.container or extractor.schema.container  # a listing's schema says so
            found = extractor.extract_all(page, container=args.container) if listing else [extractor.extract(page)]
            if listing and not found:
                print(f"warning: no records found on {page.url}", file=sys.stderr)
            elif not listing and args.verbose >= 0 and _is_listing(page, extractor.schema.name):
                print(f"note: {page.url} looks like a list of records: --all reads each of them", file=sys.stderr)
            for record in found:
                if args.explain:
                    print(record.explain(), file=sys.stderr)
                row = record.to_dict(provenance=args.provenance)
                rows.append({"url": page.url, **row} if multi and "url" not in row else row)
            if getattr(args, "why", None):
                if args.why not in extractor.schema:
                    print(f"error: the schema has no field {args.why!r}", file=sys.stderr)
                    return 2
                print(extractor.why(args.why, page), file=sys.stderr)
            continue
        if examples or schema:
            if schema is None:
                try:
                    schema = page.learn(examples)
                except ValueError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 1
                print(f"learned: {json.dumps(schema.to_dict(), ensure_ascii=False)}", file=sys.stderr)
                if args.save_schema:
                    Path(args.save_schema).write_text(
                        json.dumps(schema.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                    print(f"schema saved to {args.save_schema}", file=sys.stderr)
            rows.extend({"url": page.url, **row} if multi else row for row in schema.extract(page))
            continue
        if args.auto:
            found = page.auto_extract()
            if not found:
                print(f"warning: no repeating records found on {page.url}", file=sys.stderr)
            rows.extend({"url": page.url, **row} if multi else row for row in found)
            continue
        if records:
            for each in args.each:
                for el in page.select(each, adaptive=args.adaptive):
                    row = el.extract(fields) if fields else {"value": _value(el, "text")}
                    if multi:
                        row = {"url": page.url, **row}
                    rows.append(row)
        elif selecting:
            values: list[str] = []
            for query in args.css or ():
                values.extend(_value(s, fmt) for s in page.css(query, adaptive=args.adaptive))
            for query in args.xpath or ():
                values.extend(_value(s, fmt) for s in page.xpath(query, adaptive=args.adaptive))
            if fmt in ("json", "jsonl"):
                rows.append({"url": page.url, "matches": values} if multi else {"matches": values})
            else:
                chunks.append("\n".join(values) + ("\n" if values else ""))
        else:
            if fmt == "html":
                chunks.append(page.text)
            elif fmt == "text":
                chunks.append(page.get_text() + "\n")
            elif fmt in ("json", "jsonl"):
                rows.append(
                    {
                        "url": page.url,
                        "status": page.status,
                        "title": page.title,
                        "markdown": page.markdown(main_content=args.main_content),
                    }
                )
            else:
                chunks.append(page.markdown(main_content=args.main_content))
                for kept in page.snapshots:  # tabs: each one's content, as it was shown
                    shown = Response(page.url, body=kept["html"].encode(), headers={"content-type": "text/html"})
                    chunks.append(
                        f"\n<!-- after {kept['after']} -->\n\n" + shown.markdown(main_content=args.main_content)
                    )

    if extractor is not None and hasattr(extractor, "close"):
        extractor.close()  # a healing extractor keeps what it learned
    if records or json_modes or fmt in ("json", "jsonl", "csv"):
        _write_rows(rows, fmt, args.output, single=bool(json_modes) or args.sources)  # one page: one document
        if args.output:
            print(f"wrote {len(rows)} record(s) to {args.output}", file=sys.stderr)
    else:
        _emit("\n".join(chunks), args.output)
        if args.output:
            print(f"saved to {args.output}", file=sys.stderr)
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
# crawl
# --------------------------------------------------------------------------- #
class QuickSpider(Spider):
    """The spider behind ``wintergrab crawl URL``: follow links, extract fields."""

    name = "quick"
    follow: Sequence[str] = ()
    allow: Sequence[str] = ()
    deny: Sequence[str] = ()
    each: Sequence[str] = ()
    fields: dict[str, str] = {}
    auto: bool = False
    paginate: bool = False
    schema: dict[str, Any] | None = None
    #: A data schema file: extract typed records (see wintergrab.extraction).
    extract: str | None = None
    extract_all: bool = False
    container: str | None = None
    provenance: bool = False
    #: A directory of extractor versions: repair selectors when the site changes (--heal).
    heal: str | None = None
    #: A review queue file for what the healing extractor wants a person to decide (--review).
    review: str | None = None
    #: A language model for the fields the page's own data does not give (--model PROVIDER:NAME).
    model: str | None = None
    model_url: str | None = None
    #: Pages where --extract found no complete record (a required field missing).
    incomplete: int = 0
    #: Without --all, a page that looks like a listing (a category, search results) gives no single record: its
    #: title and first card would make one that no page states (``-s skip_listings=false`` reads them anyway).
    skip_listings: bool = True
    #: Pages left out for that.
    listing_pages: int = 0
    #: Follow every same-domain link when no --follow/--paginate is given.
    wander: bool = True
    #: Skip links to images, media, archives and crawler traps (``-s url_rules=null`` turns it off).
    url_rules = True

    def _records(self, response: Response) -> Any:
        extractor = self.__dict__.get("_extractor")
        if extractor is None:
            model = None
            if self.model:
                from .models import load_model

                model = self.__dict__["_model"] = load_model(self.model, base_url=self.model_url)
            if self.heal:
                from .extraction.healing import HealingExtractor

                extractor = HealingExtractor(self.heal, self.extract, review=self.review, provenance=self.provenance,
                                             model=model)  # fmt: skip
            else:
                from .extraction import Extractor

                extractor = Extractor(self.extract, provenance=self.provenance, model=model)  # type: ignore[arg-type]
            self.__dict__["_extractor"] = extractor
        if self.extract_all or self.container or extractor.schema.container:
            found = extractor.extract_all(response, container=self.container)
        else:
            if self.skip_listings and _is_listing(response, extractor.schema.name):
                self.listing_pages += 1
                return
            found = [extractor.extract(response)]
        for record in found:
            missing = [name for name, fv in record.fields.items() if fv.validation == "missing"]
            if missing:
                self.incomplete += 1
                if self.events.wants("extraction_failed"):
                    self.events.emit(
                        "extraction_failed", url=response.url, schema=extractor.schema.name, missing=missing
                    )
                continue
            row = record.to_dict()
            yield row if "url" in row else {"url": response.url, **row}

    def on_close(self, result: Any) -> None:
        extractor = self.__dict__.get("_extractor")
        if extractor is not None and hasattr(extractor, "close"):
            extractor.close()  # a healing extractor keeps what it learned for the next run
        model = self.__dict__.get("_model")
        if model is not None and model.usage["requests"]:
            used = model.usage
            logging.getLogger("wintergrab.models").info(
                "%s: %s request(s), %s input and %s output tokens", model.name, f"{used['requests']:,}",
                f"{used['input']:,}", f"{used['output']:,}",
            )  # fmt: skip

    def parse(self, response: Response) -> Any:
        if self.extract or self.heal:
            if response.is_html:
                yield from self._records(response)
        elif self.schema:
            from .parser.autoextract import LearnedSchema

            for row in LearnedSchema.from_dict(self.schema).extract(response):
                yield {"url": response.url, **row}
        elif self.auto:
            for row in response.auto_extract():
                yield {"url": response.url, **row}
        elif self.each:
            for query in self.each:
                for el in response.select(query):
                    row = el.extract(self.fields) if self.fields else {"text": el.text}
                    yield {"url": response.url, **row}
        elif self.fields:
            yield {"url": response.url, **response.extract(self.fields)}
        else:
            yield {"url": response.url, "status": response.status, "title": response.title}
        if not response.is_html:
            return
        links: list[str] = []
        extractor = self.__dict__.get("_extractor")
        next_page = extractor.schema.next_page if extractor is not None else None
        if self.follow:
            for query in self.follow:
                links.extend(response.links(query, allow=self.allow or None, deny=self.deny or None))
        elif next_page:  # a listing's schema: its next pages, not the whole site
            links = response.links(next_page, allow=self.allow or None, deny=self.deny or None)
        elif not self.paginate and self.wander:
            links = response.links(allow=self.allow or None, deny=self.deny or None, same_domain=True)
        if self.paginate:
            next_url = response.next_page()
            if next_url:
                links.append(next_url)
        for link in dict.fromkeys(links):
            yield response.follow(link)


def cmd_inspect(args: argparse.Namespace) -> int:
    from .intel.survey import survey_site

    try:
        survey = survey_site(
            args.url,
            pages=args.pages,
            sitemaps=not args.no_sitemaps,
            obey_robots=not args.no_robots,
            browser=args.browser,
            timeout=args.timeout,
            log_level="DEBUG" if args.verbose > 0 else "WARNING",
        )
    except WintergrabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    profile = survey.profile
    data = json.dumps(profile.to_dict(), indent=2, ensure_ascii=False, default=str)
    if args.output:
        Path(args.output).write_text(data, encoding="utf-8")
    if args.json:
        print(data)
    else:
        print(profile.describe(args.show, depth=args.depth))
        if args.output:
            print(f"saved the full profile to {args.output}", file=sys.stderr)
    return 0


def cmd_goal(args: argparse.Namespace) -> int:
    from .goals import GoalPlan, parse_goal, plan_goal

    level = "DEBUG" if args.verbose > 0 else "WARNING"
    try:
        if args.plan:
            plan = GoalPlan.load(args.plan)
            if args.verbose >= 0:
                print(f"plan {args.plan} (made {plan.created})", file=sys.stderr)
        else:
            if not args.text:
                print('error: say what to collect, e.g. wintergrab goal "products with name and price on shop.example"')
                return 2
            reader = None
            if args.model:
                from .goals import model_reader

                reader = model_reader(_model(args))
            goal = parse_goal(" ".join(args.text), sites=args.site or [], parser=reader)
            if args.verbose >= 0:
                print("Understood: " + goal.describe().replace("\n", "\n            "), file=sys.stderr)
            if not goal.sites:
                if args.find_sites:
                    return _candidate_sites(args, " ".join(args.text))
                print(
                    "error: which site? Name it in the request (shop.example) or add --site URL "
                    "(--find-sites asks a search API for candidates)",
                    file=sys.stderr,
                )
                return 2
            if args.verbose >= 0:
                print(
                    f"Surveying {', '.join(goal.sites)}: robots.txt, sitemaps, {args.sample} pages...", file=sys.stderr
                )
            plan = plan_goal(
                goal,
                sample=args.sample,
                browser=args.browser,
                timeout=args.timeout,
                log_level=level,
                api=not args.no_api,
            )
    except WintergrabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.save_plan:
        plan.save(args.save_plan)
        print(f"saved the plan to {args.save_plan}", file=sys.stderr)
    if args.json:
        print(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False, default=str))
        return 0
    # the plan on stdout when it is all there is; on stderr when the records may go to stdout
    shown = sys.stdout if args.plan_only else sys.stderr
    if args.plan_only or args.verbose >= 0:
        whole = bool(args.plan) or args.plan_only  # else "Understood:" said what the goal is
        print(plan.describe() if whole else "\n" + plan.describe(goal=False), file=shown)
        if args.explain:
            print("\nWhat the estimates rest on:\n" + plan.explain(), file=shown)
    if args.plan_only:
        return 0
    estimate = plan.estimate
    if not any(site.allowed for site in plan.sites):
        print("robots.txt keeps crawlers out: nothing to collect", file=sys.stderr)
        return 1
    big = estimate.requests > args.confirm_over or estimate.browser_pages > 50
    if big and not args.yes:
        if sys.stdin.isatty():
            answer = input(f"Run it? About {estimate.requests:,} requests. [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                return 0
        else:
            print(f"the plan makes about {estimate.requests:,} requests: add --yes to run it", file=sys.stderr)
            return 0
    output = args.output or "-"
    result = plan.run(
        output,
        max_pages=args.max_pages,
        keep_items=output == "-",
        use_api=not args.no_api,
        log_level="DEBUG" if args.verbose > 0 else ("WARNING" if args.verbose < 0 else "INFO"),
        progress=False if output == "-" else None,
        optimize=not args.no_optimize,
        **_workspace_settings(args, None),  # a goal run's recipe is its plan
        **_project_settings(args),
        **({"pipelines": [_quality_monitor(args.quality, plan.goal.kind.name)]} if args.quality else {}),
    )
    if args.verbose >= 0:
        print(result.summary(), file=sys.stderr)
        if result.crawl is not None and result.crawl.run_id:
            print(f"kept as {result.crawl.run_id}", file=sys.stderr)
        if plan.goal.monitor:
            again = f"wintergrab goal --plan {args.save_plan or args.plan or 'PLAN.json'} --yes -o {args.output or 'OUT.jsonl'}"
            print(f"to watch for changes ({plan.goal.monitor}), run this again on a schedule: {again}", file=sys.stderr)
    return 0


def _candidate_sites(args: argparse.Namespace, text: str) -> int:
    """The sites that rank for a goal's request, from a search API: printed, for the user to pick one."""
    from .intel.serp import competitors, search

    try:
        answer = search(text, provider=args.provider, endpoint=args.endpoint, pages=1, timeout=args.timeout)
    except WintergrabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    ranked = competitors(answer.results, top=10)
    if not ranked:
        print(f"{args.provider} found no site for it: name one with --site URL", file=sys.stderr)
        return 2
    print(f"Sites that rank for it ({args.provider}, {len(answer.results)} results):")
    for n, site in enumerate(ranked, 1):
        example = next(r for r in answer.results if r.domain == site.domain)
        print(f"  {n:>2}. {site.domain:<32} best #{min(r.position for r in answer.results if r.domain == site.domain)}"
              f"  {example.title[:60]}")  # fmt: skip
    print(f"pick one: wintergrab goal {_quoted(text)} --site {ranked[0].domain}", file=sys.stderr)
    return 2


def _quoted(text: str) -> str:
    import shlex

    return shlex.quote(text)


def cmd_search(args: argparse.Namespace) -> int:
    from collections import defaultdict

    from .data.io import read_records
    from .intel.serp import (
        cluster_queries,
        competitors,
        gaps,
        modules,
        ranking_changes,
        ranking_history,
        read_results,
        search,
        visibility_score,
    )
    from .spider.exporters import open_exporter

    depth = args.depth if args.depth is not None else 10
    show = args.show if args.show is not None else 15
    if args.report:
        searching = (("QUERY", args.query), ("-o", args.output), ("--append", args.append), ("--provider", args.provider),
                     ("--endpoint", args.endpoint), ("--pages", args.pages), ("--param", args.param),
                     ("--delay", args.delay is not None), ("--timeout", args.timeout is not None))  # fmt: skip
        misplaced = [name for name, given in searching if given]
        if misplaced:
            print(f"error: {', '.join(misplaced)}: for searching; --report reads results collected", file=sys.stderr)
            return 2
        if args.before and not args.domain:
            print("error: --before compares a domain's positions: say which with --domain", file=sys.stderr)
            return 2
        try:
            everything = read_results(r for path in args.report for r in read_records(path))
            before = read_results(read_records(args.before)) if args.before else []
        except WintergrabError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        web = [r for r in everything if r.type == "web"]
        if not web:
            print("error: no search results in it (records with a query, a position and a URL)", file=sys.stderr)
            return 1
        collections: dict[str, set[str]] = defaultdict(set)
        for result in web:
            collections[result.searched].add(result.fetched)
        queries = len(collections)
        searched = max(len(times) for times in collections.values())
        dated = sorted(t for times in collections.values() for t in times if t)
        print(
            f"{_many(len(web), 'result', 'results')} for {_many(queries, 'query', 'queries')}"
            + (f", searched up to {searched} times" if searched > 1 else "")
            + (f" ({dated[0][:10]} to {dated[-1][:10]}): what follows reads each query's latest" if searched > 1
               and dated else "")
        )  # fmt: skip
        if args.domain:
            print(f"{args.domain}: visibility {visibility_score(web, args.domain, depth=depth)} "
                  f"(1.0: first for every query)")  # fmt: skip
        print("\nCompetitors (visibility: the sum of 1/position, over the queries):")
        for v in competitors(web, domain=args.domain, depth=depth, top=show):
            print(
                f"  {v.domain:<32} {v.visibility:<6} in {v.queries} of {_many(queries, 'query', 'queries')}, "
                f"average #{v.average_position}"
            )
        clusters = [c for c in cluster_queries(web, depth=depth) if len(c) > 1]
        if clusters:
            print("\nQueries one page can answer (they share results):")
            for cluster in clusters[:show]:
                print("  " + " | ".join(cluster))
        boxes = modules(everything, domain=args.domain)
        if boxes:
            print("\nBesides web results, the pages have:")
            for box in boxes[:show]:
                print(
                    f"  {box.type:<12} in {box.queries} of {_many(queries, 'query', 'queries')}"
                    + (f", place #{box.average_rank:g} on the page" if box.average_rank is not None else "")
                    + (": " + ", ".join(f"{d} ({n})" for d, n in box.domains.items()) if box.domains else "")
                    + (f"; {args.domain} in it for {box.own}" if box.own else "")
                )
        if args.domain:
            missing = gaps(web, args.domain, depth=depth)
            print(
                f"\nGaps: {_many(len(missing), 'query', 'queries')} where rivals rank and {args.domain} does not"
                + (":" if missing else "")
            )
            for gap in missing[:show]:
                rivals = ", ".join(f"{d} #{p}" for d, p in gap["rivals"].items())
                print(f"  {gap['query']:<40} {rivals}")
            if before:
                changes = [c for c in ranking_changes(before, web, args.domain) if c["change"] != "same"]
                print(f"\nChanges since {args.before}: {len(changes)}" + (":" if changes else ""))
                for change in changes[:show]:
                    was = f"#{change['before']}" if change["before"] else "-"
                    now = f"#{change['after']}" if change["after"] else "-"
                    print(f"  {change['query']:<40} {was} -> {now}  ({change['change']})")
            history = [h for h in ranking_history(web, args.domain) if len(h["positions"]) > 1]
            if history:
                print(f"\nHistory of {args.domain}, oldest first (the last 8 searches; -: not in the results):")
                for entry in history[:show]:
                    marks = " ".join(str(p["position"] or "-") for p in entry["positions"][-8:])
                    moved = f", {entry['change']}" if entry["change"] not in (None, "same") else ""
                    print(f"  {entry['query']:<40} {marks:<24} best #{entry['best']}{moved}")
        return 0
    if not args.query:
        print('error: say what to search for: wintergrab search "budget laptop" (or --report FILE)', file=sys.stderr)
        return 2
    misplaced = [name for name, given in (("--domain", args.domain), ("--before", args.before),
                                          ("--depth", args.depth is not None), ("--show", args.show is not None)) if given]  # fmt: skip
    if misplaced:
        print(f"error: {', '.join(misplaced)}: for --report, which reads results collected", file=sys.stderr)
        return 2
    if args.append and not args.output:
        print("error: --append adds to an output: say which with -o FILE", file=sys.stderr)
        return 2
    params: dict[str, str] = {}
    for given in args.param or ():
        name, equals, value = given.partition("=")
        if not equals or not name.strip():
            print(f"error: --param {given!r}: say NAME=VALUE (--param country=de)", file=sys.stderr)
            return 2
        params[name.strip()] = value
    exporter = open_exporter(args.output, append=args.append) if args.output else None
    delay = args.delay if args.delay is not None else 1.0
    found = others = 0
    try:
        for n, query in enumerate(args.query):
            if n:
                time.sleep(delay)
            answer = search(query, provider=args.provider or "brave", endpoint=args.endpoint, pages=args.pages or 1,
                            params=params, delay=delay, timeout=args.timeout or 20)  # fmt: skip
            found, others = found + len(answer.results), others + len(answer.modules)
            if exporter is not None:
                for record in answer.records():
                    exporter.write(record)
            elif args.verbose >= 0:
                print(
                    f"{answer.searched}  ({len(answer.results)} results"
                    + (f" of {answer.total:,}" if answer.total else "")
                    + ")"
                )
                for result in answer.results:
                    print(f"  {result.position:>3}. {result.domain:<28} {result.title[:70]}")
                _show_boxes(answer.modules)
            for note in answer.notes:
                print(f"note: {query}: {note}", file=sys.stderr)
    except WintergrabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if exporter is not None:
            exporter.close()
    if exporter is not None and args.verbose >= 0:
        more = f" and {others:,} of the pages' other results and boxes" if others else ""
        asked = _many(len(args.query), "query", "queries")
        print(f"{_many(found, 'result', 'results')}{more} for {asked} -> {redact_url(args.output)}", file=sys.stderr)
    return 0


def _many(count: int, one: str, many: str) -> str:
    return f"{count:,} {one if count == 1 else many}"


def _show_boxes(boxes: list[Any]) -> None:
    """A search page's other results and boxes, a line for each kind, in their order on the page."""
    kinds: dict[str, list[Any]] = {}
    for box in boxes:
        kinds.setdefault(box.type, []).append(box)
    placed = sorted(kinds.items(), key=lambda kv: min((b.rank for b in kv[1] if b.rank), default=10**6))
    for kind, listed in placed:
        if kind == "related":
            print("  related: " + ", ".join(b.title for b in listed[:8]))
        elif kind == "question":
            for box in listed[:5]:
                print(f"  asked: {box.title}")
        elif kind == "correction":
            print(f"  searched for: {listed[0].title}")
        else:
            place = f" (place #{listed[0].rank})" if listed[0].rank else ""
            shown = "; ".join(b.title[:50] + (f" ({b.domain})" if b.domain else "") for b in listed[:3])
            print(f"  {kind}{place}: {shown}" + (f"; {len(listed) - 3} more" if len(listed) > 3 else ""))


def cmd_generate(args: argparse.Namespace) -> int:
    from .goals import generate_scraper

    def show(stage: Any) -> None:
        if args.verbose >= 0 and not args.json:
            print(f"{stage.name:<10} {'ok  ' if stage.ok else 'FAIL'}  {stage.summary}", file=sys.stderr)
            for problem in stage.problems:
                print(f"{'':16}{problem}", file=sys.stderr)
            for warning in stage.warnings:
                print(f"{'':16}note: {warning}", file=sys.stderr)

    try:
        result = generate_scraper(
            " ".join(args.text),
            args.out,
            sites=args.site or [],
            model=_model(args),
            sample=args.sample,
            train=args.train,
            test=args.test,
            browser=args.browser,
            timeout=args.timeout,
            min_completeness=args.min_completeness,
            min_agreement=args.min_agreement,
            log_level="DEBUG" if args.verbose > 0 else "WARNING",
            on_stage=show,
        )
    except WintergrabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str))
    elif args.verbose >= 0:
        if result.accepted:
            plan = result.directory / "plan.json"
            print(f"accepted: {plan}", file=sys.stderr)
            print(f"  run it:  wintergrab goal --plan {plan} --yes -o RECORDS.jsonl", file=sys.stderr)
            print(f"  test it: wintergrab test {result.directory / 'fixtures'}", file=sys.stderr)
        else:
            print("rejected: " + "; ".join(result.reasons), file=sys.stderr)
            print(f"  what it made, and why: {result.directory / 'report.json'}", file=sys.stderr)
    return 0 if result.accepted else 1


def cmd_review(args: argparse.Namespace) -> int:
    from .extraction.review import ReviewQueue

    try:
        queue = ReviewQueue(args.file)
        if args.accept or args.reject or args.correct:
            if args.accept:
                item = queue.decide(args.accept, "accept", choice=_choice(args.choice), note=args.note or "")
            elif args.reject:
                item = queue.decide(args.reject, "reject", note=args.note or "")
            else:
                item_id, value = args.correct
                item = queue.decide(item_id, "correct", value=value, note=args.note or "")
            print(f"{item.id}: {item.status}" + (f" ({item.chosen!r})" if item.chosen is not None else ""))
            print("the extractor applies it the next time it runs", file=sys.stderr)
            return 0
    except WintergrabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    items = list(queue) if args.all else queue.pending()
    if args.json:
        print(json.dumps([{k: v for k, v in i.to_dict().items() if k != "html"} for i in items], indent=2, default=str))
        return 0
    if not items:
        print("nothing to review" if not args.all else "the queue is empty")
        return 0
    for item in items:
        print(item.describe())
    if not args.all:
        print(
            f"\n{len(items)} item(s) to review: --accept ID [--choice B], --reject ID, --correct ID VALUE",
            file=sys.stderr,
        )
    return 0


def _project_file(given: str | None, first: str | None) -> tuple[str | None, bool]:
    """The project file: ``--project FILE``, or a first argument naming one (``wintergrab run project.yaml``);
    and whether that argument was it."""
    if first and Path(first).suffix.lower() in (".yaml", ".yml", ".toml", ".json") and Path(first).is_file():
        if given:
            raise ConfigurationError(f"two projects: {first} and --project {given}")
        return first, True
    return given, False


def cmd_run(args: argparse.Namespace) -> int:
    from .project import Scheduler, find_project
    from .redact import redact_argv

    names = list(args.jobs or [])
    path, named = _project_file(args.project, names[0] if names else None)
    names = names[1:] if named else names
    project = find_project(path)
    jobs = project.select(names)
    if args.list:
        for job in project.jobs.values():
            print(f"{job.name:<16} wintergrab {' '.join(redact_argv(job.command()))}  ({job.trigger()})")
        return 0
    if not names:  # every job: the ones that come after another run after it
        jobs = [job for job in jobs if not job.after]
    scheduler = Scheduler(project)

    def started(job: Any, trigger: str, reason: str | None) -> None:
        why = f" ({reason})" if reason else ""
        print(f"== {job.name}{why}: wintergrab {' '.join(redact_argv(job.command()))}", file=sys.stderr)

    scheduler.on_start = started
    try:
        for job in jobs:
            done = len(scheduler.results)
            scheduler.run(job, logged=False)
            for result in scheduler.results[done:]:
                print(result.describe(), file=sys.stderr)
    finally:
        for hook in scheduler.webhooks:
            hook.close()
    return 1 if any(not r.ok for r in scheduler.results) else 0


def cmd_schedule(args: argparse.Namespace) -> int:
    from .project import Scheduler, find_project

    if args.listen and (args.list or args.once):
        print("error: --listen keeps the scheduler running: not with --list or --once", file=sys.stderr)
        return 2
    if args.file and args.project:
        print(f"error: two projects: {args.file} and --project {args.project}", file=sys.stderr)
        return 2
    project = find_project(args.file or args.project)
    scheduler = Scheduler(project, webhooks=[] if args.list else None)  # listing needs no secrets
    plan = scheduler.plan()
    if args.list or (not plan and not args.listen):
        now = scheduler.now()
        for job, when in plan:
            next_time = "never" if when is None else "now (due)" if when <= now else f"{when:%Y-%m-%d %H:%M}"
            print(f"{job.name:<16} {job.trigger():<28} next{' check' if job.watch and not job.schedule else ''}: "
                  f"{next_time}")  # fmt: skip
        for job in project.jobs.values():
            if job.schedule is None and not job.watch:
                shown = job.trigger() if job.after else f"(when asked: wintergrab run {job.name})"
                print(f"{job.name:<16} {shown}")
        if not plan:
            print("no job has a schedule or a watched URL (--listen runs them when asked over HTTP)", file=sys.stderr)
        return 0
    configure_logging(logging.DEBUG if args.verbose > 0 else logging.INFO)
    if args.once:
        results = scheduler.run_due()
        for hook in scheduler.webhooks:
            hook.close()
        print(f"{len(results)} job(s) were due", file=sys.stderr)
        return 1 if any(not r.ok for r in results) else 0
    listener = None
    if args.listen:
        import threading

        from .triggers import TOKEN_VARIABLE, TriggerServer

        host, _, port = args.listen.rpartition(":")
        if not port.isdigit() or int(port) > 65535:
            print(f"error: --listen {args.listen!r}: say [HOST:]PORT (8765, 127.0.0.1:8765)", file=sys.stderr)
            return 2
        try:
            listener = TriggerServer(scheduler, host.strip("[]") or "127.0.0.1", int(port))
        except ConfigurationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"error: cannot listen on {args.listen}: {exc.strerror or exc}", file=sys.stderr)
            return 1
        threading.Thread(target=listener.serve_forever, name="wintergrab-triggers", daemon=True).start()
        scheduler.listening = True
    print(f"{project.path}: {len(plan)} scheduled job(s), logs in {project.workspace / 'logs'}; Ctrl+C to stop",
          file=sys.stderr)  # fmt: skip
    for job, when in plan:
        print(f"  {job.name}: {job.trigger()}, next {when:%Y-%m-%d %H:%M}" if when else f"  {job.name}: never",
              file=sys.stderr)  # fmt: skip
    if listener is not None:
        print(f"  and when asked: POST {listener.url}/jobs/NAME/run, with the token ({TOKEN_VARIABLE})",
              file=sys.stderr)  # fmt: skip
        if not listener.local:
            print("  warning: other machines can reach it, over plain HTTP: put a TLS proxy in front of it",
                  file=sys.stderr)  # fmt: skip
    try:
        scheduler.loop()
    finally:
        if listener is not None:
            listener.shutdown()
            listener.server_close()
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    from .project import starter_project
    from .runs import DEFAULT_WORKSPACE

    directory = Path(args.directory or ".")
    target = directory / "wintergrab.yaml"
    if target.exists() and not args.force:
        print(f"error: {target} exists (--force to write over it)", file=sys.stderr)
        return 1
    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(starter_project(), encoding="utf-8")
    (directory / DEFAULT_WORKSPACE).mkdir(exist_ok=True)
    print(f"wrote {target} and {directory / DEFAULT_WORKSPACE} (where every run is kept)")
    print("next: edit the jobs, then: wintergrab run   (or: wintergrab schedule)", file=sys.stderr)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    import webbrowser

    from .dashboard import serve
    from .project import find_project
    from .runs import DEFAULT_WORKSPACE

    project = None
    if args.project:
        project = find_project(args.project)
    elif not args.workspace:
        try:
            project = find_project()
        except ConfigurationError as exc:
            if "no project in" not in str(exc):
                print(f"warning: the project here was not read: {exc}", file=sys.stderr)
    workspace = args.workspace or (str(project.workspace) if project is not None else DEFAULT_WORKSPACE)
    server = serve(workspace, project=project, host=args.host, port=args.port)
    print(f"wintergrab dashboard: {server.url}  ({workspace}; Ctrl+C to stop)", file=sys.stderr)
    _print_other_devices(server)
    if args.open:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    import webbrowser

    from .builder import BuilderSession, serve
    from .fetchers import BrowserFetcher, Fetcher

    try:
        if args.browser:
            with BrowserFetcher(timeout=args.timeout) as browser:
                page = browser.get(ensure_scheme(args.url))
        else:
            with Fetcher(timeout=args.timeout) as http:
                page = http.get(ensure_scheme(args.url))
        if not page.ok:
            print(f"error: {args.url} answered {page.status}", file=sys.stderr)
            return 1
        if not page.is_html:
            print(f"error: {args.url} is not an HTML page", file=sys.stderr)
            return 1
        session = BuilderSession(page, args.output, name=args.name)
        server = serve(session, host=args.host, port=args.port)
    except (WintergrabError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wintergrab build: {server.url}  ({page.url} -> {args.output}; Ctrl+C to stop)", file=sys.stderr)
    _print_other_devices(server)
    if args.open:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    from .runs import DEFAULT_WORKSPACE, RunRegistry

    registry = RunRegistry(args.workspace or DEFAULT_WORKSPACE)
    if args.remove:
        run = registry.remove(args.remove)
        print(f"removed {run.id}")
        return 0
    if args.run:
        run = registry.get(args.run)
        print(json.dumps(run.to_dict(), indent=2, default=str) if args.json else run.details())
        return 0
    runs = registry.runs(limit=args.limit)
    if args.json:
        print(json.dumps([r.to_dict() for r in runs], indent=2, default=str))
    elif not runs:
        print(f"no runs in {registry.runs_dir} yet: add --record to a crawl (or create the directory to keep them all)")
    else:
        for run in runs:
            print(run.describe())
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from .runs import DEFAULT_WORKSPACE, RunRegistry, load_spider, replay

    registry = RunRegistry(args.workspace or DEFAULT_WORKSPACE)
    spider = load_spider(args.spider) if args.spider else None
    result = replay(registry.get(args.run), spider, registry=registry, output=args.output, key=args.key)
    if args.json:
        print(json.dumps({"run": result.run.id, "same": result.same, "output": str(result.output),
                          "missing": result.missing, **result.diff.to_dict()}, indent=2, default=str))  # fmt: skip
    else:
        print(result.summary())
        print(f"the replay's items: {result.output}", file=sys.stderr)
    return 0 if result.same else 1


def cmd_fixture(args: argparse.Namespace) -> int:
    import re

    from .extraction.fixtures import FixtureSuite, _show
    from .fetchers import Fetcher
    from .fetchers.response import Headers, Response

    suite = FixtureSuite(args.to)
    expect = {k: _coerce(v) for k, v in _parse_pairs(args.expect, "=", "--expect").items()}
    pages: list[Response] = []
    if args.from_run:
        from .fetchers.cache import HTTPCache
        from .runs import DEFAULT_WORKSPACE, RunRegistry

        run = RunRegistry(args.workspace or DEFAULT_WORKSPACE).get(args.from_run)
        if not run.recorded:
            print(f"error: {run.id} was not recorded (crawl --record): it kept no page", file=sys.stderr)
            return 1
        wanted = re.compile(args.match) if args.match else None
        archive = HTTPCache(run.archive, mode="offline")
        try:
            for entry in archive.entries():
                html = "html" in entry.header_map.get("content-type", "")
                if entry.status != 200 or not html or (wanted is not None and not wanted.search(entry.url)):
                    continue
                pages.append(Response(entry.url, headers=Headers(entry.headers), body=entry.body))
                if len(pages) >= args.limit:
                    break
        finally:
            archive.close()
    elif args.urls:
        with Fetcher(timeout=args.timeout) as fetcher:
            for url in args.urls:
                response = fetcher.get(ensure_scheme(url))
                if response.status != 200:
                    print(f"error: {url} answered {response.status}", file=sys.stderr)
                    return 1
                pages.append(response)
    else:
        print("error: give URLs, or --from-run RUN", file=sys.stderr)
        return 2
    if not pages:
        print("no page to keep (none matched)", file=sys.stderr)
        return 1
    for page in pages:
        fixture = suite.add(page, schema=args.schema, expect=expect, only=args.only, name=args.name,
                            note=args.note or "")  # fmt: skip
        values = ", ".join(f"{k}={_show(v)}" for k, v in fixture.expected.items())
        print(f"{fixture.path}: {values}")
    print("check these values: they are what wintergrab test will expect", file=sys.stderr)
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    from .extraction.fixtures import FixtureSuite

    schema: Any = args.schema
    directory = args.directory
    if args.heal:
        from .extraction.healing import ExtractorVersions

        versions = ExtractorVersions(args.heal)
        schema = schema or versions.schema()  # its active version
        directory = directory or str(versions.directory / "fixtures")
    if not directory:
        print("error: which fixtures? give their directory (or --heal DIR)", file=sys.stderr)
        return 2
    suite = FixtureSuite(directory)
    if not suite.fixtures(args.only):
        print(f"error: no fixture in {directory}", file=sys.stderr)
        return 1
    report = suite.run(schema, names=args.only)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False, default=str))
    else:
        print(report.describe(verbose=args.verbose > 0))
    if args.update and not report.ok:
        changed = suite.update(report)
        print(f"{changed} fixture(s) now expect what was read: review the change", file=sys.stderr)
        return 0
    return 0 if report.ok else 1


def _choice(text: str | None) -> int | None:
    """``--choice``: a candidate's letter (``B``) or number (``2``), as an index."""
    if not text:
        return None
    text = text.strip()
    if text.isdigit() and int(text) >= 1:
        return int(text) - 1
    if len(text) == 1 and text.isalpha():
        return ord(text.upper()) - ord("A")
    raise ConfigurationError(f"--choice is a candidate's letter (A, B...) or number (1, 2...), not {text!r}")


def cmd_heal(args: argparse.Namespace) -> int:
    from .data import Schema
    from .extraction.healing import HealingExtractor

    try:
        extractor = HealingExtractor(args.directory, review=args.review)
        versions = extractor.versions
        if args.rollback:
            version = versions.rollback(reason=args.note or "rolled back by hand", by="human")
            print(f"version {version.number} is active again")
        elif args.activate:
            versions.activate(args.activate, reason=args.note or "activated by hand", by="human")
            print(f"version {args.activate} is active")
        elif args.import_schema:
            version = versions.add(
                Schema.load(args.import_schema), reason=f"imported from {args.import_schema}", by="human"
            )
            print(f"version {version.number} (from {args.import_schema}) is active")
        elif args.diff:
            for line in versions.diff(*args.diff) or ["no difference"]:
                print(line)
        elif args.check:
            failures = versions.check_fixtures()
            count = len(versions.fixtures())
            print(f"{count} fixture(s): " + ("all reproduced" if not failures else f"{len(failures)} value(s) differ"))
            for line in failures:
                print(f"  {line}")
            return 1 if failures else 0
        elif args.log:
            for entry in versions.history():
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.get("at", 0)))
                facts = ", ".join(f"{k}={v}" for k, v in entry.items() if k not in ("at", "event", "candidates"))
                print(f"{when}  {entry.get('event')}: {facts}")
        else:
            print(extractor.status())
    except WintergrabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def load_spider_class(target: str) -> type[Spider]:
    """Load ``path/to/file.py`` or ``path/to/file.py:ClassName``."""
    path_str, _, class_name = target.partition(".py:")
    path = Path(path_str + ".py" if class_name else target)
    if not path.exists():
        raise SystemExit(f"error: no such file: {path}")
    module_name = f"wintergrab_user_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, str(path.parent.resolve()))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    spiders = [
        obj
        for obj in vars(module).values()
        if inspect.isclass(obj) and issubclass(obj, Spider) and obj.__module__ == module_name
    ]
    if class_name:
        for cls in spiders:
            if cls.__name__ == class_name or cls.name == class_name:
                return cls
        raise SystemExit(f"error: {path} has no spider named {class_name!r}")
    if not spiders:
        raise SystemExit(f"error: {path} defines no Spider subclass")
    if len(spiders) > 1:
        names = ", ".join(c.__name__ for c in spiders)
        raise SystemExit(f"error: {path} defines several spiders ({names}); pick one with {path}:ClassName")
    return spiders[0]


def _coerce(value: str) -> Any:
    try:
        return json.loads(value)
    except ValueError:
        return value


def _crawl_settings(args: argparse.Namespace) -> tuple[type[Spider], dict[str, Any]]:
    """The spider class and settings a ``crawl`` command line makes."""
    overrides: dict[str, Any] = {k: _coerce(v) for k, v in _parse_pairs(args.set, "=", "--set").items()}
    is_url = args.target.startswith(("http://", "https://")) or (
        not args.target.endswith(".py") and ".py:" not in args.target and not Path(args.target).exists()
    )
    if is_url:
        start = ensure_scheme(args.target)
        cls: type[Spider] = QuickSpider
        overrides.setdefault("start_urls", [start])
        overrides.setdefault("allowed_domains", [] if args.any_domain else [host_of(start)])
        overrides.update(
            follow=args.follow or (),
            allow=args.allow or (),
            deny=args.deny or (),
            each=args.each or (),
            fields=_fields(args),
            auto=args.auto,
            paginate=args.paginate,
            schema=json.loads(Path(args.schema).read_text(encoding="utf-8")) if args.schema else None,
            extract=args.extract,
            extract_all=args.all,
            container=args.container,
            provenance=args.provenance,
            heal=args.heal,
            review=args.review,
            model=args.model,
            model_url=args.model_url,
        )
        if args.sitemap:
            overrides["sitemap_urls"] = list(args.sitemap)
            if not args.follow and not args.paginate:
                overrides["wander"] = False  # sitemap-driven: crawl what the sitemap lists
    else:
        cls = load_spider_class(args.target)
        if args.follow or args.each or args.field or args.auto or args.paginate or args.schema or args.extract:
            print(
                "warning: --follow/--each/--field/--auto/--paginate/--schema/--extract only apply to URL crawls",
                file=sys.stderr,
            )
        if args.sitemap:
            overrides["sitemap_urls"] = list(args.sitemap)
    option_map = {
        "output": args.output,
        "crawl_dir": args.crawl_dir,
        "concurrency": args.concurrency,
        "concurrency_per_domain": args.per_domain,
        "download_delay": args.delay,
        "max_pages": args.max_pages,
        "max_items": args.max_items,
        "max_depth": args.max_depth,
        "max_requests": args.max_requests,
        "max_bytes": args.max_bytes,
        "max_runtime": args.max_runtime,
        "crawl_order": args.order,
        "event_log": args.events,
        "history": args.history,
        "profile": args.profile,
        "adaptive_fetch": args.fetch_stats or (True if args.auto_browser else None),
        "optimize": args.optimize,
    }
    if args.retry_failed:
        overrides["retry_dead_letters"] = True
    overrides.update({k: v for k, v in option_map.items() if v is not None})
    if args.render_if_missing:
        overrides["render_if_missing"] = list(args.render_if_missing)
        overrides.setdefault("adaptive_fetch", True)
    if args.skip_fresh:
        overrides["skip_fresh"] = True
    if args.history_html:
        overrides["history_html"] = True
    if args.no_autothrottle:
        overrides["autothrottle"] = False
    if args.no_robots:
        overrides["obey_robots_txt"] = False
    if args.browser:
        overrides["use_browser"] = True
    if args.public_only:
        overrides["network_policy"] = "public"
    if args.normalize_urls:
        overrides["url_normalizer"] = True
    if args.block_trackers:
        overrides["resource_filter"] = True
    cache = _cache_options(args)
    if cache:
        overrides["cache"] = cache["cache"]
        if cache["cache_mode"]:
            overrides["cache_mode"] = cache["cache_mode"]
    if args.unique_key:
        overrides["unique_key"] = args.unique_key
    if args.pipeline:
        from .data import Pipeline

        pipeline = Pipeline.load(args.pipeline, allow_imports=args.allow_imports)
        overrides["pipelines"] = [*(overrides.get("pipelines") or getattr(cls, "pipelines", None) or ()), pipeline]
    if args.quality:
        from .extraction.templates import schema_named

        name = schema_named(args.extract).name if getattr(args, "extract", None) else overrides.get("name") or cls.name
        monitor = _quality_monitor(args.quality, name)
        overrides["pipelines"] = [*(overrides.get("pipelines") or getattr(cls, "pipelines", None) or ()), monitor]
    if args.progress is not None:
        overrides["progress"] = args.progress
    rotator = _proxies(args)
    if rotator:
        overrides["proxies"] = rotator
    overrides["log_level"] = "DEBUG" if args.verbose > 0 else ("WARNING" if args.verbose < 0 else "INFO")
    to_stdout = not overrides.get("output") and not getattr(cls, "output", None)
    if to_stdout:
        # JSON Lines on stdout, written after pipelines and de-duplication.
        overrides["output"] = "-"
        overrides["keep_items"] = False
    return cls, overrides


def spider_from_command(command: Sequence[str]) -> tuple[type[Spider], dict[str, Any]]:
    """The spider class and settings of a ``wintergrab crawl ...`` command line, as runs keep it (what
    :func:`wintergrab.runs.replay` crawls with). A healing extractor is replaced by its active version,
    as it is: a replay changes nothing in it."""
    args = build_parser().parse_args(list(command))
    if getattr(args, "command", None) != "crawl":
        raise ConfigurationError(f"only crawl commands can be replayed, not {getattr(args, 'command', None)!r}")
    cls, settings = _crawl_settings(args)
    if settings.get("heal"):
        from .extraction.healing import ExtractorVersions

        versions = ExtractorVersions(settings["heal"])
        settings["extract"] = str(versions.directory / f"v{versions.active_number}.json")
        settings["heal"] = settings["review"] = None
    return cls, settings


def _workspace_settings(args: argparse.Namespace, recipe: dict[str, Any] | None) -> dict[str, Any]:
    """``--record`` / ``--workspace``: settings that keep the run (always, once a workspace exists)."""
    from .runs import DEFAULT_WORKSPACE

    workspace = getattr(args, "workspace", None)
    settings: dict[str, Any] = {}
    if getattr(args, "record", False):
        settings["record"] = True
        settings["run_registry"] = workspace or True
    elif workspace or Path(DEFAULT_WORKSPACE).is_dir():
        settings["run_registry"] = workspace or True  # a workspace keeps every run's record
    if settings and recipe is not None:
        settings["run_recipe"] = recipe
    return settings


def _quality_monitor(path: str, name: str | None) -> Any:
    """``--quality FILE``: measure the items, compare them with FILE (the last run's report: events
    ``quality_degraded`` and ``schema_changed``), then keep this run's report there."""
    from .data.quality import QualityMonitor

    return QualityMonitor(name=name, baseline=path, save_to=path)


def _project_settings(args: argparse.Namespace) -> dict[str, Any]:
    """``--project FILE --job NAME`` (what a project's jobs are run with): its webhooks, the run's label."""
    settings: dict[str, Any] = {}
    if getattr(args, "project", None):
        from .project import Project

        settings["webhooks"] = Project(args.project).webhooks()
    if getattr(args, "job", None):
        settings["run_label"] = args.job
    return settings


def cmd_crawl(args: argparse.Namespace) -> int:
    cls, overrides = _crawl_settings(args)
    overrides.update(_workspace_settings(args, {"command": list(getattr(args, "argv", None) or [])}))
    overrides.update(_project_settings(args))
    try:
        spider = cls(**overrides)
    except TypeError as exc:
        raise SystemExit(f"error: {exc}") from None

    try:
        result = spider.run(resume=not args.fresh)
    except WintergrabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    stats = result.stats
    status = result.status + (f" ({result.limit_reason})" if result.limit_reason else "")
    print(
        f"{status}: {stats.get('pages', 0)} pages, {stats.get('items', 0)} items, "
        f"{stats.get('errors', 0)} errors in {stats.get('elapsed_seconds', 0):.1f}s"
        + (f" -> {redact_url(str(spider.output))}" if spider.output and spider.output != "-" else ""),
        file=sys.stderr,
    )
    _print_failures(result, verbose=args.verbose)
    if result.changes is not None and result.changes.old is not None and args.verbose >= 0:
        print(f"changes since run {result.changes.old.id} ({args.history or spider.history}):", file=sys.stderr)
        print(result.changes.summary(), file=sys.stderr)
    if result.profile is not None and args.profile:
        print(f"site profile saved to {args.profile}", file=sys.stderr)
    rendered = stats.get("adaptive/rendered", 0) + stats.get("adaptive/browser_first", 0)
    if result.fetch_strategy is not None and args.verbose >= 0:
        print(f"adaptive fetching: {rendered} page(s) fetched in a browser", file=sys.stderr)
        if result.fetch_strategy.patterns:
            print(result.fetch_strategy.describe(5), file=sys.stderr)
    incomplete = getattr(spider, "incomplete", 0)
    if incomplete:
        print(f"{incomplete} page(s) had no complete record (a required field was missing)", file=sys.stderr)
    listings = getattr(spider, "listing_pages", 0)
    if listings:
        print(f"{listings} page(s) looked like lists of records, not one: not read as one (--all reads each record)",
              file=sys.stderr)  # fmt: skip
    if result.paused:
        print("paused - run the same command again to resume", file=sys.stderr)
    if result.run_id and args.verbose >= 0:
        replay = f"; replay it with: wintergrab replay {result.run_id}" if overrides.get("record") else ""
        print(f"kept as {result.run_id}{replay}", file=sys.stderr)
    if stats.get("dead_letters") and spider.crawl_dir:
        print(
            f"{stats['dead_letters']} failed request(s) recorded; retry just those with --retry-failed",
            file=sys.stderr,
        )
    return 0


def _print_failures(result: Any, verbose: int) -> None:
    """A short diagnosis of what went wrong (the full report with -v)."""
    if not result.failures or verbose < 0:
        return
    if verbose > 0:
        print("\n" + result.failure_report(), file=sys.stderr)
        return
    print("failures:", file=sys.stderr)
    for diagnosis in result.failures[:3]:
        cause = (
            f"cause: {diagnosis.confirmed_cause}"
            if diagnosis.confirmed_cause
            else f"likely: {diagnosis.likely_cause}"
            if diagnosis.likely_cause
            else "cause unknown"
        )
        print(
            f"  {diagnosis.signature} on {diagnosis.domain}: {diagnosis.affected_urls:,} URL(s); {cause}",
            file=sys.stderr,
        )
    if len(result.failures) > 3:
        print(f"  ... {len(result.failures) - 3} more (-v for the full report)", file=sys.stderr)


# --------------------------------------------------------------------------- #
# shell
# --------------------------------------------------------------------------- #
def cmd_shell(args: argparse.Namespace) -> int:
    import wintergrab as wg

    namespace: dict[str, Any] = {
        "wg": wg,
        "wintergrab": wg,
        "get": wg.get,
        "render": wg.render,
        "parse": wg.parse,
        "Field": wg.Field,
    }
    banner = [f"wintergrab {__version__} shell", "  wg / get(url) / render(url) / parse(html) / Field(...)"]
    if args.url:
        page = wg.render(args.url) if args.browser else wg.get(args.url)
        namespace["page"] = page
        banner.append(f"  page = {page!r}  -> try page.css('title::text').get()")
    banner_text = "\n".join(banner)
    try:
        from IPython import start_ipython  # type: ignore[import-not-found]

        print(banner_text)
        start_ipython(argv=[], user_ns=namespace)
    except ImportError:
        import code

        code.interact(banner=banner_text, local=namespace, exitmsg="")
    return 0


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def cmd_benchmark(args: argparse.Namespace) -> int:
    from .bench import SCENARIOS, describe, run_benchmark

    scenarios = list(args.scenario or [s for s in SCENARIOS if s != "browser" or args.browser])
    if args.browser and "browser" not in scenarios:
        scenarios.append("browser")
    if args.quick:
        args.pages, args.items, args.rounds = min(args.pages, 20), min(args.items, 200), min(args.rounds, 30)
    for name, value in (("--pages", args.pages), ("--items", args.items), ("--concurrency", args.concurrency),
                        ("--rounds", args.rounds)):  # fmt: skip
        if value < 1:
            print(f"error: {name} must be at least 1", file=sys.stderr)
            return 2
    if args.latency < 0:
        print("error: --latency must not be negative", file=sys.stderr)
        return 2

    def progress(name: str, result: dict[str, Any]) -> None:
        if args.verbose >= 0:
            print(f"  {name}: {'failed: ' + result['error'] if 'error' in result else 'done'}", file=sys.stderr)

    if args.verbose >= 0:
        print(f"measuring {', '.join(scenarios)} on this machine...", file=sys.stderr)
    report = run_benchmark(
        scenarios=scenarios,
        pages=args.pages,
        items=args.items,
        latency=args.latency / 1000,
        concurrency=args.concurrency,
        rounds=args.rounds,
        startup_runs=1 if args.quick else 5,
        stores=args.store or (),
        on_result=progress,
    )
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2) if args.json else describe(report))
    return 1 if any("error" in result for result in report["results"].values()) else 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report what is installed and what each optional feature needs."""
    import platform

    from .adaptive.storage import default_storage_path
    from .fetchers.browser import _discover_chromium

    def version(module: str) -> str | None:
        try:
            from importlib.metadata import version as dist_version

            return dist_version(module)
        except Exception:
            return None

    rows: list[tuple[str, str, str]] = []

    def check(name: str, ok: bool, detail: str, hint: str = "") -> None:
        rows.append(("ok " if ok else "-- ", name, detail if ok else f"{detail}  ->  {hint}"))

    check("python", sys.version_info >= (3, 10), platform.python_version(), "Python 3.10+ is required")
    for dist in ("curl_cffi", "lxml", "cssselect"):
        v = version(dist)
        check(dist, v is not None, v or "missing", f"pip install {dist}")
    pw = version("playwright")
    check("playwright", pw is not None, pw or "not installed", 'pip install "wintergrab[browser]"')
    if pw:
        found = _discover_chromium()
        check(
            "chromium",
            bool(found) or bool(os.environ.get("WINTERGRAB_BROWSER_PATH")),
            found[0] if found else "no browser binary found",
            "playwright install chromium",
        )
    for dist, why in (("uvloop", "faster event loop"), ("orjson", "faster JSON output")):
        v = version(dist)
        check(dist, v is not None, v or f"not installed ({why})", 'pip install "wintergrab[speed]"')
    v = version("pyyaml")
    check("pyyaml", v is not None, v or "not installed (YAML schemas and pipelines)", 'pip install "wintergrab[yaml]"')
    v = version("pypdf")
    check("pypdf", v is not None, v or "not installed (reading PDFs)", 'pip install "wintergrab[pdf]"')
    for dist, what, extra in (
        ("pyarrow", "Parquet files", "parquet"),
        ("openpyxl", "Excel files", "xlsx"),
        ("psycopg", "PostgreSQL output", "postgres"),
        ("pymysql", "MySQL and MariaDB output", "mysql"),
        ("pymongo", "MongoDB output", "mongodb"),
        ("s3fs", "S3 output", "s3"),
    ):
        v = version(dist)
        check(dist, v is not None, v or f"not installed ({what})", f'pip install "wintergrab[{extra}]"')
    check("adaptive db", True, str(default_storage_path()))
    width = max(len(r[1]) for r in rows)
    for status, name, detail in rows:
        print(f"{status} {name.ljust(width)}  {detail}")
    return 0 if all(r[0].strip() == "ok" for r in rows[:4]) else 1


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def other_devices_url(host: str, port: int) -> str | None:
    """Where other devices (a phone on this network) reach a server listening on every address
    (``0.0.0.0``): this machine's address on its network; ``None`` for other hosts, or with no network."""
    import socket

    if host not in ("0.0.0.0", "::", ""):
        return None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # a documentation address: nothing is sent, the route picks the interface
            address = str(probe.getsockname()[0])
    except OSError:
        return None
    return None if address.startswith("127.") or address == "0.0.0.0" else f"http://{address}:{port}/"


def _print_other_devices(server: Any) -> None:
    host, port = server.server_address[:2]
    url = other_devices_url(host.decode() if isinstance(host, bytes) else str(host), port)
    if url:
        print(f"  from other devices on this network (a phone): {url}", file=sys.stderr)


def _write_records(records: Iterable[Any], output: str | None) -> int:
    from .spider.exporters import open_exporter

    exporter = open_exporter(output or "-")
    count = 0
    try:
        for record in records:
            exporter.write(record)
            count += 1
    finally:
        exporter.close()
    return count


def cmd_data_run(args: argparse.Namespace) -> int:
    from .data import Pipeline
    from .data.io import read_records

    pipeline = Pipeline.load(args.pipeline, allow_imports=args.allow_imports)
    records = read_records(args.input, limit=args.limit)
    if pipeline.is_async:
        written = _write_records(asyncio.run(pipeline.arun(records)), args.output)
    else:
        written = _write_records(pipeline.stream(records), args.output)
    if args.verbose >= 0:
        print(pipeline.describe(), file=sys.stderr)
        if args.output:
            print(f"wrote {written:,} record(s) to {args.output}", file=sys.stderr)
    return 0


def cmd_data_validate(args: argparse.Namespace) -> int:
    from .data import Normalize, Pipeline, Validate
    from .data.io import read_records
    from .extraction.templates import schema_named

    schema = schema_named(args.schema)
    stages: list[Any] = []
    if not args.no_normalize:
        stages.append(Normalize(schema, country=args.country, currency=args.currency, dayfirst=args.dayfirst))
    validate = Validate(schema, on_error="drop", rejects=args.rejects)
    stages.append(validate)
    pipeline = Pipeline(stages, name=f"validate {schema.name}")
    valid = pipeline.stream(read_records(args.input, limit=args.limit))
    if args.output:
        _write_records(valid, args.output)
    else:
        for _ in valid:
            pass
    total, invalid = validate.stats["in"], validate.stats["invalid"]
    if args.verbose >= 0:
        print(f"{total:,} record(s): {total - invalid:,} valid, {invalid:,} invalid", file=sys.stderr)
        for field, code, severity, count in validate.summary(args.max_issues):
            print(f"  {count:>8,}  {severity:<7}  {field or '(record)'}: {code}", file=sys.stderr)
        if args.rejects and invalid:
            print(f"invalid records and their issues: {args.rejects}", file=sys.stderr)
    return 1 if invalid else 0


def cmd_data_infer(args: argparse.Namespace) -> int:
    from .data import TypeGuess, infer_schema
    from .data.io import read_records

    records = list(read_records(args.input, limit=args.sample))
    if not records:
        print("error: no records to learn from", file=sys.stderr)
        return 1
    name = args.name or (Path(args.input).stem if args.input != "-" else "records")
    guesses: list[TypeGuess] = []
    schema = infer_schema(records, name=name, sample=args.sample, guesses=guesses)
    if args.explain:
        for guess in guesses:
            print(
                f"{guess.field}: {guess.type} ({guess.share:.0%} of {guess.samples:,} value(s) fit; "
                f"present in {guess.present:,}/{guess.records:,} record(s))",
                file=sys.stderr,
            )
    if args.output:
        schema.save(args.output)
        print(f"saved the schema to {args.output}", file=sys.stderr)
    else:
        print(json.dumps(schema.to_dict(), indent=2, ensure_ascii=False))
    return 0


def cmd_data_entities(args: argparse.Namespace) -> int:
    from .data.entities import EntityResolver
    from .data.io import read_records

    attributes: dict[str, str] = {}
    for raw in args.attribute or ():
        name, _, field = raw.partition("=")
        attributes[name.strip()] = (field or name).strip()
    try:
        resolver = EntityResolver(args.kind, merge_threshold=args.merge, review_threshold=args.review)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    records = list(read_records(args.input, limit=args.limit))
    source = args.source or ("url" if any("url" in r for r in records[:100] if isinstance(r, dict)) else None)
    mentions = resolver.add_records(records, args.field, source_field=source, attributes=attributes)
    result = resolver.resolve()
    if args.output:
        _write_records(result.records(), args.output)
    if args.review_output:
        _write_records((match.to_dict() for match in result.review), args.review_output)
    if args.annotate:
        prefix = args.field.replace(".", "_")

        def annotated() -> Iterable[dict[str, Any]]:
            for record, mention in zip(records, mentions, strict=True):
                if mention is not None and isinstance(record, dict):
                    entity = result.entity_of(mention)
                    record = {**record, f"{prefix}_entity": entity.id, f"{prefix}_canonical": entity.name}
                yield record

        _write_records(annotated(), args.annotate)
    if args.verbose >= 0:
        print(result.summary(), file=sys.stderr)
        for entity in result.entities[: args.show]:
            others = len(entity.aliases) - 1
            spelled = f", {others} other spelling(s)" if others else ""
            print(
                f"  {len(entity.mentions):>6,}  {entity.name}  ({entity.id}{spelled}, "
                f"confidence {entity.confidence:.2f})",
                file=sys.stderr,
            )
        if result.review:
            print("to review:", file=sys.stderr)
        for match in result.review[: args.show]:
            note = f" [{match.note}]" if match.note else ""
            print(
                f"  {match.score:.2f}  {match.a.name!r} ~ {match.b.name!r}: {'; '.join(match.reasons)}{note}",
                file=sys.stderr,
            )
        for label, path in (
            ("entities", args.output),
            ("pairs to review", args.review_output),
            ("records", args.annotate),
        ):
            if path:
                print(f"wrote the {label} to {path}", file=sys.stderr)
    return 0


def cmd_data_analyze(args: argparse.Namespace) -> int:
    from collections import Counter

    from .data import Analyze, Classify, Pipeline
    from .data.io import read_records

    try:
        features = [a.strip() for a in args.add.split(",") if a.strip()] if args.add else None
        analyze = (
            Analyze(args.field, prefix=args.prefix)
            if features is None
            else Analyze(args.field, add=features, prefix=args.prefix)
        )
        stages: list[Any] = [analyze]
        if args.model:
            categories = [c.strip() for c in args.categories.split(",") if c.strip()] if args.categories else None
            stages.append(Classify(args.field, _model(args), categories=categories, prefix=args.prefix))
    except WintergrabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    pipeline = Pipeline(stages, name="analyze")
    records = pipeline.run(read_records(args.input, limit=args.limit))
    _write_records(records, args.output)
    if args.verbose >= 0:
        languages = Counter(r.get(args.prefix + "language") for r in records)
        shown = ", ".join(f"{language or 'unknown'} {count:,}" for language, count in languages.most_common(8))
        print(f"{len(records):,} record(s); languages: {shown}", file=sys.stderr)
        keywords = Counter(k for r in records for k in r.get(args.prefix + "keywords") or ())
        if keywords:
            print("keywords: " + ", ".join(f"{k} ({n})" for k, n in keywords.most_common(10)), file=sys.stderr)
        sentiments = Counter(r.get(args.prefix + "sentiment") for r in records if r.get(args.prefix + "sentiment"))
        if sentiments:
            print("sentiment: " + ", ".join(f"{s} {n:,}" for s, n in sentiments.most_common()), file=sys.stderr)
        errors = sum(stage.stats["errors"] for stage in pipeline)
        if errors:
            print(f"the model failed on {errors:,} record(s): they are kept without its labels", file=sys.stderr)
    return 0


def cmd_data_places(args: argparse.Namespace) -> int:
    import re
    from collections import Counter

    from .data.io import read_records
    from .data.normalize import normalize_country, parse_coordinates, place_meanings
    from .data.places import DEFAULT_PARTS, PLACE_FIELDS, PLACE_PARTS, distance_km, group_records, places_of

    fields: dict[str, str] = {}
    for spec in args.field or ():
        part, _, name = spec.partition("=")
        if part not in PLACE_FIELDS or not name:
            print(f"error: --field {spec}: PART=NAME, with PART one of {', '.join(PLACE_FIELDS)}", file=sys.stderr)
            return 2
        fields[part] = name
    add = [a.strip() for a in args.add.split(",") if a.strip()] if args.add else list(DEFAULT_PARTS)
    unknown = [a for a in add if a not in PLACE_PARTS]
    if unknown:
        print(f"error: --add: unknown part(s) {', '.join(unknown)}; known: {', '.join(PLACE_PARTS)}", file=sys.stderr)
        return 2
    if args.country and normalize_country(args.country) is None:
        print(f"error: --country {args.country}: not a country", file=sys.stderr)
        return 2
    wanted: list[tuple[str, str]] = []  # (part, value)
    for text in args.within_place or ():
        meanings = place_meanings(text.strip())
        if len(text.strip()) == 2 and normalize_country(text):
            wanted.append(("country", normalize_country(text) or ""))
        elif len(meanings) == 1:
            wanted.append(("region" if "-" in meanings[0] else "country", meanings[0]))
        elif meanings:
            print(f"error: --in {text}: it could be {' or '.join(meanings)}; write one of those", file=sys.stderr)
            return 2
        elif re.fullmatch(r"[A-Za-z]{2}-[A-Za-z0-9]{1,3}", text.strip()):
            wanted.append(("region", text.strip().upper()))
        else:
            wanted.append(("city", " ".join(text.casefold().split())))
    center, within = None, float(args.within or 0)
    if args.near or args.within is not None:
        center = parse_coordinates(args.near) if args.near else None
        if center is None or within <= 0:
            print(
                "error: --near LAT,LON and --within KM go together (a point, and a distance above 0)", file=sys.stderr
            )
            return 2

    records = list(read_records(args.input, limit=args.limit))
    places, settled = places_of(records, country=args.country, fields=fields)
    kept_records, kept_places = [], []
    no_point = 0
    for record, place in zip(records, places, strict=True):
        if wanted and not any(
            (place.key("city")[0] if part == "city" and place.city else getattr(place, part)) == value
            for part, value in wanted
        ):
            continue
        if args.remote and not place.remote:
            continue
        if center is not None:
            distance = distance_km(place, center)
            if distance is None:
                no_point += 1
                continue
            if distance > within:
                continue
            record[args.prefix + "distance_km"] = distance
        values = place.to_dict()
        for part in add:
            key = args.prefix + part
            if values[part] is not None or record.get(key) in (None, "", [], {}):
                record[key] = values[part]
        kept_records.append(record)
        kept_places.append(place)

    if args.verbose >= 0:
        known = sum(1 for p in places if p.known)
        countries = len({p.country for p in places if p.country})
        remote = sum(1 for p in places if p.remote)
        line = f"{len(records):,} record(s): {known:,} with a place"
        if countries:
            line += f" ({countries:,} countr{'y' if countries == 1 else 'ies'})"
        if remote:
            line += f", {remote:,} remote"
        if len(kept_records) != len(records):
            line += f"; {len(kept_records):,} kept"
        print(line, file=sys.stderr)
        unsure = Counter(p.unsure for p in places if p.unsure)
        if unsure:
            codes = ", ".join(f"{code!r} ({n:,} record(s))" for code, n in unsure.most_common(5))
            print(f"not read: {codes}: each could name several places; --country COUNTRY reads them in that country",
                  file=sys.stderr)  # fmt: skip
        if settled:
            count = sum(1 for p in places if p.sources.get("country") == "other records")
            print(f"{count:,} record(s) naming a code of several places were read in {settled}, the country the "
                  "other records name most (--country to choose)", file=sys.stderr)  # fmt: skip
        if no_point:
            print(f"{no_point:,} record(s) left out by --near: they state no coordinates", file=sys.stderr)

    if args.by:
        groups = group_records(kept_records, args.by, stats=args.stats or (), places=kept_places)
        if args.json:
            print(json.dumps([g.to_dict() for g in groups], ensure_ascii=False, indent=2))
        else:
            _print_groups(groups, args.by, args.stats or (), args.top)
        if args.output:
            _write_records(kept_records, args.output)
        return 0
    _write_records(kept_records, args.output)
    return 0


def _print_groups(groups: list[Any], by: str, stats: Sequence[str], top: int) -> None:
    """A table of groups: label, records, share, and the median of each stats field."""
    rows = []
    for group in groups[:top]:
        row = [group.label, f"{group.count:,}", f"{group.share:.0%}"]
        for name in stats:
            found = [(label, s) for label, s in group.stats.items() if label == name or label.startswith(f"{name} (")]
            row.append(
                "; ".join(f"{s['median']:,}{' ' + s['currency'] if s.get('currency') else ''}" for _, s in found)
            )
        rows.append(row)
    header = [by, "records", "share", *(f"{name} (median)" for name in stats)]
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        cells = [str(cell).ljust(widths[0]) if i == 0 else str(cell).rjust(widths[i]) for i, cell in enumerate(row)]
        print("  ".join(cells).rstrip())
    if len(groups) > top:
        rest = sum(g.count for g in groups[top:])
        print(f"... and {len(groups) - top:,} more group(s) of {rest:,} record(s) (--top N, --json)")


def cmd_data_graph(args: argparse.Namespace) -> int:
    from .data.graph import RELATIONS, KnowledgeGraph, Relation
    from .data.io import read_records

    try:
        extra = [Relation.parse(text) for text in args.relation or ()]
        graph = KnowledgeGraph(merge_threshold=args.merge, review_threshold=args.review)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for spec in args.input:
        kind, _, path = spec.partition("=") if "=" in spec and not Path(spec).exists() else ("", "", spec)
        records = list(read_records(path, limit=args.limit))
        kind = kind or args.kind or _record_kind(records)
        if not kind:
            print(f"error: what are the records of {path}? Say it: --kind KIND, or KIND={path}", file=sys.stderr)
            return 2
        relations = extra if args.only else [*RELATIONS.get(kind, ()), *extra]
        if not relations:
            print(f"warning: no relation for {kind} records: add --relation FIELD=RELATION:KIND", file=sys.stderr)
        count = graph.add_records(records, kind, relations=relations)
        if args.verbose >= 0:
            print(f"{path}: {count:,} {kind} record(s)", file=sys.stderr)
    graph.build()
    if args.output:
        try:
            graph.save(args.output)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.verbose >= 0:
        print(graph.describe(top=args.show), file=sys.stderr)
        if args.output:
            print(f"wrote the graph to {args.output}", file=sys.stderr)
    return 0


def _record_kind(records: list[dict[str, Any]]) -> str | None:
    """What records are, from the extractor they name (``_provenance.extractor``: ``"product@1"``)."""
    for record in records[:20]:
        provenance = record.get("_provenance") if isinstance(record, dict) else None
        extractor = provenance.get("extractor") if isinstance(provenance, dict) else None
        if isinstance(extractor, str) and extractor:
            return extractor.split("@")[0]
    return None


def _dataset(ref: str, limit: int | None = None) -> tuple[list[dict[str, Any]], list[str] | None, str]:
    """Records from a file, a versions directory (its latest version) or DIR@VERSION; the
    versions' key; a label."""
    from .data.io import read_records
    from .data.versions import DatasetVersions

    base, _, version = ref.rpartition("@") if "@" in ref else (ref, "", "")
    directory = Path(base or ref)
    if (directory / DatasetVersions.MANIFEST).is_file():
        versions = DatasetVersions(directory)
        found = versions.get(version or "latest")
        return versions.load(found.number)[:limit], versions.key, f"{directory}@{found.name}"
    return list(read_records(ref, limit=limit)), None, ref


def cmd_data_diff(args: argparse.Namespace) -> int:
    from .data.versions import diff_records

    try:
        old, old_key, old_label = _dataset(args.old)
        new, new_key, new_label = _dataset(args.new)
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    key = args.key or old_key or new_key
    if key is None and any(isinstance(r, dict) and "url" in r for r in [*old[:100], *new[:100]]):
        key = ["url"]
    diff = diff_records(old, new, key, ignore=args.ignore or (), private=args.private)
    if args.output:
        _write_records(diff.rows(), args.output)
    if args.json:
        print(json.dumps({"old": old_label, "new": new_label, "key": key, **diff.to_dict()}, indent=2, default=str))
    else:
        matched = f"by {', '.join(key)}" if key else "by content (no --key)"
        print(f"{old_label} -> {new_label}, records matched {matched}")
        print(diff.describe(args.fields))
        if diff.stats.get("duplicate_keys"):
            print(f"note: {diff.stats['duplicate_keys']:,} record(s) repeated a key; the last one was compared")
        if key and diff.stats.get("unkeyed"):
            print(f"note: {diff.stats['unkeyed']:,} record(s) had no key and were matched by content")
    if args.output and args.verbose >= 0:
        print(f"wrote the differences to {args.output}", file=sys.stderr)
    return 1 if args.exit_code and diff else 0


def cmd_data_commit(args: argparse.Namespace) -> int:
    from .data.io import read_records
    from .data.versions import DatasetVersions

    try:
        versions = DatasetVersions(args.directory)
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    latest = versions.latest
    key = args.key or versions.key
    if key is None:
        print("error: say which field(s) identify a record with --key (remembered for later versions)", file=sys.stderr)
        return 2
    version = versions.commit(read_records(args.input), message=args.message, key=key, force=args.force)
    if latest is not None and version.number == latest.number:
        print(f"no changes since {version.name}: nothing saved (--force to save anyway)")
        return 0
    changes = ""
    if version.changes:
        c = version.changes
        changes = (
            f": +{c['added']:,} added, -{c['removed']:,} removed, ~{c['changed']:,} changed, "
            f"{c['unchanged']:,} unchanged"
        )
    print(f"saved {version.name} ({version.records:,} records){changes}")
    return 0


def cmd_data_log(args: argparse.Namespace) -> int:
    from .data.versions import DatasetVersions

    directory = Path(args.directory)
    if not (directory / DatasetVersions.MANIFEST).is_file():
        print(f"error: {directory} holds no versions (save one with `wintergrab data commit`)", file=sys.stderr)
        return 2
    try:
        versions = DatasetVersions(directory)
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    key = f", key {', '.join(versions.key)}" if versions.key else ""
    print(f"{directory}: {len(versions.versions)} version(s){key}")
    for version in reversed(versions.versions):
        print(f"  {version.describe()}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    from .history import PageHistory

    if not Path(args.file).is_file():
        print(f"error: no history file at {args.file}", file=sys.stderr)
        return 2
    try:
        history = PageHistory(args.file)
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        if args.url:
            return _history_url(history, args)
        if args.due:
            now = time.time()
            due = [url for url in history.urls() if history.due(url, now)]
            if args.json:
                print(json.dumps({"due": len(due), "urls": due[: args.show]}, indent=2))
            else:
                print(f"{len(due):,} page(s) due for another look")
                for url in due[: args.show]:
                    print(f"  {url}")
            return 0
        runs = history.runs(args.name)
        if not runs:
            print("no runs recorded" + (f" for {args.name}" if args.name else ""))
            return 0
        report = history.compare(*args.compare) if args.compare else history.compare(name=args.name)
        if args.json:
            print(json.dumps({"runs": [vars(run) for run in runs], "changes": report.to_dict()}, indent=2, default=str))
            return 0
        if not args.compare:
            for run in runs[-args.show :]:
                print(run.describe())
            print()
        print(report.describe(args.show))
        return 0
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        history.close()


def _history_url(history: Any, args: argparse.Namespace) -> int:
    from .history import compare_snapshots

    snapshots = history.snapshots(args.url)
    if not snapshots:
        print(f"error: {args.url} is not in the history", file=sys.stderr)
        return 2
    info = history.freshness(args.url)
    if args.json:
        data = {"freshness": info.to_dict(), "snapshots": [{"run": run, **snap.to_dict()} for run, snap in snapshots]}
        print(json.dumps(data, indent=2, default=str))
        return 0
    previous = None
    for run, snap in snapshots:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(snap.fetched_at))
        facts = [f"status {snap.status}"]
        if snap.price is not None:
            facts.append(f"price {snap.price:g}{' ' + snap.currency if snap.currency else ''}")
        if snap.availability:
            facts.append(snap.availability)
        change = compare_snapshots(previous, snap) if previous is not None else None
        facts.append(
            "changed: " + ", ".join(change.kinds) if change else ("first seen" if previous is None else "unchanged")
        )
        print(f"run {run:<4} {when}  " + "; ".join(facts))
        previous = snap
    rate = f"{info.rate:.2f} change(s) per day" if info.rate is not None else "change rate unknown (seen once)"
    print(
        f"{info.observations} fetch(es), {info.changes} with changes; {rate}; "
        f"look again after {info.recrawl_after / 3600:.1f} h; still unchanged now with probability {info.fresh:.0%}"
    )
    return 0


def cmd_data_quality(args: argparse.Namespace) -> int:
    from .data import QualityMonitor, QualityReport
    from .data.io import read_records
    from .extraction.templates import schema_named

    schema = schema_named(args.schema) if args.schema else None
    name = Path(args.input).stem if args.input != "-" else "records"
    monitor = QualityMonitor(schema, name=name, key=args.key or None, save_to=None)
    for record in read_records(args.input, limit=args.limit):
        monitor.observe(record)
    report = monitor.report()
    comparison = []
    if args.baseline:
        try:
            baseline = QualityReport.load(args.baseline)
        except (OSError, ValueError) as exc:
            print(f"error: cannot read the baseline {args.baseline}: {exc}", file=sys.stderr)
            return 2
        comparison = report.compare(baseline)
    if args.save:
        report.save(args.save)
    if args.json:
        data = report.to_dict()
        if args.baseline:
            data["comparison"] = [issue.to_dict() for issue in comparison]
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(report.describe())
        if args.baseline:
            print(f"compared with {args.baseline}:" if comparison else f"no degradation compared with {args.baseline}")
            for issue in comparison:
                print(f"  {issue}")
    return 1 if any(issue.severity == "error" for issue in comparison) else 0


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def _add_network_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--proxy", action="append", metavar="URL", help="proxy to use (repeat to rotate several)")
    p.add_argument("--proxy-file", metavar="FILE", help="file with one proxy per line")
    p.add_argument("--browser", "-b", action="store_true", help="use a headless browser (renders JavaScript)")
    p.add_argument(
        "--public-only",
        action="store_true",
        help="refuse private, loopback and cloud-metadata addresses (SSRF protection for untrusted sites)",
    )
    p.add_argument(
        "--block-trackers", action="store_true", help="(browser) also block ads, analytics and tracker requests"
    )


def _add_cache_options(p: argparse.ArgumentParser) -> None:
    group = p.add_argument_group("caching")
    group.add_argument("--cache", action="store_true", help="cache responses on disk (in .wintergrab-cache)")
    group.add_argument("--cache-dir", metavar="DIR", help="cache directory (implies --cache)")
    group.add_argument(
        "--cache-mode", choices=["revalidate", "prefer", "offline", "refresh"], help="how to use the cache"
    )
    group.add_argument("--offline", action="store_true", help="replay from the cache only; never touch the network")


def _add_typed_extract_options(p: Any, *, schema_flag: bool = True) -> None:
    if schema_flag:
        p.add_argument(
            "--extract",
            metavar="SCHEMA",
            help="extract typed records described by a data schema file (see docs/extraction.md)",
        )
    p.add_argument("--all", action="store_true", help="(--extract) every record of a listing page")
    p.add_argument("--container", metavar="SELECTOR", help="(--extract) the elements holding one record each")
    p.add_argument("--provenance", action="store_true", help="(--extract) add where each value came from")
    p.add_argument(
        "--heal",
        metavar="DIR",
        help="(--extract) keep versions of the extractor in DIR and repair its selectors when the site changes",
    )
    p.add_argument("--review", metavar="FILE", help="(--heal) queue what needs a person in FILE (wintergrab review)")
    p.add_argument(
        "--model",
        metavar="PROVIDER:NAME",
        help="(--extract) ask a language model for the fields the page's own data does not give: openai:NAME, "
        "anthropic:NAME, ollama:NAME (see docs/models.md)",
    )
    p.add_argument("--model-url", metavar="URL", help="(--model) where the model's API is (a server of your own)")


def _model(args: argparse.Namespace) -> Any:
    """``--model PROVIDER:NAME [--model-url URL]``: a model provider, or ``None``."""
    spec = getattr(args, "model", None)
    if not spec:
        return None
    from .models import load_model

    return load_model(spec, base_url=getattr(args, "model_url", None))


def _extractor(args: argparse.Namespace) -> Any:
    if not getattr(args, "extract", None) and not getattr(args, "heal", None):
        return None
    if getattr(args, "heal", None):
        from .extraction.healing import HealingExtractor

        return HealingExtractor(args.heal, args.extract, review=args.review, provenance=args.provenance,
                                model=_model(args))  # fmt: skip
    from .extraction import Extractor

    return Extractor(
        args.extract, provenance=args.provenance, model=_model(args), vision=getattr(args, "vision", False)
    )


def _add_extract_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--each", action="append", metavar="SELECTOR", help="make one record per element matched by this selector"
    )
    p.add_argument(
        "--field",
        action="append",
        metavar="NAME=SELECTOR",
        help="a field of each record, e.g. price=.price::text (repeatable)",
    )


def _add_crawl_options(c: argparse.ArgumentParser, *, extract_command: bool = False) -> None:
    """The options of ``crawl`` (and of ``extract``, where ``--schema`` names the records to extract)."""
    c.add_argument(
        "-o", "--output", metavar="FILE", help="save items to .jsonl/.json/.csv (default: JSON lines on stdout)"
    )
    c.add_argument("--crawl-dir", metavar="DIR", help="directory for pause/resume state")
    c.add_argument(
        "--history", metavar="FILE", help="record page fingerprints here and report what changed since the last run"
    )
    c.add_argument("--history-html", action="store_true", help="keep every page's HTML in the history too")
    c.add_argument(
        "--auto-browser",
        action="store_true",
        help="fetch over HTTP, and in a browser the pages that need JavaScript, learning which URL patterns do",
    )
    c.add_argument(
        "--fetch-stats",
        metavar="FILE",
        help="(implies --auto-browser) keep what was learned about each URL pattern here for the next crawls",
    )
    c.add_argument(
        "--render-if-missing",
        action="append",
        metavar="SELECTOR",
        help="(implies --auto-browser) a page where this finds nothing is fetched again in a browser",
    )
    c.add_argument(
        "--profile",
        metavar="FILE",
        help="save the site's profile and topology (sections, dead ends, orphans...) here as JSON",
    )
    c.add_argument(
        "--record",
        action="store_true",
        help="keep this run's pages, items and events, to replay it without the network (wintergrab replay)",
    )
    c.add_argument(
        "--workspace",
        metavar="DIR",
        help="where runs are kept (default .wintergrab; once it exists, every crawl's run is kept)",
    )
    c.add_argument("--project", metavar="FILE", help="(set by wintergrab run/schedule) post events to its webhooks")
    c.add_argument("--job", metavar="NAME", help="(set by wintergrab run/schedule) the job's name, kept with the run")
    c.add_argument(
        "--optimize",
        nargs="?",
        const=True,
        metavar="FILE",
        help="learn which URL patterns give items: fetch those first, skip the ones that give nothing, drop "
        "parameters that change nothing (FILE: keep what was learned for the next crawls)",
    )
    c.add_argument(
        "--skip-fresh", action="store_true", help="with --history: skip pages that have probably not changed"
    )
    c.add_argument("--fresh", action="store_true", help="ignore saved state and start over")
    c.add_argument("--concurrency", type=int, metavar="N", help="max requests in flight")
    c.add_argument("--per-domain", type=int, metavar="N", help="max requests in flight per domain")
    c.add_argument("--delay", type=float, metavar="SEC", help="minimum delay between requests to a domain")
    c.add_argument("--no-autothrottle", action="store_true", help="fixed speed instead of adaptive")
    c.add_argument("--max-pages", type=int, metavar="N")
    c.add_argument("--max-items", type=int, metavar="N")
    c.add_argument("--max-depth", type=int, metavar="N")
    c.add_argument("--max-requests", type=int, metavar="N", help="budget: requests sent, retries included")
    c.add_argument("--max-bytes", type=int, metavar="N", help="budget: bytes downloaded")
    c.add_argument("--max-runtime", type=float, metavar="SEC", help="budget: seconds of crawling (across resumes)")
    c.add_argument("--order", choices=["bfs", "dfs"], help="breadth-first (default) or depth-first")
    c.add_argument("--events", metavar="FILE", help="write structured events (JSON lines) to FILE")
    c.add_argument(
        "--retry-failed", action="store_true", help="queue the requests an earlier run gave up on (needs --crawl-dir)"
    )
    c.add_argument("--no-robots", action="store_true", help="ignore robots.txt")
    c.add_argument("-s", "--set", action="append", metavar="NAME=VALUE", help="override a spider attribute")
    _add_network_options(c)
    c.add_argument("--follow", action="append", metavar="SELECTOR", help="(URL mode) links/containers to follow")
    c.add_argument("--allow", action="append", metavar="REGEX", help="(URL mode) only follow matching URLs")
    c.add_argument("--deny", action="append", metavar="REGEX", help="(URL mode) never follow matching URLs")
    c.add_argument("--any-domain", action="store_true", help="(URL mode) follow links to other domains too")
    c.add_argument("--paginate", action="store_true", help="(URL mode) follow next-page links (auto-detected)")
    c.add_argument("--auto", action="store_true", help="(URL mode) extract repeating records automatically")
    if extract_command:
        c.add_argument(
            "--schema",
            dest="extract",
            required=True,
            metavar="SCHEMA",
            help="the records to extract: a data schema file, or a template (product, article, job...)",
        )
        _add_typed_extract_options(c, schema_flag=False)
        c.set_defaults(schema=None)
    else:
        c.add_argument("--schema", metavar="FILE", help="(URL mode) extract with a schema saved by get --save-schema")
        _add_typed_extract_options(c)
    c.add_argument("--sitemap", action="append", metavar="URL", help="take pages from a sitemap or robots.txt")
    c.add_argument("--unique-key", metavar="FIELD", help="drop duplicate items (and upsert into .sqlite output)")
    c.add_argument(
        "--normalize-urls",
        action="store_true",
        help="drop tracking parameters, session ids and fragments from URLs before queueing them",
    )
    c.add_argument("--pipeline", metavar="FILE", help="clean, validate and filter items with a pipeline file")
    c.add_argument(
        "--quality",
        metavar="FILE",
        help="measure the items' quality and compare it with the last run's, kept in FILE (events quality_degraded, "
        "schema_changed)",
    )
    c.add_argument(
        "--allow-imports", action="store_true", help="let the --pipeline file call Python functions it names"
    )
    c.add_argument(
        "--progress",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="live status line (default: on a terminal)",
    )
    _add_cache_options(c)
    _add_extract_options(c)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wintergrab",
        description="Fetch pages, extract data and crawl sites.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"wintergrab {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="more logging")
    parser.add_argument("-q", "--quiet", action="store_const", const=-1, dest="verbose", help="less output")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    g = sub.add_parser(
        "get",
        help="fetch page(s) and print or save content",
        description="Fetch one or more pages and print them (Markdown by default), "
        "selected elements, or structured records.",
        epilog=EPILOG_GET,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g.add_argument("urls", nargs="+", metavar="URL")
    g.add_argument("--css", action="append", metavar="SELECTOR", help="print what this CSS selector matches")
    g.add_argument("--xpath", action="append", metavar="XPATH", help="print what this XPath matches")
    _add_extract_options(g)
    g.add_argument("-f", "--format", choices=[*FORMATS, "csv"], help="output format (default: from -o extension)")
    g.add_argument("-o", "--output", metavar="FILE", help="write to a file instead of stdout")
    g.add_argument("--main-content", action="store_true", help="only convert the page's main content")
    g.add_argument("--adaptive", action="store_true", help="use adaptive selectors (survive layout changes)")
    g.add_argument(
        "--impersonate",
        default="chrome",
        metavar="BROWSER",
        help="browser fingerprint: chrome, firefox, safari, edge, or none (default: chrome)",
    )
    g.add_argument("-H", "--header", action="append", metavar="'NAME: VALUE'", help="extra request header")
    g.add_argument("--cookie", action="append", metavar="NAME=VALUE", help="cookie to send")
    g.add_argument("--timeout", type=float, default=30.0, metavar="SEC")
    g.add_argument("--retries", type=int, default=2)
    g.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    _add_network_options(g)
    g.add_argument("--wait-for", metavar="SELECTOR", help="(browser) wait for this element")
    g.add_argument("--wait", type=float, default=0.0, metavar="SEC", help="(browser) extra wait after load")
    g.add_argument("--scroll", action="store_true", help="(browser) scroll to the bottom (infinite scroll)")
    g.add_argument("--headful", action="store_true", help="(browser) show the browser window")
    g.add_argument("--screenshot", metavar="FILE", help="(browser) save a full-page screenshot")
    g.add_argument(
        "--layout",
        action="store_true",
        help="(implies --browser) record where the page draws its text: --extract then reads labelled values "
        "from it (a tile's number under its label, a value beside its label)",
    )
    g.add_argument(
        "--vision",
        action="store_true",
        help="(--extract, --model; implies --browser) show the model the page's screenshot too",
    )
    g.add_argument(
        "--do",
        action="append",
        metavar="STEP",
        help='(implies --browser) do this on the page first: "click .more until-gone", "expand .faq button", '
        '"dismiss #cookies button", "fill #q => parka", "press Enter", "wait .results", "scroll 5", '
        '"tabs .tabs a", "pdf page.pdf", "download a.csv" (repeatable; see docs/fetching.md)',
    )
    g.add_argument("--actions", metavar="FILE", help="(implies --browser) the steps, from a JSON or YAML list")
    g.add_argument("--downloads", metavar="DIR", help="(download steps) where to keep the files")
    g.add_argument("--capture", action="store_true", help="(browser) record the page's own JSON API calls")
    g.add_argument(
        "--why", metavar="FIELD", help="(--extract) say why FIELD is what it is (or empty) on the page, and how sure"
    )
    g.add_argument(
        "--auto-browser",
        action="store_true",
        help="fetch over HTTP, and again in a browser if the page needs JavaScript to show its content",
    )
    g.add_argument(
        "--render-if-missing",
        action="append",
        metavar="SELECTOR",
        help="(implies --auto-browser) a page where this finds nothing is fetched again in a browser",
    )
    g.add_argument(
        "--capture-filter",
        metavar="URL_PATTERN",
        help="(browser) record the API calls whose URL matches this glob/substring (implies --capture)",
    )
    smart = g.add_argument_group("zero-selector extraction")
    smart.add_argument("--auto", action="store_true", help="find the page's repeating records and extract them")
    smart.add_argument(
        "--learn",
        action="append",
        metavar="FIELD=EXAMPLE",
        help="learn selectors from example values on the page, e.g. --learn 'title=A Light in the Attic'",
    )
    smart.add_argument("--save-schema", metavar="FILE", help="save the schema learned with --learn")
    smart.add_argument("--schema", metavar="FILE", help="extract with a schema saved by --save-schema")
    _add_typed_extract_options(smart)
    smart.add_argument("--structured", action="store_true", help="JSON-LD, microdata, OpenGraph and meta data")
    smart.add_argument("--json-data", action="store_true", help="JSON embedded by JS apps (__NEXT_DATA__, ...)")
    smart.add_argument("--tables", action="store_true", help="every HTML table as records")
    smart.add_argument(
        "--visual-tables",
        action="store_true",
        help="(implies --browser) the tables the page draws, whatever its HTML: rows aligned in columns",
    )
    smart.add_argument("--next", action="store_true", help="the URL of the next page (pagination)")
    smart.add_argument(
        "--sources",
        action="store_true",
        help="where the page's data is: HTML records, tables, JSON-LD, embedded JSON and (with --browser) the API "
        "calls it makes, with the records each holds, GraphQL operations and pagination (see docs/sources.md)",
    )
    smart.add_argument("--explain", action="store_true", help="(--extract) show where every value came from")
    _add_cache_options(g)
    g.set_defaults(func=cmd_get)

    c = sub.add_parser(
        "crawl",
        help="run a spider file, or crawl a site from a URL",
        description="Run a Spider subclass from a .py file, or crawl a site starting at a URL.",
        epilog=EPILOG_CRAWL,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    c.add_argument("target", metavar="SPIDER.py[:Class] | URL")
    _add_crawl_options(c)
    c.set_defaults(func=cmd_crawl)
    x = sub.add_parser(
        "extract",
        help="crawl a site and extract typed records with a schema or a template (crawl --extract)",
        description="Crawl a site from a URL and extract the records a schema (or a template) describes, from "
        "every page that holds one. The same as crawl URL --extract SCHEMA, with every crawl option.",
        epilog=EPILOG_EXTRACT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    x.add_argument("target", metavar="URL")
    _add_crawl_options(x, extract_command=True)
    x.set_defaults(func=cmd_crawl)

    data = sub.add_parser(
        "data",
        help="infer schemas, validate, clean and check the quality of datasets",
        description="Work with scraped datasets: JSON Lines, JSON, CSV, Parquet or Excel files, or PostgreSQL tables.",
        epilog=EPILOG_DATA,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    data.set_defaults(func=lambda args: _print_help(data))
    actions = data.add_subparsers(dest="action", metavar="ACTION")
    run = actions.add_parser("run", help="run records through a pipeline file")
    run.add_argument("pipeline", metavar="PIPELINE", help="pipeline file (.yaml, .json or .toml)")
    run.add_argument("input", metavar="INPUT")
    run.add_argument(
        "-o", "--output", metavar="FILE", help="where to write the results (default: JSON lines on stdout)"
    )
    run.add_argument("--allow-imports", action="store_true", help="let the pipeline call Python functions it names")
    run.add_argument("--limit", type=int, metavar="N", help="only the first N records")
    run.set_defaults(func=cmd_data_run)
    val = actions.add_parser("validate", help="normalize and validate records against a schema")
    val.add_argument("schema", metavar="SCHEMA", help="schema file (.json, .yaml or .toml)")
    val.add_argument("input", metavar="INPUT")
    val.add_argument("-o", "--output", metavar="FILE", help="write the valid (normalized) records")
    val.add_argument("--rejects", metavar="FILE", help="write the invalid records with their issues (JSON lines)")
    val.add_argument("--no-normalize", action="store_true", help="validate the records as they are")
    val.add_argument("--country", metavar="CC", help="country for local formats (phone numbers, $, kr...)")
    val.add_argument("--currency", metavar="CODE", help="currency of prices that name none")
    val.add_argument("--dayfirst", action="store_true", default=None, help="read 03/05/2024 as 3 May")
    val.add_argument("--max-issues", type=int, default=10, metavar="N", help="issue kinds to list (default 10)")
    val.add_argument("--limit", type=int, metavar="N", help="only the first N records")
    val.set_defaults(func=cmd_data_validate)
    inf = actions.add_parser("infer", help="guess a schema from sample records")
    inf.add_argument("input", metavar="INPUT")
    inf.add_argument("-o", "--output", metavar="FILE", help="save the schema (.json or .yaml)")
    inf.add_argument("--name", help="the schema's name (default: the file name)")
    inf.add_argument("--sample", type=int, default=1000, metavar="N", help="records to learn from (default 1000)")
    inf.add_argument("--explain", action="store_true", help="say why each field got its type")
    inf.set_defaults(func=cmd_data_infer)
    qual = actions.add_parser("quality", help="measure quality, and compare with a baseline report")
    qual.add_argument("input", metavar="INPUT")
    qual.add_argument("--schema", metavar="FILE", help="validate against this schema too")
    qual.add_argument("--key", action="append", metavar="FIELD", help="field(s) identifying a record")
    qual.add_argument("--baseline", metavar="FILE", help="an earlier report (exit status 1 on serious degradation)")
    qual.add_argument("--save", metavar="FILE", help="save the report (the next run's baseline)")
    qual.add_argument("--json", action="store_true", help="print the report as JSON")
    qual.add_argument("--limit", type=int, metavar="N", help="only the first N records")
    qual.set_defaults(func=cmd_data_quality)
    ent = actions.add_parser("entities", help="find which names are the same company, brand, product, person or place")
    ent.add_argument("input", metavar="INPUT")
    ent.add_argument("--field", required=True, metavar="FIELD", help="the field holding the names (dotted paths work)")
    ent.add_argument(
        "--kind",
        default="company",
        choices=("company", "organization", "brand", "product", "person", "location"),
        help="what the names are (default: company)",
    )
    ent.add_argument(
        "--attribute",
        action="append",
        metavar="ATTR[=FIELD]",
        help="evidence to compare, e.g. website=company_url, phone, country, gtin, email (repeatable)",
    )
    ent.add_argument("--source", metavar="FIELD", help="the field saying where a record came from (default: url)")
    ent.add_argument("--merge", type=float, default=0.95, metavar="P", help="merge pairs scoring at least P (0.95)")
    ent.add_argument("--review", type=float, default=0.5, metavar="P", help="list pairs scoring at least P (0.5)")
    ent.add_argument("-o", "--output", metavar="FILE", help="write one merged record per entity")
    ent.add_argument("--review-output", metavar="FILE", help="write the pairs to review, with their evidence")
    ent.add_argument(
        "--annotate", metavar="FILE", help="write the input records with FIELD_entity and FIELD_canonical added"
    )
    ent.add_argument("--show", type=int, default=10, metavar="N", help="entities and review pairs to print (10)")
    ent.add_argument("--limit", type=int, metavar="N", help="only the first N records")
    ent.set_defaults(func=cmd_data_entities)
    an = actions.add_parser(
        "analyze", help="a text field's language, keywords and length; with a model, its topic, sentiment and entities"
    )
    an.add_argument("input", metavar="INPUT")
    an.add_argument("--field", required=True, metavar="FIELD", help="the text to analyze (dotted paths work)")
    an.add_argument(
        "--add",
        metavar="FEATURES",
        help="comma-separated: language, language_confidence, keywords, words, sentences, characters, "
        "reading_minutes, script "
        "(default: language, keywords, words, reading_minutes)",
    )
    an.add_argument("--prefix", default="", metavar="TEXT", help="put before the added fields' names")
    an.add_argument(
        "--model",
        metavar="PROVIDER:NAME",
        help="also ask a language model for the topic, sentiment and entities (checked; see docs/models.md)",
    )
    an.add_argument("--model-url", metavar="URL", help="(--model) where the model's API is (a server of your own)")
    an.add_argument("--categories", metavar="A,B,C", help="(--model) the categories to choose one from")
    an.add_argument("-o", "--output", metavar="FILE", help="write the records with what was found (default stdout)")
    an.add_argument("--limit", type=int, metavar="N", help="only the first N records")
    an.set_defaults(func=cmd_data_analyze)
    pl = actions.add_parser(
        "places", help="where records are, normalized; kept by country, region, city or distance; grouped by place"
    )
    pl.add_argument("input", metavar="INPUT")
    pl.add_argument(
        "--country",
        metavar="COUNTRY",
        help="the country the records' addresses are in when they do not say it (settles 'CA', 'WA'...)",
    )
    pl.add_argument(
        "--in",
        dest="within_place",
        action="append",
        metavar="PLACE",
        help="keep records in a country (DE, Germany), region (US-CA, California) or city (Berlin); repeatable",
    )
    pl.add_argument("--near", metavar="LAT,LON", help="keep records within --within km of a point (their coordinates)")
    pl.add_argument("--within", type=float, metavar="KM", help="(--near) the distance, in kilometres")
    pl.add_argument("--remote", action="store_true", help="keep records whose place says remote")
    pl.add_argument(
        "--by",
        metavar="PART",
        help="print the records grouped by country, region, city, postal_code or remote (or another field)",
    )
    pl.add_argument("--stats", action="append", metavar="FIELD", help="(--by) sum up a numeric field in each group")
    pl.add_argument("--top", type=int, default=20, metavar="N", help="(--by) groups to print (20)")
    pl.add_argument("--json", action="store_true", help="(--by) print the groups as JSON")
    pl.add_argument(
        "--add",
        metavar="PARTS",
        help="comma-separated parts to add to the records: country, region, city, postal_code, street, "
        "coordinates, remote (default: country, region, city, postal_code, coordinates)",
    )
    pl.add_argument("--prefix", default="", metavar="TEXT", help="put before the added fields' names")
    pl.add_argument(
        "--field",
        action="append",
        metavar="PART=NAME",
        help="where a part is in the records, e.g. city=town or address=hq.address (repeatable)",
    )
    pl.add_argument(
        "-o", "--output", metavar="FILE", help="write the records kept, with their place (default stdout, unless --by)"
    )
    pl.add_argument("--limit", type=int, metavar="N", help="only the first N records")
    pl.set_defaults(func=cmd_data_places)
    gr = actions.add_parser(
        "graph", help="a knowledge graph: the things records name, resolved, and how they relate, with sources"
    )
    gr.add_argument(
        "input",
        nargs="+",
        metavar="[KIND=]INPUT",
        help="records (job=jobs.jsonl for records of a kind; by default --kind, or what the records say)",
    )
    gr.add_argument("--kind", metavar="KIND", help="what the records are (product, job, article, event, company...)")
    gr.add_argument(
        "--relation",
        action="append",
        metavar="FIELD=RELATION:KIND",
        help="another edge, e.g. seller=sold_by:company (repeatable)",
    )
    gr.add_argument("--only", action="store_true", help="only the --relation edges, not the kind's usual ones")
    gr.add_argument("--merge", type=float, default=0.95, metavar="P", help="merge names scoring at least P (0.95)")
    gr.add_argument("--review", type=float, default=0.5, metavar="P", help="list pairs scoring at least P (0.5)")
    gr.add_argument(
        "-o", "--output", metavar="FILE", help="write it: .json, .graphml, or a directory (nodes.csv, edges.csv)"
    )
    gr.add_argument("--show", type=int, default=5, metavar="N", help="nodes of each kind to print (5)")
    gr.add_argument("--limit", type=int, metavar="N", help="only the first N records of each input")
    gr.set_defaults(func=cmd_data_graph)
    dif = actions.add_parser("diff", help="what was added, removed and changed between two datasets")
    dif.add_argument("old", metavar="OLD", help="a file, or DIR@VERSION (DIR@v2, DIR@previous) saved by `data commit`")
    dif.add_argument("new", metavar="NEW", help="a file, or DIR@VERSION (DIR@latest)")
    dif.add_argument(
        "--key",
        action="append",
        metavar="FIELD",
        help="field(s) identifying a record (default: the versions' key, or url)",
    )
    dif.add_argument("--ignore", action="append", metavar="FIELD", help="a field not to compare (repeatable)")
    dif.add_argument("--private", action="store_true", help="also compare fields starting with _")
    dif.add_argument(
        "-o", "--output", metavar="FILE", help="write the differences: added and removed records, changed fields"
    )
    dif.add_argument("--json", action="store_true", help="print the summary as JSON")
    dif.add_argument("--fields", type=int, default=10, metavar="N", help="changed fields to list (10)")
    dif.add_argument("--exit-code", action="store_true", help="exit with status 1 when the datasets differ")
    dif.set_defaults(func=cmd_data_diff)
    com = actions.add_parser("commit", help="save a dataset as the next version in a versions directory")
    com.add_argument("directory", metavar="DIR", help="the versions directory (created if missing)")
    com.add_argument("input", metavar="INPUT")
    com.add_argument("--key", action="append", metavar="FIELD", help="field(s) identifying a record")
    com.add_argument("-m", "--message", help="a note saved with the version")
    com.add_argument("--force", action="store_true", help="save a version even if nothing changed")
    com.set_defaults(func=cmd_data_commit)
    log = actions.add_parser("log", help="list the versions saved in a versions directory")
    log.add_argument("directory", metavar="DIR")
    log.set_defaults(func=cmd_data_log)

    ins = sub.add_parser(
        "inspect",
        help="profile a website: technologies, page types, templates, APIs, crawlability",
        description="Read a site's robots.txt and sitemaps, visit a sample of its pages, and describe the site.",
        epilog=EPILOG_INSPECT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ins.add_argument("url", metavar="URL")
    ins.add_argument("--pages", type=int, default=30, metavar="N", help="pages to visit (default 30)")
    ins.add_argument("--browser", "-b", action="store_true", help="render pages and record their API calls")
    ins.add_argument("--no-robots", action="store_true", help="do not obey robots.txt (it is still read)")
    ins.add_argument("--no-sitemaps", action="store_true", help="do not read the sitemaps")
    ins.add_argument("--timeout", type=float, default=20, metavar="SEC", help="per request (default 20)")
    ins.add_argument("-o", "--output", metavar="FILE", help="save the full profile as JSON")
    ins.add_argument("--json", action="store_true", help="print the profile as JSON")
    ins.add_argument("--show", type=int, default=8, metavar="N", help="entries per list (8)")
    ins.add_argument("--depth", type=int, default=2, metavar="N", help="levels of the section tree (2)")
    ins.set_defaults(func=cmd_inspect)

    gp = sub.add_parser(
        "goal",
        help="say what data you want: wintergrab plans the crawl, shows it, and collects it",
        description="Read a request in plain words, survey the site, show the plan and its cost, and run it.",
        epilog=EPILOG_GOAL,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    gp.add_argument(
        "text", nargs="*", metavar="REQUEST", help='what to collect, e.g. "products under $50 on shop.example"'
    )
    gp.add_argument("--site", action="append", metavar="URL", help="a site to look at (repeatable)")
    gp.add_argument("--plan", metavar="FILE", help="run a saved plan instead of reading a request")
    gp.add_argument("--plan-only", action="store_true", help="show the plan, collect nothing")
    gp.add_argument("--save-plan", metavar="FILE", help="save the plan as JSON (to edit it, or run it later)")
    gp.add_argument("--explain", action="store_true", help="also say what each estimate rests on")
    gp.add_argument("-y", "--yes", action="store_true", help="run big plans without asking")
    gp.add_argument(
        "--confirm-over", type=int, default=200, metavar="N", help="ask before plans of more than N requests (200)"
    )
    gp.add_argument("--sample", type=int, default=30, metavar="N", help="pages to survey per site (30)")
    gp.add_argument("--max-pages", type=int, metavar="N", help="stop after N pages")
    gp.add_argument("--record", action="store_true", help="keep the run's pages and items, to replay it")
    gp.add_argument(
        "--quality",
        metavar="FILE",
        help="measure the records' quality and compare it with the last run's, kept in FILE",
    )
    gp.add_argument(
        "--model",
        metavar="PROVIDER:NAME",
        help="read the request with a language model (openai:NAME, anthropic:NAME, ollama:NAME); its reading is "
        "checked, and the rules read it when the model cannot (see docs/models.md)",
    )
    gp.add_argument("--model-url", metavar="URL", help="(--model) where the model's API is (a server of your own)")
    gp.add_argument("--workspace", metavar="DIR", help="where runs are kept (default .wintergrab)")
    gp.add_argument("--project", metavar="FILE", help="(set by wintergrab run/schedule) post events to its webhooks")
    gp.add_argument("--job", metavar="NAME", help="(set by wintergrab run/schedule) the job's name, kept with the run")
    gp.add_argument(
        "--no-optimize",
        action="store_true",
        help="fetch every page the plan leads to (by default, URL patterns that give nothing are skipped)",
    )
    gp.add_argument("--browser", "-b", action="store_true", help="survey with a browser (slower)")
    gp.add_argument(
        "--find-sites",
        action="store_true",
        help="when the request names no site, ask a search API which sites rank for it (see wintergrab search)",
    )
    gp.add_argument("--provider", default="brave", choices=["brave", "google", "searxng"],
                    help="(--find-sites) the search API (brave)")  # fmt: skip
    gp.add_argument("--endpoint", metavar="URL", help="(--find-sites) the search API's URL (a SearXNG instance)")
    gp.add_argument(
        "--no-api",
        action="store_true",
        help="read the pages, even where the site's pages call an API that holds the records "
        "(by default the plan collects from it)",
    )
    gp.add_argument("--timeout", type=float, default=20, metavar="SEC", help="per request (default 20)")
    gp.add_argument("-o", "--output", metavar="FILE", help="save the records (.jsonl, .csv, .json); default stdout")
    gp.add_argument("--json", action="store_true", help="print the plan as JSON (and collect nothing)")
    gp.set_defaults(func=cmd_goal)

    se = sub.add_parser(
        "search",
        help="search results from a search API (Brave, Google, your SearXNG), and what they say of rankings",
        description="Ask a search API that permits it, with your key, for the results of queries (and the pages' "
        "other results and boxes: news, videos, local results, questions...); or read collected results for "
        "competitors, gaps, queries one page can answer, the pages' boxes, and rankings over time.",
        epilog=EPILOG_SEARCH,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    se.add_argument("query", nargs="*", metavar="QUERY", help="what to search for (repeatable)")
    se.add_argument("--provider", choices=["brave", "google", "searxng"],
                    help="the search API: brave (BRAVE_SEARCH_API_KEY; the default), google (GOOGLE_API_KEY and "
                    "GOOGLE_CSE_ID), searxng (SEARXNG_URL)")  # fmt: skip
    se.add_argument("--endpoint", metavar="URL", help="the API's URL (your SearXNG instance: https://searx.example)")
    se.add_argument("--pages", type=int, metavar="N", help="pages of results per query (1; 10 at most)")
    se.add_argument("--param", action="append", metavar="NAME=VALUE",
                    help="one of the API's own parameters, such as where and in what language to search: "
                    "country=de, search_lang=de (Brave), gl=de, hl=de (Google), language=de (SearXNG); repeatable")  # fmt: skip
    se.add_argument("--delay", type=float, metavar="SEC", help="between requests (1)")
    se.add_argument("--timeout", type=float, metavar="SEC", help="per request (20)")
    se.add_argument("-o", "--output", metavar="FILE", help="save the results as records (.jsonl, .csv, a database...)")
    se.add_argument("--append", action="store_true",
                    help="(-o) add to the output instead of replacing it: search again later, and --report shows how "
                    "rankings moved")  # fmt: skip
    se.add_argument("--report", nargs="+", metavar="FILE", help="read collected results instead, and report on them")
    se.add_argument("--domain", metavar="DOMAIN", help="(--report) your site: its visibility, gaps and history")
    se.add_argument("--before", metavar="FILE", help="(--report, --domain) earlier results: what moved since")
    se.add_argument("--depth", type=int, metavar="N", help="(--report) positions that count (10)")
    se.add_argument("--show", type=int, metavar="N", help="(--report) entries per list (15)")
    se.set_defaults(func=cmd_search)

    gen = sub.add_parser(
        "generate",
        help="generate a scraper for a goal, test it, and keep it if it passes",
        description="Survey a site for a goal, learn selectors for its fields from record pages, then lint, "
        "test, sample-crawl, validate and benchmark the scraper, and accept or reject it.",
        epilog=EPILOG_GENERATE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    gen.add_argument("text", nargs="+", metavar="REQUEST", help='what to collect, e.g. "books with title and price"')
    gen.add_argument("-o", "--out", required=True, metavar="DIR", help="where to write the scraper")
    gen.add_argument("--site", action="append", metavar="URL", help="the site, when the request does not name it")
    gen.add_argument(
        "--model",
        metavar="PROVIDER:NAME",
        help="a language model to read the request and find the values the pages do not publish while "
        "generating; the scraper runs without it (see docs/models.md)",
    )
    gen.add_argument("--model-url", metavar="URL", help="(--model) where the model's API is (a server of your own)")
    gen.add_argument("--sample", type=int, default=30, metavar="N", help="pages to survey the site with (30)")
    gen.add_argument("--train", type=int, default=5, metavar="N", help="record pages to generate it from (5)")
    gen.add_argument("--test", type=int, default=10, metavar="N", help="record pages beyond those to test it on (10)")
    gen.add_argument(
        "--min-completeness",
        type=float,
        default=0.9,
        metavar="SHARE",
        help="share of the pages tested on on which each required field must be found (0.9)",
    )
    gen.add_argument(
        "--min-agreement",
        type=float,
        default=0.9,
        metavar="SHARE",
        help="share of the goal's own extraction's values it must find too (0.9)",
    )
    gen.add_argument("--browser", "-b", action="store_true", help="survey with a browser (slower)")
    gen.add_argument("--timeout", type=float, default=20, metavar="SEC", help="per request (default 20)")
    gen.add_argument("--json", action="store_true", help="print the report as JSON")
    gen.set_defaults(func=cmd_generate)

    rv = sub.add_parser(
        "review",
        help="decide what a healing extractor was unsure of: values, selector repairs",
        description="List the items of a review queue (--review FILE of get/crawl --heal), and decide them.",
    )
    rv.add_argument("file", metavar="FILE", help="the review queue (JSON Lines)")
    rv.add_argument("--all", action="store_true", help="every item, decided ones too")
    rv.add_argument("--accept", metavar="ID", help="accept an item (its first candidate, or --choice)")
    rv.add_argument("--choice", metavar="LETTER", help="(--accept) the candidate: A, B, C...")
    rv.add_argument("--reject", metavar="ID", help="reject an item")
    rv.add_argument("--correct", nargs=2, metavar=("ID", "VALUE"), help="give the right value (or selector)")
    rv.add_argument("--note", metavar="TEXT", help="why (kept with the decision)")
    rv.add_argument("--json", action="store_true", help="print the items as JSON")
    rv.set_defaults(func=cmd_review)

    fx = sub.add_parser(
        "fixture",
        help="keep a page and the values a schema must read from it (for wintergrab test)",
        description="Keep pages as fixtures: the page, and the values expected from it (what --schema reads "
        "now, and --expect FIELD=VALUE on top). Review the values: they are what wintergrab test expects.",
    )
    fx.add_argument("urls", nargs="*", metavar="URL", help="pages to keep")
    fx.add_argument("--to", required=True, metavar="DIR", help="the fixtures' directory")
    fx.add_argument("--schema", metavar="FILE", help="what reads the pages (kept as the suite's schema)")
    fx.add_argument("--expect", action="append", metavar="FIELD=VALUE", help="a value to expect (JSON; null: none)")
    fx.add_argument("--only", action="store_true", help="expect only the --expect values")
    fx.add_argument("--from-run", metavar="RUN", help="take the pages a recorded run kept (crawl --record)")
    fx.add_argument("--match", metavar="REGEX", help="(--from-run) only pages whose URL matches")
    fx.add_argument("--limit", type=int, default=20, metavar="N", help="(--from-run) at most N pages (20)")
    fx.add_argument("--workspace", metavar="DIR", help="(--from-run) where runs are kept (default .wintergrab)")
    fx.add_argument("--name", metavar="NAME", help="the fixture's name (default: from the URL)")
    fx.add_argument("--note", metavar="TEXT", help="why it is there")
    fx.add_argument("--timeout", type=float, default=30, metavar="SEC", help="per page fetched (default 30)")
    fx.set_defaults(func=cmd_fixture)

    ts = sub.add_parser(
        "test",
        help="check that a schema still reads what its fixtures expect",
        description="Read every fixture's page with the schema and compare each value with the expected one. "
        "Exit status 1 when one differs.",
    )
    ts.add_argument("directory", nargs="?", metavar="DIR", help="the fixtures (wintergrab fixture --to DIR)")
    ts.add_argument("--schema", metavar="FILE", help="the schema to test (default: the one the suite was made with)")
    ts.add_argument("--heal", metavar="DIR", help="test a healing extractor's active version (its fixtures by default)")
    ts.add_argument("--only", action="append", metavar="NAME", help="only these fixtures (repeatable; a prefix works)")
    ts.add_argument("--update", action="store_true", help="accept what was read where it differs (review it)")
    ts.add_argument("--json", action="store_true", help="print the report as JSON")
    ts.set_defaults(func=cmd_test)

    rj = sub.add_parser(
        "run",
        help="run a project's jobs now (wintergrab.yaml)",
        description="Run the jobs of a project (wintergrab.yaml in the current directory, or a file named first, "
        "or --project FILE) now: all of them, or those named. Each runs in a process of its own; its run is kept "
        "in the workspace.",
    )
    rj.add_argument("jobs", nargs="*", metavar="[PROJECT] JOB",
                    help="the jobs to run (default: all), after the project file if it is not wintergrab.yaml here "
                    "(wintergrab run shop.yaml prices)")  # fmt: skip
    rj.add_argument("--project", metavar="FILE", help="the project file (default: wintergrab.yaml here)")
    rj.add_argument("--list", action="store_true", help="list the jobs and their command lines")
    rj.set_defaults(func=cmd_run)

    sc = sub.add_parser(
        "schedule",
        help="run a project's jobs on their schedules, until stopped",
        description="Run the jobs of a project on their schedules (cron, 'every 2 hours', 'daily at 06:00'), one "
        "after the other, until stopped. Their output goes to the workspace's logs/.",
    )
    sc.add_argument("file", nargs="?", metavar="PROJECT", help="the project file (default: wintergrab.yaml here)")
    sc.add_argument("--project", metavar="FILE", help="the project file, the same way")
    sc.add_argument("--list", action="store_true", help="the scheduled jobs and when they run next")
    sc.add_argument("--once", action="store_true", help="run the jobs due now, then stop (for cron or CI)")
    sc.add_argument("--listen", metavar="[HOST:]PORT",
                    help="run jobs when asked over HTTP too (POST /jobs/NAME/run), requests carrying the token in "
                    "WINTERGRAB_TRIGGER_TOKEN; on 127.0.0.1 unless a host is given")  # fmt: skip
    sc.set_defaults(func=cmd_schedule)

    it = sub.add_parser(
        "init",
        help="start a project: wintergrab.yaml and a workspace",
        description="Write a wintergrab.yaml to start from, and create the workspace (.wintergrab) that keeps runs.",
    )
    it.add_argument("directory", nargs="?", metavar="DIR", help="where (default: here)")
    it.add_argument("--force", action="store_true", help="write over an existing wintergrab.yaml")
    it.set_defaults(func=cmd_init)

    db = sub.add_parser(
        "dashboard",
        help="a web page over the runs: what each crawl did, and what one is doing now",
        description="Serve a dashboard of the workspace's runs, and of the project's jobs, on this machine "
        "(http://127.0.0.1:8710/): each run's numbers, failures, domains, extraction, changes and events, live "
        "while it runs. Also as JSON: /api/runs, /api/runs/RUN, /api/jobs.",
    )
    db.add_argument("--workspace", metavar="DIR", help="the workspace (default: the project's, or .wintergrab)")
    db.add_argument("--project", metavar="FILE", help="show this project's jobs (default: wintergrab.yaml here)")
    db.add_argument("--host", default="127.0.0.1", help="the address to listen on (default: this machine only)")
    db.add_argument("--port", type=int, default=8710, help="the port (default 8710)")
    db.add_argument("--open", action="store_true", help="open it in a browser")
    db.set_defaults(func=cmd_dashboard)

    bu = sub.add_parser(
        "build",
        help="build an extraction schema by clicking the parts of a page",
        description="Fetch a page and serve the visual builder on this machine (http://127.0.0.1:8711/): click "
        "the fields, a repeated card, a table or the next-page link; test the schema on the page; save it. The "
        "page is shown without its scripts.",
        epilog="examples:\n"
        "  wintergrab build https://books.toscrape.com/ -o books.schema.json --open\n"
        "  wintergrab crawl https://books.toscrape.com/ --extract books.schema.json -o books.jsonl\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    bu.add_argument("url", metavar="URL")
    bu.add_argument("-o", "--output", required=True, metavar="FILE", help="the schema file to write (and to load)")
    bu.add_argument("--name", metavar="NAME", help="what the records are (a new schema's name)")
    bu.add_argument("--browser", "-b", action="store_true", help="fetch the page in a browser (pages built by JS)")
    bu.add_argument("--timeout", type=float, default=30, metavar="SEC", help="to fetch the page (default 30)")
    bu.add_argument("--host", default="127.0.0.1", help="the address to listen on (default: this machine only)")
    bu.add_argument("--port", type=int, default=8711, help="the port (default 8711)")
    bu.add_argument("--open", action="store_true", help="open it in a browser")
    bu.set_defaults(func=cmd_build)

    ru = sub.add_parser(
        "runs",
        help="the runs kept in the workspace (crawl --record)",
        description="List the runs kept in the workspace (.wintergrab), show one, or remove one.",
    )
    ru.add_argument("run", nargs="?", metavar="RUN", help="show this run (run-7, 7, or last)")
    ru.add_argument("--limit", type=int, metavar="N", help="the N latest runs")
    ru.add_argument("--remove", metavar="RUN", help="delete a run and what it kept")
    ru.add_argument("--workspace", metavar="DIR", help="where runs are kept (default .wintergrab)")
    ru.add_argument("--json", action="store_true", help="print JSON")
    ru.set_defaults(func=cmd_runs)

    rp = sub.add_parser(
        "replay",
        help="crawl a recorded run again from its pages, offline, and compare the items",
        description="Crawl a recorded run (crawl --record) again from its recorded pages, without the network, "
        "and compare the items with the recorded ones. Exit status 1 when they differ.",
    )
    rp.add_argument("run", metavar="RUN", help="the run (run-7, 7, or last)")
    rp.add_argument("-o", "--output", metavar="FILE", help="the replay's items (default: in the run's directory)")
    rp.add_argument("--key", metavar="FIELD", help="the field identifying an item (default: unique_key, or url)")
    rp.add_argument("--spider", metavar="FILE.py:Class", help="replay with this spider instead of the run's own")
    rp.add_argument("--workspace", metavar="DIR", help="where runs are kept (default .wintergrab)")
    rp.add_argument("--json", action="store_true", help="print the differences as JSON")
    rp.set_defaults(func=cmd_replay)

    he = sub.add_parser(
        "heal",
        help="a healing extractor's versions, health and repairs",
        description="Show or change a self-healing extractor's versions (get/crawl --extract SCHEMA --heal DIR).",
    )
    he.add_argument("directory", metavar="DIR", help="the extractor's directory")
    he.add_argument("--log", action="store_true", help="every repair, rollback and review applied")
    he.add_argument("--rollback", action="store_true", help="go back to the version the active one came from")
    he.add_argument("--activate", type=int, metavar="N", help="make version N the active one")
    he.add_argument("--import", dest="import_schema", metavar="SCHEMA", help="a new version from a schema file")
    he.add_argument("--diff", nargs=2, type=int, metavar=("A", "B"), help="what changed between two versions")
    he.add_argument("--check", action="store_true", help="run the regression fixtures against the active version")
    he.add_argument("--review", metavar="FILE", help="apply the decisions of this review queue first")
    he.add_argument("--note", metavar="TEXT", help="why (kept in the log)")
    he.set_defaults(func=cmd_heal)

    h = sub.add_parser(
        "history",
        help="what changed between crawls, and how often pages change",
        description="Read a history file written by `crawl --history` (or a spider's `history` setting).",
        epilog=EPILOG_HISTORY,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    h.add_argument("file", metavar="FILE", help="the history file")
    h.add_argument("--name", help="only the runs of this spider")
    h.add_argument("--compare", nargs=2, type=int, metavar=("OLD", "NEW"), help="compare two runs (by id)")
    h.add_argument("--url", help="the history of one page, and how often it changes")
    h.add_argument("--due", action="store_true", help="the pages due for another look")
    h.add_argument("--show", type=int, default=10, metavar="N", help="pages to list (10)")
    h.add_argument("--json", action="store_true", help="print JSON")
    h.set_defaults(func=cmd_history)

    s = sub.add_parser("shell", help="interactive Python shell with a page loaded")
    s.add_argument("url", nargs="?")
    s.add_argument("--browser", "-b", action="store_true", help="render the page with a headless browser")
    s.set_defaults(func=cmd_shell)

    bm = sub.add_parser(
        "benchmark",
        help="measure how fast wintergrab runs on this machine: crawl, parse, extract, validate, deduplicate, "
        "write outputs",
        description="Measure wintergrab on this machine, against a synthetic shop served from 127.0.0.1: "
        "crawl throughput and latency, CPU and memory, parsing, extraction, validation, URL deduplication, "
        "start-up time, and with --browser, rendering.",
        epilog=EPILOG_BENCHMARK,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    bm.add_argument(
        "--scenario",
        action="append",
        choices=["startup", "crawl", "parse", "extract", "data", "dedupe", "outputs", "browser"],
        help="only this one (repeatable; default all but browser)",
    )
    bm.add_argument("--browser", action="store_true", help="also render pages in Chromium (needs the browser extra)")
    bm.add_argument("--pages", type=int, default=100, metavar="N", help="the shop's listing pages (100)")
    bm.add_argument("--items", type=int, default=1000, metavar="N", help="the shop's product pages (1000)")
    bm.add_argument("--latency", type=float, default=0.0, metavar="MS", help="delay each response, as a network would")
    bm.add_argument("--concurrency", type=int, default=32, metavar="N", help="the crawl's requests in flight (32)")
    bm.add_argument("--rounds", type=int, default=200, metavar="N", help="pages read by parse and extract (200)")
    bm.add_argument(
        "--store",
        action="append",
        metavar="URL",
        help="(outputs) also measure this database table or S3 object (postgresql://...?table=bench): its rows are "
        "replaced, as a fresh crawl replaces them (repeatable)",
    )
    bm.add_argument("--quick", action="store_true", help="a small shop and few rounds: a check in a few seconds")
    bm.add_argument("--json", action="store_true", help="print the report as JSON")
    bm.add_argument("-o", "--output", metavar="FILE", help="also save the report here as JSON")
    bm.set_defaults(func=cmd_benchmark)
    d = sub.add_parser("doctor", help="check the installation and optional features")
    d.set_defaults(func=cmd_doctor)

    tp = sub.add_parser(
        "templates",
        help="ready-made extraction schemas (product, article, job, event, property...)",
        description="List the extraction templates, or print one as a schema file to start your own from "
        "(wintergrab templates product > product.schema.json). Use one directly with --extract NAME.",
    )
    tp.add_argument("name", nargs="?", help="print this template as a schema (JSON)")
    tp.set_defaults(func=cmd_templates)

    pl = sub.add_parser(
        "plugins",
        help="the installed plugins and what they add",
        description="List the installed plugins (packages with a 'wintergrab.plugins' entry point) and what each "
        "adds: outputs, inputs, field types, stages, strategies, model providers, commands. WINTERGRAB_PLUGINS=0 "
        "loads none.",
    )
    pl.add_argument("--json", action="store_true", help="print JSON")
    pl.set_defaults(func=cmd_plugins)

    from .plugins import COMMANDS

    for name, (help_text, add_arguments, handler) in sorted(COMMANDS.items()):
        if name in sub.choices:
            logging.getLogger("wintergrab.plugins").warning("a plugin's command %r is a built-in one: skipped", name)
            continue
        plugin_parser = sub.add_parser(name, help=help_text)
        add_arguments(plugin_parser)
        plugin_parser.set_defaults(func=handler)
    return parser


def cmd_templates(args: argparse.Namespace) -> int:
    from .extraction.templates import ALIASES, template, template_names

    if args.name:
        try:
            schema = template(args.name)
        except KeyError as exc:
            print(f"error: {exc.args[0]}", file=sys.stderr)
            return 1
        print(json.dumps(schema.to_dict(), indent=2, ensure_ascii=False))
        return 0
    for name in template_names():
        schema = template(name)
        also = sorted(alias for alias, target in ALIASES.items() if target == name and "_" not in alias)
        fields = ", ".join(f.name + ("*" if f.required else "") for f in schema.fields)
        print(f"{name:<14} {fields}" + (f"  (also: {', '.join(also)})" if also else ""))
    print("\n* a page without it gives no record. Use one: --extract NAME; start from one: wintergrab templates NAME",
          file=sys.stderr)  # fmt: skip
    return 0


def cmd_plugins(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from .plugins import load_plugins

    found = load_plugins()
    if args.json:
        print(json.dumps([asdict(info) for info in found], indent=2))
        return 0
    if not found:
        disabled = os.environ.get("WINTERGRAB_PLUGINS", "1").strip().lower() in ("0", "false", "no", "off")
        print("plugins are turned off (WINTERGRAB_PLUGINS)" if disabled else "no plugin installed")
        return 0
    for info in found:
        print(info.describe())
    return 1 if any(info.error for info in found) else 0


def _print_help(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 2


def _utf8_output() -> None:
    """Write UTF-8 to redirected stdout/stderr.

    Scraped text can hold any character. Where the locale encoding is narrower
    (Windows pipes and files default to cp1252) printing it would crash, so
    switch to UTF-8 unless the user chose an encoding with PYTHONIOENCODING.
    """
    if os.environ.get("PYTHONIOENCODING"):
        return
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # e.g. a stream that was already written to in binary mode
                pass


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_output()
    from .plugins import load_plugins

    load_plugins()  # (commands of their own among them)
    parser = build_parser()
    args = parser.parse_args(argv)
    args.argv = list(argv) if argv is not None else sys.argv[1:]  # kept with recorded runs
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    if args.command != "crawl":
        level = logging.DEBUG if args.verbose > 0 else logging.WARNING
        configure_logging(level)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except WintergrabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
