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
from collections.abc import Iterable, Sequence
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

EPILOG_DATA = """examples:
  wintergrab data infer items.jsonl -o product.schema.json       # guess a schema from records
  wintergrab data validate product.schema.json items.jsonl -o clean.jsonl --rejects rejects.jsonl
  wintergrab data run pipeline.yaml items.jsonl -o clean.csv
  wintergrab data quality items.jsonl --schema product.schema.json --save quality.json
  wintergrab data quality items.jsonl --baseline quality.json    # exit status 1 if quality collapsed

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
        if args.screenshot:
            options["screenshot"] = args.screenshot
        if args.capture:
            options["capture"] = args.capture_filter or True
            options.setdefault("wait_until", "networkidle")
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


def cmd_get(args: argparse.Namespace) -> int:
    urls = [ensure_scheme(u) for u in args.urls]
    if args.capture_filter:
        args.capture = True
    fields = _fields(args)
    if fields and not args.each:
        args.each = ["html"]  # one record for the whole page
    examples = _parse_pairs(args.learn, "=", "--learn")
    schema = _load_schema(args.schema) if args.schema else None
    # Modes that print one JSON document per page instead of page content.
    json_modes = [m for m in ("structured", "json_data", "tables", "next", "capture") if getattr(args, m)]
    selecting = bool(args.css or args.xpath)
    records = bool(args.each or args.auto or examples or schema)
    if json_modes:
        args.format = args.format or ("jsonl" if len(urls) > 1 else "json")
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
        if json_modes:
            doc: dict[str, Any] = {"url": page.url}
            if args.structured:
                doc["structured"] = page.structured_data()
            if args.json_data:
                doc["embedded_json"] = page.embedded_json()
            if args.tables:
                doc["tables"] = page.tables()
            if args.next:
                doc["next_page"] = page.next_page()
            if args.capture:
                doc["captured"] = [
                    {"url": c.url, "method": c.method, "status": c.status, "json": _maybe_json(c)}
                    for c in page.captured
                ]
            rows.append(doc)
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

    if records or json_modes or fmt in ("json", "jsonl", "csv"):
        _write_rows(rows, fmt, args.output, single=bool(json_modes))
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
    #: Follow every same-domain link when no --follow/--paginate is given.
    wander: bool = True
    #: Skip links to images, media, archives and crawler traps (``-s url_rules=null`` turns it off).
    url_rules = True

    def parse(self, response: Response) -> Any:
        if self.schema:
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
        if self.follow:
            for query in self.follow:
                links.extend(response.links(query, allow=self.allow or None, deny=self.deny or None))
        elif not self.paginate and self.wander:
            links = response.links(allow=self.allow or None, deny=self.deny or None, same_domain=True)
        if self.paginate:
            next_url = response.next_page()
            if next_url:
                links.append(next_url)
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
            auto=args.auto,
            paginate=args.paginate,
            schema=json.loads(Path(args.schema).read_text(encoding="utf-8")) if args.schema else None,
        )
        if args.sitemap:
            overrides["sitemap_urls"] = list(args.sitemap)
            if not args.follow and not args.paginate:
                overrides["wander"] = False  # sitemap-driven: crawl what the sitemap lists
    else:
        cls = load_spider_class(args.target)
        if args.follow or args.each or args.field or args.auto or args.paginate or args.schema:
            print(
                "warning: --follow/--each/--field/--auto/--paginate/--schema only apply to URL crawls", file=sys.stderr
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
    }
    if args.retry_failed:
        overrides["retry_dead_letters"] = True
    overrides.update({k: v for k, v in option_map.items() if v is not None})
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
        + (f" -> {spider.output}" if spider.output and spider.output != "-" else ""),
        file=sys.stderr,
    )
    _print_failures(result, verbose=args.verbose)
    if result.paused:
        print("paused - run the same command again to resume", file=sys.stderr)
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
    check("adaptive db", True, str(default_storage_path()))
    width = max(len(r[1]) for r in rows)
    for status, name, detail in rows:
        print(f"{status} {name.ljust(width)}  {detail}")
    return 0 if all(r[0].strip() == "ok" for r in rows[:4]) else 1


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
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
    from .data import Normalize, Pipeline, Validate, load_schema
    from .data.io import read_records

    schema = load_schema(args.schema)
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


def cmd_data_quality(args: argparse.Namespace) -> int:
    from .data import QualityMonitor, QualityReport, load_schema
    from .data.io import read_records

    schema = load_schema(args.schema) if args.schema else None
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
    g.add_argument("--capture", action="store_true", help="(browser) record the page's own JSON API calls")
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
    smart.add_argument("--structured", action="store_true", help="JSON-LD, microdata, OpenGraph and meta data")
    smart.add_argument("--json-data", action="store_true", help="JSON embedded by JS apps (__NEXT_DATA__, ...)")
    smart.add_argument("--tables", action="store_true", help="every HTML table as records")
    smart.add_argument("--next", action="store_true", help="the URL of the next page (pagination)")
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
    c.add_argument("--schema", metavar="FILE", help="(URL mode) extract with a schema saved by get --save-schema")
    c.add_argument("--sitemap", action="append", metavar="URL", help="take pages from a sitemap or robots.txt")
    c.add_argument("--unique-key", metavar="FIELD", help="drop duplicate items (and upsert into .sqlite output)")
    c.add_argument(
        "--normalize-urls",
        action="store_true",
        help="drop tracking parameters, session ids and fragments from URLs before queueing them",
    )
    c.add_argument("--pipeline", metavar="FILE", help="clean, validate and filter items with a pipeline file")
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
    c.set_defaults(func=cmd_crawl)

    data = sub.add_parser(
        "data",
        help="infer schemas, validate, clean and check the quality of datasets",
        description="Work with scraped datasets: JSON Lines, JSON or CSV files.",
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

    s = sub.add_parser("shell", help="interactive Python shell with a page loaded")
    s.add_argument("url", nargs="?")
    s.add_argument("--browser", "-b", action="store_true", help="render the page with a headless browser")
    s.set_defaults(func=cmd_shell)

    d = sub.add_parser("doctor", help="check the installation and optional features")
    d.set_defaults(func=cmd_doctor)
    return parser


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
