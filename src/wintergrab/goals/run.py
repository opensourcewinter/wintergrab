"""Running a plan: a spider per site that collects the goal's records.

::

    result = plan.run("laptops.jsonl")      # or run_plan(plan, "laptops.jsonl")
    print(result.summary())
    result = plan.run("laptops.jsonl", provenance=True, heal="laptops.extractor")   # the whole loop

Each site's :class:`GoalSpider` follows its :class:`~wintergrab.goals.plan.SitePlan`:
the API the site's pages call when the plan found one (:mod:`wintergrab.goals.api`),
page by page; the record pages listed in the sitemaps, or links followed from the start
pages through listings and pagination. An API that fails without refusing (it is not
found, it answers without records) leaves its site to its pages; one that refuses
(401, 403, 429, 451, a bot check) is not asked another way. On each record page the extraction
engine fills the goal's fields (with their confidence, ``_confidence``); a
data :class:`~wintergrab.data.Pipeline` keeps the records that meet the
goal's conditions and drops duplicates (the same URL: pages that name their
canonical URL count once). Pages whose content needs JavaScript go to a
browser when the plan says so, record pages are fetched before more listing
pages, and the crawl stops at the goal's limit. The crawl learns as it goes
(:mod:`wintergrab.spider.optimizer`): URL patterns that give nothing are
skipped, and query parameters that change nothing are dropped.

With ``provenance=True`` every record says where each value came from. With ``heal=DIR`` the records are
read by a self-healing extractor kept in that directory (:mod:`wintergrab.extraction.healing`): the
schema's selectors are repaired when the site changes, what it cannot decide and the values it found with
little confidence are questions for a person in its review queue, and the first complete record of each
site is kept as a regression fixture, so the whole loop of a goal (collect, keep provenance, notice a
change, repair or ask, test) is one run, repeated.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ..data.pipeline import Deduplicate, Filter, Pipeline
from ..data.schema import Schema
from ..errors import ConfigurationError, RobotsPolicyError, describe
from ..extraction import Extractor
from ..extraction.healing import HealingExtractor
from ..extraction.review import ReviewQueue
from ..fetchers.resources import registrable_domain
from ..fetchers.response import Response
from ..fetchers.strategy import FetchStrategy
from ..intel.classify import classify_page
from ..request import Request
from ..spider import CrawlResult, Spider
from ..spider.exporters import output_failures
from ..utils import host_of
from .api import MAX_API_PAGES, ApiSource, next_page, records_of, total_of
from .goal import Goal

if TYPE_CHECKING:
    from .plan import GoalPlan, SitePlan

__all__ = ["GoalResult", "GoalSpider", "run_plan"]

#: An API's answers that refuse: they are the site's answer, and the site is not asked another way.
_REFUSALS = (401, 403, 429, 451)


def _url_regex(pattern: str) -> str:
    """A path pattern (``/p/*``, ``/books/**``) as a regex for whole URLs."""
    parts = [s for s in pattern.split("/") if s]
    anywhere = bool(parts) and parts[-1] == "**"
    body = "/".join("[^/?#]+" if part == "*" else re.escape(part) for part in (parts[:-1] if anywhere else parts))
    tail = r"(?:/[^?#]*)?" if anywhere else "/?"
    return rf"^https?://[^/]+/{body}{tail}(?:[?#].*)?$"


class GoalSpider(Spider):
    """Collects a goal's records, following a plan per site (see the module docs).

    Args:
        goal: What to collect.
        plans: How, per site.
        schema: What records are read with (default: the goal's fields).
        keep_pages: Keep the record pages fetched (:attr:`pages`).
        use_api: Ask the API a plan found (``False``: read the pages).
        provenance: Records say where each value came from (``_provenance``: for a page's record, the
            extractor's evidence per field; for an API's, the call, its page and the field each value was
            read from).
        heal: A directory: records are read by a :class:`~wintergrab.extraction.healing.HealingExtractor`
            kept there. It repairs the schema's selectors when the site changes, puts what it cannot decide
            and the values it found with little confidence to a person (``review``), and the first complete
            record of each site is kept there as a regression fixture (``wintergrab heal DIR --check``).
        review: With ``heal``: the review queue (a :class:`~wintergrab.extraction.review.ReviewQueue` or
            its file; by default ``review.jsonl`` in the extractor's directory).
        settings: More :class:`~wintergrab.Spider` settings.
    """

    name = "goal"
    url_rules = True  # skip media, archives and crawler traps

    def __init__(
        self,
        goal: Goal,
        plans: list[SitePlan],
        *,
        schema: Schema | None = None,
        keep_pages: bool = False,
        use_api: bool = True,
        provenance: bool = False,
        heal: str | os.PathLike[str] | None = None,
        review: ReviewQueue | str | os.PathLike[str] | None = None,
        **settings: Any,
    ) -> None:
        self.goal = goal
        # by site (www.shop.example and shop.example are one site: sitemaps mix them)
        self.plans = {registrable_domain(host_of(plan.site)): plan for plan in plans}
        #: The APIs asked, by site; their sites' pages are read only if they fail.
        self.apis = {site: ApiSource.from_dict(plan.api) for site, plan in self.plans.items() if use_api and plan.api}
        pages = [plan for site, plan in self.plans.items() if site not in self.apis]
        rules = [(_url_regex(p), "parse_record") for plan in plans for p in plan.target]
        settings.setdefault("sitemap_rules", rules)
        settings.setdefault(
            "sitemap_urls", [u for plan in pages if plan.strategy == "sitemap" for u in plan.sitemap_urls]
        )
        settings.setdefault("start_urls", [u for plan in pages if plan.strategy == "follow" for u in plan.start_urls])
        # and the hosts of the APIs, which may be elsewhere (api.shop-cdn.example)
        settings.setdefault("allowed_domains", sorted({*self.plans, *(host_of(a.url) for a in self.apis.values())}))
        super().__init__(**settings)
        self.provenance = provenance
        read_with = schema if schema is not None else goal.schema()
        #: The self-healing extractor's directory, with ``heal``.
        self.heal = Path(heal) if heal is not None else None
        self.extractor: Extractor | HealingExtractor
        if self.heal is not None:
            queue = review if review is not None else self.heal / "review.jsonl"
            self.extractor = HealingExtractor(self.heal, read_with, review=queue, provenance=provenance)
        elif review is not None:
            raise ConfigurationError("review needs heal: the review queue is a self-healing extractor's", key="review")
        else:
            self.extractor = Extractor(read_with, provenance=provenance)
        #: Regression fixtures kept in this run (one per site, with ``heal``).
        self.fixtures_kept = 0
        self._fixture_sites: set[str] = set()
        if isinstance(self.extractor, HealingExtractor):
            fixtures = self.extractor.versions.fixtures()
            self._fixture_sites = {registrable_domain(host_of(f.url)) for f in fixtures if f.by == "goal"}
        self.identity = next((f for f in goal.fields if f in ("name", "title")), goal.fields[0])
        #: Record pages where the record's name (or title) was not found.
        self.incomplete = 0
        #: Record pages seen.
        self.record_pages = 0
        #: The record pages fetched, with ``keep_pages``.
        self.pages: list[Response] | None = [] if keep_pages else None
        #: API pages that held records, the records they held, and those without the record's name (or title).
        self.api_pages = 0
        self.api_records = 0
        self.api_incomplete = 0
        #: Per API site: the records its answers held, and how many its first answer said it has.
        self.api_read: Counter[str] = Counter()
        self.api_total: dict[str, int] = {}
        #: What happened to an API that failed or stopped early.
        self.api_notes: list[str] = []
        self._api_asked: set[str] = set()

    @property
    def healer(self) -> HealingExtractor | None:
        """The self-healing extractor (with ``heal``)."""
        return self.extractor if isinstance(self.extractor, HealingExtractor) else None

    def on_close(self, result: CrawlResult) -> None:
        """A self-healing extractor keeps what it learned for the next run."""
        if isinstance(self.extractor, HealingExtractor):
            self.extractor.close()

    # -- the APIs ------------------------------------------------------------------------------ #
    def start_requests(self) -> Iterator[Request | str]:
        """The plans' sitemaps and start pages, and the first page of each API (whose sites' pages wait)."""
        yield from cast("Iterable[Request | str]", super().start_requests())  # the base's is a generator
        for site, source in self.apis.items():
            yield self._api_request(site, source.url, source.body, 1)

    def _api_request(self, site: str, url: str, body: Any, number: int) -> Request:
        source = self.apis[site]
        self._api_asked.add(url + json.dumps(body, sort_keys=True, default=str))
        return Request(
            url,
            method=source.method,
            json=body if source.method == "POST" else None,
            headers={"Accept": "application/json"},
            callback="parse_api",
            errback="api_failed",
            dont_filter=True,  # the same URL with another body (a GraphQL cursor) is another page
            priority=30,
            meta={"api_site": site, "api_body": body, "api_page": number},
        )

    def parse_api(self, response: Response) -> Any:
        """A page of a site's API: its records, and the next page."""
        site, number, body = response.meta["api_site"], response.meta["api_page"], response.meta.get("api_body")
        source = self.apis[site]
        try:
            answer = response.json()
        except ValueError:
            answer = None
        records = records_of(source, answer, self.extractor.schema) if answer is not None else []
        if not records:
            if number == 1:
                why = "its answer holds no records" if answer is not None else "its answer is not JSON"
                yield from self._fall_back(site, why)
            return
        self.api_pages += 1
        self.api_read[site] += len(records)
        asked = response.request.url if response.request is not None else response.url
        if number == 1:
            total = total_of(source, asked, body, answer, len(records))
            if total is not None:
                self.api_total[site] = total
        for record in records:
            if record.get(self.identity) in (None, "", []):
                self.api_incomplete += 1
                continue
            self.api_records += 1
            if self.provenance:
                record["_provenance"] = self._api_provenance(source, asked, number)
            yield record
        following = next_page(source, asked, body, answer, len(records))
        if following is None:
            return
        url, next_body = following
        if number >= MAX_API_PAGES:
            self.api_notes.append(f"{self.plans[site].site}: stopped after {number:,} pages of its API")
            return
        if url + json.dumps(next_body, sort_keys=True, default=str) in self._api_asked:
            self.api_notes.append(f"{self.plans[site].site}: its API gave page {number + 1} as one already read")
            return
        yield self._api_request(site, url, next_body, number + 1)

    def _api_provenance(self, source: ApiSource, url: str, page: int) -> dict[str, Any]:
        """Where an API record's values came from: the call, its page, and the field each value was read from
        (as a page's record says the page and the evidence per field)."""
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")  # (the answer was just received)
        api = {"method": source.method, "page": page, "records": source.path, "fields": dict(source.fields)}
        return {"url": url, "fetched_at": stamp, "extractor": self.extractor.name, "api": api}

    def api_failed(self, request: Request, error: BaseException) -> Any:
        """An API request that failed: a refusal is reported; another failure on the first page leaves the site
        to its pages."""
        site, number = request.meta["api_site"], request.meta["api_page"]
        name = self.plans[site].site
        response = getattr(error, "response", None)
        status = getattr(response, "status", None)
        if status in _REFUSALS or (response is not None and self.is_blocked(response)):
            how = f"HTTP {status}" if status in _REFUSALS else "a bot check"
            note = f"{name}: its API refused ({how}): it is not asked another way"
            self.api_notes.append(note)
            self.logger.warning(note)
            return
        if isinstance(error, RobotsPolicyError):
            why = "robots.txt forbids it"
        else:
            why = f"HTTP {status}" if status is not None else describe(error)
        if number == 1:
            yield from self._fall_back(site, why)
        else:
            self.api_notes.append(f"{name}: its API stopped at page {number} ({why})")

    def _fall_back(self, site: str, reason: str) -> Any:
        plan = self.plans[site]
        note = f"{plan.site}: its API could not be used ({reason}): its pages were read instead"
        self.api_notes.append(note)
        self.logger.warning(note)
        if plan.strategy == "sitemap":
            for url in plan.sitemap_urls:
                yield Request(url, callback="_parse_sitemap", priority=100)
        else:
            for url in plan.start_urls:
                yield Request(url)

    def _plan(self, url: str) -> SitePlan | None:
        return self.plans.get(registrable_domain(host_of(url)))

    def _is_record_page(self, response: Response, plan: SitePlan | None) -> bool:
        if plan is not None and plan.target:
            return plan.is_target(response.url)
        classified = classify_page(response)
        return classified.confidence >= 0.25 and classified.type in self.goal.kind.page_types

    def parse_record(self, response: Response) -> Any:
        """A page that holds a record: extract it."""
        if not response.is_html:
            return
        self.record_pages += 1
        if self.pages is not None:
            self.pages.append(response)
        record = self.extractor.extract(response)
        data = record.to_dict()
        if data.get(self.identity) in (None, "", []):
            self.incomplete += 1
            if self.events.wants("extraction_failed"):
                self.events.emit("extraction_failed", url=response.url, schema=self.extractor.schema.name,
                                 missing=[self.identity])  # fmt: skip
            return
        if not data.get("url"):
            data["url"] = response.url
        healer = self.healer
        if healer is not None:
            site = registrable_domain(host_of(response.url))
            if site not in self._fixture_sites:  # a site's first complete record: what later versions must read
                values = {k: v for k, v in record.data.items() if not k.startswith("_") and v not in (None, "", [], {})}
                healer.versions.add_fixture(response.url, response.body, values, by="goal")
                self._fixture_sites.add(site)
                self.fixtures_kept += 1
        yield data

    def parse(self, response: Response) -> Any:
        """A page of a ``follow`` plan: its record if it has one, and the links that lead to more."""
        if not response.is_html:
            return
        plan = self._plan(response.url)
        if self._is_record_page(response, plan):
            yield from self.parse_record(response)
            if plan is not None and plan.sections:
                return  # a record page leads out of the goal's sections ("related items"), not to more of them
        wander = plan is None or not plan.follow
        for link in response.links(same_domain=True):
            if plan is not None and plan.is_target(link):
                yield response.follow(link, priority=20)  # record pages first: a limit is reached sooner
            elif wander or plan.is_followed(link):  # type: ignore[union-attr]
                yield response.follow(link)
        next_url = response.next_page()
        if next_url:
            yield response.follow(next_url, priority=10)  # then more of the listing


@dataclass
class GoalResult:
    """What running a plan gave: the records (when kept), the crawl's result, and counts."""

    plan: GoalPlan
    crawl: CrawlResult | None = None
    records: list[dict[str, Any]] = field(default_factory=list)
    output: str | None = None
    counts: Counter[str] = field(default_factory=Counter)
    found: Counter[str] = field(default_factory=Counter)
    #: The record pages fetched (``run_plan(keep_pages=True)``).
    pages: list[Response] = field(default_factory=list)
    #: What happened to an API that failed or stopped early.
    notes: list[str] = field(default_factory=list)
    #: With ``heal``: the self-healing extractor's directory, its active version, and its review queue's file.
    extractor: str | None = None
    extractor_version: int | None = None
    review: str | None = None

    def summary(self) -> str:
        """The records, the fields they have, and what was left out and why."""
        c = self.counts
        lines = [f"{c['records']:,} record(s)" + (f" -> {self.output}" if self.output else "")]
        if c["export_errors"]:
            lines.append(output_failures(c["export_errors"], c["not_written"], self.output or "-"))
        if c["records"] and self.found:
            share = ", ".join(f"{f} {n / c['records']:.0%}" for f, n in self.found.items())
            lines.append(f"fields found: {share}")
        dropped = []
        if c["filtered"]:
            dropped.append(f"{c['filtered']:,} not meeting the conditions")
        if c["duplicates"]:
            dropped.append(f"{c['duplicates']:,} duplicate(s)")
        if c["incomplete"]:
            dropped.append(f"{c['incomplete']:,} page(s) without a {self.plan.goal.kind.default_fields[0]}")
        if c["api_incomplete"]:
            dropped.append(f"{c['api_incomplete']:,} API record(s) without a {self.plan.goal.kind.default_fields[0]}")
        if dropped:
            lines.append("left out: " + "; ".join(dropped))
        if c["api_pages"]:
            lines.append(f"{c['api_records']:,} record(s) from {c['api_pages']:,} page(s) of the site's API")
        lines.append(
            f"{c['pages']:,} page(s) fetched"
            + (f", {c['record_pages']:,} with a record" if c["record_pages"] or not c["api_pages"] else "")
            + (f", {c['browser_pages']:,} in a browser" if c["browser_pages"] else "")
            + f", {c['errors']:,} error(s)"
        )
        if self.extractor:
            healing = f"self-healing extractor {self.extractor}: version {self.extractor_version}"
            if c["repairs"]:
                healing += f", {c['repairs']:,} repair(s) this run"
            if c["fixtures"]:
                healing += f", {c['fixtures']:,} regression fixture(s) kept"
            lines.append(healing)
        if c["questions"]:
            lines.append(f"{c['questions']:,} question(s) waiting for you: wintergrab review {self.review}")
        lines.extend(f"note: {note}" for note in self.notes)
        return "\n".join(lines)


def run_plan(
    plan: GoalPlan,
    output: str | None = None,
    *,
    max_pages: int | None = None,
    keep_items: bool | None = None,
    keep_pages: bool = False,
    use_api: bool = True,
    log_level: str | None = "INFO",
    progress: bool | None = None,
    provenance: bool = False,
    heal: str | os.PathLike[str] | None = None,
    review: ReviewQueue | str | os.PathLike[str] | None = None,
    **settings: Any,
) -> GoalResult:
    """Collect ``plan``'s records into ``output`` (``.jsonl``, ``.csv``, ``.json``...; see the module docs).

    Records are read with the plan's schema (:meth:`~wintergrab.goals.GoalPlan.extraction_schema`).

    Args:
        max_pages: Stop after this many pages.
        keep_items: Keep the records in memory (``result.records``); by default when there is no ``output``.
        keep_pages: Keep the record pages fetched (``result.pages``).
        use_api: Collect from the API the plan found, where it found one (``False``: read the pages).
        provenance: Records say where each value came from (``_provenance``).
        heal: A directory for a self-healing extractor (see :class:`GoalSpider`): selectors repaired when
            the site changes, questions for a person in its review queue, a regression fixture per site.
            ``result.summary()`` says what it did; ``result.counts``: ``repairs``, ``questions``, ``fixtures``.
        review: With ``heal``: the review queue's file (by default ``review.jsonl`` in the directory).
        settings: More :class:`~wintergrab.Spider` settings (``concurrency``, ``cache``, ``obey_robots_txt``...);
            ``optimize=False`` fetches every page the plan leads to (see :mod:`wintergrab.spider.optimizer`).
    """
    goal = plan.goal
    result = GoalResult(plan=plan, output=output)
    keep = keep_items if keep_items is not None else output is None
    sites = [site for site in plan.sites if site.allowed or not settings.get("obey_robots_txt", True)]
    if not sites:
        return result  # robots.txt keeps crawlers out of every site
    stages: list[Any] = [Filter(f.expression, name=f"condition {i + 1}") for i, f in enumerate(goal.filters)]
    if goal.dedupe:
        stages.append(Deduplicate(key="url", name="duplicates"))
    pipeline = Pipeline(stages, name="goal")
    options: dict[str, Any] = dict(settings)
    options.setdefault("optimize", True)  # skip what gives nothing, drop parameters that change nothing
    if options.get("record") or options.get("run_registry"):
        # a replay needs no survey, nor the plan's schema file
        recipe = plan.to_dict(embed_schema=True)
        if not use_api:  # the replay reads the pages, as this run did
            for site in recipe["sites"]:
                site["api"] = None
        options.setdefault("run_recipe", {"goal_plan": recipe})
    if any(site.fetch == "adaptive" for site in sites):
        strategy = FetchStrategy()
        for site in sites:
            for url in site.sample.get("javascript_urls", []):
                strategy.record(url, "http", False)  # what the sample showed: those pages need a browser
        options.setdefault("adaptive_fetch", strategy)
    if goal.monitor and output:
        from ..spider.exporters import output_scheme

        if output_scheme(output) is None:
            options.setdefault("history", str(Path(output).with_suffix(".history")))
        else:  # a database: the history in the workspace, named after the goal's records
            options.setdefault("history", str(Path(".wintergrab") / f"{goal.kind.name}.history"))
    if goal.limit:
        options.setdefault("max_items", goal.limit)
    started = time.time()
    spider = GoalSpider(
        goal,
        sites,
        schema=plan.extraction_schema() if plan.schema is not None else None,
        keep_pages=keep_pages,
        use_api=use_api,
        provenance=provenance,
        heal=heal,
        review=review,
        output=output,
        keep_items=keep,
        max_pages=max_pages,
        pipelines=[*([pipeline] if stages else []), *options.pop("pipelines", ())],  # the goal's first
        log_level=log_level,
        progress=progress,
        **options,
    )
    crawl = spider.run(resume=False)
    result.crawl = crawl
    result.pages = spider.pages or []
    result.records = list(crawl.items) if keep else []
    counts = result.counts
    counts["records"] = int(crawl.stats.get("items", 0))
    counts["pages"] = int(crawl.stats.get("pages", 0))
    counts["browser_pages"] = int(crawl.stats.get("browser_pages", 0))
    counts["errors"] = int(crawl.stats.get("errors", 0))
    counts["export_errors"] = int(crawl.stats.get("export_errors", 0))
    counts["not_written"] = int(crawl.stats.get("items_not_written", 0))
    counts["record_pages"] = spider.record_pages
    counts["incomplete"] = spider.incomplete
    counts["api_pages"] = spider.api_pages
    counts["api_records"] = spider.api_records
    counts["api_incomplete"] = spider.api_incomplete
    healer = spider.healer
    if healer is not None:
        directory = str(healer.versions.directory)
        result.extractor, result.extractor_version = directory, healer.versions.active_number
        history = healer.versions.history()
        counts["repairs"] = sum(
            1
            for e in history
            if e.get("event") == "repair" and e.get("outcome") == "applied" and e.get("at", 0) >= started
        )
        counts["fixtures"] = spider.fixtures_kept
        if healer.review is not None:
            result.review = str(healer.review.path)
            counts["questions"] = sum(
                1 for item in healer.review.pending() if item.details.get("extractor") == directory
            )
    result.notes = list(spider.api_notes)
    cut_short = crawl.status != "finished" or bool(goal.limit and counts["records"] >= goal.limit)
    for site, total in spider.api_total.items():
        read = spider.api_read[site]
        if read < total and not cut_short:
            result.notes.append(f"{spider.plans[site].site}: its API said it has {total:,} records; {read:,} were read")
    for stage in pipeline:
        counts["duplicates" if isinstance(stage, Deduplicate) else "filtered"] += stage.stats.get("dropped", 0)
    fields = [f for f in goal.fields]
    if keep:
        for record in result.records:
            result.found.update(f for f in fields if record.get(f) not in (None, "", []))
    elif output:
        result.found = _fields_in(output, fields)
    return result


def _fields_in(output: str, fields: list[str]) -> Counter[str]:
    """How many records of a JSON Lines output have each field."""
    found: Counter[str] = Counter()
    path = Path(output)
    if path.suffix.lower() not in (".jsonl", ".ndjson") or not path.exists():
        return found
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            found.update(f for f in fields if record.get(f) not in (None, "", []))
    return found
