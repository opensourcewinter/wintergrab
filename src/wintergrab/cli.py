"""Command line interface: ``wintergrab get``, ``wintergrab crawl``, ``wintergrab shell``."""

from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.util
import inspect
import io
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .errors import FetchError, WintergrabError, describe
from .fetchers import AsyncBrowserFetcher, AsyncFetcher, Response
from .parser import Selector
from .proxy import ProxyRotator
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

Press Ctrl+C once to pause (state is saved when --crawl-dir is set); run the
same command again to resume. Press Ctrl+C twice to force quit.
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


def _write_rows(rows: list[dict[str, Any]], fmt: str, output: str | None) -> None:
    if fmt == "json":
        text = json.dumps([to_dict(r) for r in rows], ensure_ascii=False, indent=2, default=str) + "\n"
    elif fmt == "csv":
        buf = io.StringIO()
        columns = list(dict.fromkeys(k for r in rows for k in r))
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
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


def _value(sel: Selector, fmt: str) -> str:
    if not sel.is_element:
        return sel.get() or ""
    if fmt == "html":
        return sel.html
    if fmt == "md":
        return sel.markdown().strip()
    return sel.text


# --------------------------------------------------------------------------- #
# get
# --------------------------------------------------------------------------- #
async def _fetch_all(args: argparse.Namespace, urls: list[str]) -> list[Response | Exception]:
    headers = _parse_pairs(args.header, ":", "--header")
    cookies = _parse_pairs(args.cookie, "=", "--cookie")
    rotator = _proxies(args)
    fetcher: Any
    if args.browser:
        fetcher = AsyncBrowserFetcher(
            headless=not args.headful,
            proxies=rotator,
            timeout=args.timeout,
            retries=args.retries,
            cookies=cookies or None,
            extra_headers=headers or None,
        )
        options: dict[str, Any] = {"wait_for": args.wait_for, "wait": args.wait, "scroll": args.scroll}
        if args.screenshot:
            options["screenshot"] = args.screenshot
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


def cmd_get(args: argparse.Namespace) -> int:
    urls = [ensure_scheme(u) for u in args.urls]
    fields = _fields(args)
    if fields and not args.each:
        args.each = ["html"]  # one record for the whole page
    selecting = bool(args.css or args.xpath)
    records = bool(args.each)
    default_fmt = "jsonl" if records else ("text" if selecting else "md")
    fmt = _format_for(args, default_fmt)
    if fmt == "csv" and not records:
        raise SystemExit("error: CSV output needs --each/--field")

    results = asyncio.run(_fetch_all(args, urls))
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
        if page.status >= 400:
            failures += 1
        multi = len(urls) > 1
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

    if records or fmt in ("json", "jsonl", "csv"):
        _write_rows(rows, fmt, args.output)
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

    def parse(self, response: Response) -> Any:
        if self.each:
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
        if self.follow:
            links: list[str] = []
            for query in self.follow:
                links.extend(response.links(query, allow=self.allow or None, deny=self.deny or None))
        else:
            links = response.links(allow=self.allow or None, deny=self.deny or None, same_domain=True)
        for link in dict.fromkeys(links):
            yield response.follow(link)


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


