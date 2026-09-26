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
        if args.screenshot:
            options["screenshot"] = args.screenshot
        if args.capture:
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


def cmd_get(args: argparse.Namespace) -> int:
    urls = [ensure_scheme(u) for u in args.urls]
    if args.capture_filter:
        args.capture = True
    fields = _fields(args)
    if fields and not args.each:
        args.each = ["html"]  # one record for the whole page
    examples = _parse_pairs(args.learn, "=", "--learn")
    schema = _load_schema(args.schema) if args.schema else None
    extractor = _extractor(args)
    # Modes that print one JSON document per page instead of page content.
    json_modes = [m for m in ("structured", "json_data", "tables", "next", "capture") if getattr(args, m)]
    selecting = bool(args.css or args.xpath)
    records = bool(args.each or args.auto or examples or schema or extractor)
    if json_modes:
        args.format = args.format or ("jsonl" if len(urls) > 1 else "json")
    default_fmt = "jsonl" if records else ("text" if selecting else "md")
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
        if extractor is not None:
            listing = args.all or args.container
            found = extractor.extract_all(page, container=args.container) if listing else [extractor.extract(page)]
            if listing and not found:
                print(f"warning: no records found on {page.url}", file=sys.stderr)
            for record in found:
                if args.explain:
                    print(record.explain(), file=sys.stderr)
                row = record.to_dict(provenance=args.provenance)
                rows.append({"url": page.url, **row} if multi and "url" not in row else row)
            if getattr(args, "why", None):
                if not hasattr(extractor, "why"):
                    print("error: --why needs --heal DIR", file=sys.stderr)
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

    if extractor is not None and hasattr(extractor, "close"):
        extractor.close()  # a healing extractor keeps what it learned
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
    #: A data schema file: extract typed records (see wintergrab.extraction).
    extract: str | None = None
    extract_all: bool = False
    container: str | None = None
    provenance: bool = False
    #: A directory of extractor versions: repair selectors when the site changes (--heal).
    heal: str | None = None
    #: A review queue file for what the healing extractor wants a person to decide (--review).
    review: str | None = None
    #: Pages where --extract found no complete record (a required field missing).
    incomplete: int = 0
    #: Follow every same-domain link when no --follow/--paginate is given.
    wander: bool = True
    #: Skip links to images, media, archives and crawler traps (``-s url_rules=null`` turns it off).
    url_rules = True

    def _records(self, response: Response) -> Any:
        extractor = self.__dict__.get("_extractor")
        if extractor is None:
            if self.heal:
                from .extraction.healing import HealingExtractor

                extractor = HealingExtractor(self.heal, self.extract, review=self.review, provenance=self.provenance)
            else:
                from .extraction import Extractor

                extractor = Extractor(self.extract, provenance=self.provenance)  # type: ignore[arg-type]
            self.__dict__["_extractor"] = extractor
        if self.extract_all or self.container:
            found = extractor.extract_all(response, container=self.container)
        else:
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
            goal = parse_goal(" ".join(args.text), sites=args.site or [])
            if args.verbose >= 0:
                print("Understood: " + goal.describe().replace("\n", "\n            "), file=sys.stderr)
            if not goal.sites:
                print("error: which site? Name it in the request (shop.example) or add --site URL", file=sys.stderr)
                return 2
            if args.verbose >= 0:
                print(
                    f"Surveying {', '.join(goal.sites)}: robots.txt, sitemaps, {args.sample} pages...", file=sys.stderr
                )
            plan = plan_goal(goal, sample=args.sample, browser=args.browser, timeout=args.timeout, log_level=level)
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