def cmd_crawl(args: argparse.Namespace) -> int:
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
        )
    else:
        cls = load_spider_class(args.target)
        if args.follow or args.each or args.field:
            print("warning: --follow/--each/--field only apply to URL crawls; ignored", file=sys.stderr)
    option_map = {
        "output": args.output,
        "crawl_dir": args.crawl_dir,
        "concurrency": args.concurrency,
        "concurrency_per_domain": args.per_domain,
        "download_delay": args.delay,
        "max_pages": args.max_pages,
        "max_items": args.max_items,
        "max_depth": args.max_depth,
    }
    overrides.update({k: v for k, v in option_map.items() if v is not None})
    if args.no_autothrottle:
        overrides["autothrottle"] = False
    if args.no_robots:
        overrides["obey_robots_txt"] = False
    if args.browser:
        overrides["use_browser"] = True
    rotator = _proxies(args)
    if rotator:
        overrides["proxies"] = rotator
    overrides["log_level"] = "DEBUG" if args.verbose > 0 else ("WARNING" if args.verbose < 0 else "INFO")
    to_stdout = not overrides.get("output") and not getattr(cls, "output", None)
    if to_stdout:
        overrides["keep_items"] = False
    try:
        spider = cls(**overrides)
    except TypeError as exc:
        raise SystemExit(f"error: {exc}") from None

    if to_stdout:
        original = spider.process_item

        async def print_item(item: Any) -> Any:
            from .utils import maybe_await

            item = await maybe_await(original(item))
            if item is not None:
                sys.stdout.write(dumps(item) + "\n")
                sys.stdout.flush()
            return item

        spider.process_item = print_item  # type: ignore[method-assign]
    try:
        result = spider.run(resume=not args.fresh)
    except WintergrabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    stats = result.stats
    print(
        f"{result.status}: {stats.get('pages', 0)} pages, {stats.get('items', 0)} items, "
        f"{stats.get('errors', 0)} errors in {stats.get('elapsed_seconds', 0):.1f}s"
        + (f" -> {spider.output}" if spider.output else ""),
        file=sys.stderr,
    )
    if result.paused:
        print("paused - run the same command again to resume", file=sys.stderr)
    return 0


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
# argument parsing
# --------------------------------------------------------------------------- #
def _add_network_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--proxy", action="append", metavar="URL", help="proxy to use (repeat to rotate several)")
    p.add_argument("--proxy-file", metavar="FILE", help="file with one proxy per line")
    p.add_argument("--browser", "-b", action="store_true", help="use a headless browser (renders JavaScript)")


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
    g.set_defaults(func=cmd_get)

    c = sub.add_parser(
        "crawl",
        help="run a spider file, or crawl a site from a URL",
        description="Run a Spider subclass from a .py file, or crawl a site starting at a URL.",
        epilog=EPILOG_CRAWL,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    c.add_argument("target", metavar="SPIDER.py[:Class] | URL")
    c.add_argument(
        "-o", "--output", metavar="FILE", help="save items to .jsonl/.json/.csv (default: JSON lines on stdout)"
    )
    c.add_argument("--crawl-dir", metavar="DIR", help="directory for pause/resume state")
    c.add_argument("--fresh", action="store_true", help="ignore saved state and start over")
    c.add_argument("--concurrency", type=int, metavar="N", help="max requests in flight")
    c.add_argument("--per-domain", type=int, metavar="N", help="max requests in flight per domain")
    c.add_argument("--delay", type=float, metavar="SEC", help="minimum delay between requests to a domain")
    c.add_argument("--no-autothrottle", action="store_true", help="fixed speed instead of adaptive")
    c.add_argument("--max-pages", type=int, metavar="N")
    c.add_argument("--max-items", type=int, metavar="N")
    c.add_argument("--max-depth", type=int, metavar="N")
    c.add_argument("--no-robots", action="store_true", help="ignore robots.txt")
    c.add_argument("-s", "--set", action="append", metavar="NAME=VALUE", help="override a spider attribute")
    _add_network_options(c)
    c.add_argument("--follow", action="append", metavar="SELECTOR", help="(URL mode) links/containers to follow")
    c.add_argument("--allow", action="append", metavar="REGEX", help="(URL mode) only follow matching URLs")
    c.add_argument("--deny", action="append", metavar="REGEX", help="(URL mode) never follow matching URLs")
    c.add_argument("--any-domain", action="store_true", help="(URL mode) follow links to other domains too")
    _add_extract_options(c)
    c.set_defaults(func=cmd_crawl)

    s = sub.add_parser("shell", help="interactive Python shell with a page loaded")
    s.add_argument("url", nargs="?")
    s.add_argument("--browser", "-b", action="store_true", help="render the page with a headless browser")
    s.set_defaults(func=cmd_shell)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