def cmd_run(args: argparse.Namespace) -> int:
    from .project import Scheduler, find_project
    from .redact import redact_argv

    project = find_project(args.project)
    jobs = project.select(args.jobs)
    if args.list:
        for job in project.jobs.values():
            print(f"{job.name:<16} wintergrab {' '.join(redact_argv(job.command()))}  ({job.trigger()})")
        return 0
    if not args.jobs:  # every job: the ones that come after another run after it
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

    project = find_project(args.project)
    scheduler = Scheduler(project, webhooks=[] if args.list else None)  # listing needs no secrets
    plan = scheduler.plan()
    if args.list or not plan:
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
            print("no job has a schedule or a watched URL", file=sys.stderr)
        return 0
    configure_logging(logging.DEBUG if args.verbose > 0 else logging.INFO)
    if args.once:
        results = scheduler.run_due()
        for hook in scheduler.webhooks:
            hook.close()
        print(f"{len(results)} job(s) were due", file=sys.stderr)
        return 1 if any(not r.ok for r in results) else 0
    print(f"{project.path}: {len(plan)} scheduled job(s), logs in {project.workspace / 'logs'}; Ctrl+C to stop",
          file=sys.stderr)  # fmt: skip
    for job, when in plan:
        print(f"  {job.name}: {job.trigger()}, next {when:%Y-%m-%d %H:%M}" if when else f"  {job.name}: never",
              file=sys.stderr)  # fmt: skip
    scheduler.loop()
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
        from .data.schema import load_schema

        name = load_schema(args.extract).name if getattr(args, "extract", None) else overrides.get("name") or cls.name
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


def _add_typed_extract_options(p: Any) -> None:
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


def _extractor(args: argparse.Namespace) -> Any:
    if not getattr(args, "extract", None) and not getattr(args, "heal", None):
        return None
    if getattr(args, "heal", None):
        from .extraction.healing import HealingExtractor

        return HealingExtractor(args.heal, args.extract, review=args.review, provenance=args.provenance)
    from .extraction import Extractor

    return Extractor(args.extract, provenance=args.provenance)


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
    g.add_argument("--why", metavar="FIELD", help="(--heal) say why FIELD is what it is (or empty) on the page")
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
    smart.add_argument("--next", action="store_true", help="the URL of the next page (pagination)")
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
    c.set_defaults(func=cmd_crawl)

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
    gp.add_argument("--workspace", metavar="DIR", help="where runs are kept (default .wintergrab)")
    gp.add_argument("--project", metavar="FILE", help="(set by wintergrab run/schedule) post events to its webhooks")
    gp.add_argument("--job", metavar="NAME", help="(set by wintergrab run/schedule) the job's name, kept with the run")
    gp.add_argument(
        "--no-optimize",
        action="store_true",
        help="fetch every page the plan leads to (by default, URL patterns that give nothing are skipped)",
    )
    gp.add_argument("--browser", "-b", action="store_true", help="survey with a browser (slower)")
    gp.add_argument("--timeout", type=float, default=20, metavar="SEC", help="per request (default 20)")
    gp.add_argument("-o", "--output", metavar="FILE", help="save the records (.jsonl, .csv, .json); default stdout")
    gp.add_argument("--json", action="store_true", help="print the plan as JSON (and collect nothing)")
    gp.set_defaults(func=cmd_goal)

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
        description="Run the jobs of a project (wintergrab.yaml in the current directory, or --project FILE) now: "
        "all of them, or those named. Each runs in a process of its own; its run is kept in the workspace.",
    )
    rj.add_argument("jobs", nargs="*", metavar="JOB", help="the jobs to run (default: all)")
    rj.add_argument("--project", metavar="FILE", help="the project file (default: wintergrab.yaml here)")
    rj.add_argument("--list", action="store_true", help="list the jobs and their command lines")
    rj.set_defaults(func=cmd_run)

    sc = sub.add_parser(
        "schedule",
        help="run a project's jobs on their schedules, until stopped",
        description="Run the jobs of a project on their schedules (cron, 'every 2 hours', 'daily at 06:00'), one "
        "after the other, until stopped. Their output goes to the workspace's logs/.",
    )
    sc.add_argument("--project", metavar="FILE", help="the project file (default: wintergrab.yaml here)")
    sc.add_argument("--list", action="store_true", help="the scheduled jobs and when they run next")
    sc.add_argument("--once", action="store_true", help="run the jobs due now, then stop (for cron or CI)")
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
