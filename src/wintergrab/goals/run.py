"""Running a plan: a spider per site that collects the goal's records.

::

    result = plan.run("laptops.jsonl")      # or run_plan(plan, "laptops.jsonl")
    print(result.summary())

Each site's :class:`GoalSpider` follows its :class:`~wintergrab.goals.plan.SitePlan`:
the record pages listed in the sitemaps, or links followed from the start
pages through listings and pagination. On each record page the extraction
engine fills the goal's fields (with their confidence, ``_confidence``); a
data :class:`~wintergrab.data.Pipeline` keeps the records that meet the
goal's conditions and drops duplicates (the same URL: pages that name their
canonical URL count once). Pages whose content needs JavaScript go to a
browser when the plan says so, record pages are fetched before more listing
pages, and the crawl stops at the goal's limit. The crawl learns as it goes
(:mod:`wintergrab.spider.optimizer`): URL patterns that give nothing are
skipped, and query parameters that change nothing are dropped.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..data.pipeline import Deduplicate, Filter, Pipeline
from ..extraction import Extractor
from ..fetchers.resources import registrable_domain
from ..fetchers.response import Response
from ..fetchers.strategy import FetchStrategy
from ..intel.classify import classify_page
from ..spider import CrawlResult, Spider
from ..utils import host_of
from .goal import Goal

if TYPE_CHECKING:
    from .plan import GoalPlan, SitePlan

__all__ = ["GoalResult", "GoalSpider", "run_plan"]


def _url_regex(pattern: str) -> str:
    """A path pattern (``/p/*``, ``/books/**``) as a regex for whole URLs."""
    parts = [s for s in pattern.split("/") if s]
    anywhere = bool(parts) and parts[-1] == "**"
    body = "/".join("[^/?#]+" if part == "*" else re.escape(part) for part in (parts[:-1] if anywhere else parts))
    tail = r"(?:/[^?#]*)?" if anywhere else "/?"
    return rf"^https?://[^/]+/{body}{tail}(?:[?#].*)?$"


class GoalSpider(Spider):
    """Collects a goal's records, following a plan per site (see the module docs)."""

    name = "goal"
    url_rules = True  # skip media, archives and crawler traps

    def __init__(self, goal: Goal, plans: list[SitePlan], **settings: Any) -> None:
        self.goal = goal
        # by site (www.shop.example and shop.example are one site: sitemaps mix them)
        self.plans = {registrable_domain(host_of(plan.site)): plan for plan in plans}
        rules = [(_url_regex(p), "parse_record") for plan in plans for p in plan.target]
        settings.setdefault("sitemap_rules", rules)
        settings.setdefault(
            "sitemap_urls", [u for plan in plans if plan.strategy == "sitemap" for u in plan.sitemap_urls]
        )
        settings.setdefault("start_urls", [u for plan in plans if plan.strategy == "follow" for u in plan.start_urls])
        settings.setdefault("allowed_domains", sorted(self.plans))
        super().__init__(**settings)
        self.extractor = Extractor(goal.schema())
        self.identity = next((f for f in goal.fields if f in ("name", "title")), goal.fields[0])
        #: Record pages where the record's name (or title) was not found.
        self.incomplete = 0
        #: Record pages seen.
        self.record_pages = 0

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
        record = self.extractor.extract(response)
        data = record.to_dict()
        if data.get(self.identity) in (None, "", []):
            self.incomplete += 1
            return
        if not data.get("url"):
            data["url"] = response.url
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

    def summary(self) -> str:
        """The records, the fields they have, and what was left out and why."""
        c = self.counts
        lines = [f"{c['records']:,} record(s)" + (f" -> {self.output}" if self.output else "")]
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
        if dropped:
            lines.append("left out: " + "; ".join(dropped))
        lines.append(
            f"{c['pages']:,} page(s) fetched, {c['record_pages']:,} with a record"
            + (f", {c['browser_pages']:,} in a browser" if c["browser_pages"] else "")
            + f", {c['errors']:,} error(s)"
        )
        return "\n".join(lines)


def run_plan(
    plan: GoalPlan,
    output: str | None = None,
    *,
    max_pages: int | None = None,
    keep_items: bool | None = None,
    log_level: str | None = "INFO",
    progress: bool | None = None,
    **settings: Any,
) -> GoalResult:
    """Collect ``plan``'s records into ``output`` (``.jsonl``, ``.csv``, ``.json``...; see the module docs).

    Args:
        max_pages: Stop after this many pages.
        keep_items: Keep the records in memory (``result.records``); by default when there is no ``output``.
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
        options.setdefault("run_recipe", {"goal_plan": plan.to_dict()})  # a replay needs no survey
    if any(site.fetch == "adaptive" for site in sites):
        strategy = FetchStrategy()
        for site in sites:
            for url in site.sample.get("javascript_urls", []):
                strategy.record(url, "http", False)  # what the sample showed: those pages need a browser
        options.setdefault("adaptive_fetch", strategy)
    if goal.monitor and output:
        options.setdefault("history", str(Path(output).with_suffix(".history")))
    if goal.limit:
        options.setdefault("max_items", goal.limit)
    spider = GoalSpider(
        goal,
        sites,
        output=output,
        keep_items=keep,
        max_pages=max_pages,
        pipelines=[pipeline] if stages else [],
        log_level=log_level,
        progress=progress,
        **options,
    )
    crawl = spider.run(resume=False)
    result.crawl = crawl
    result.records = list(crawl.items) if keep else []
    counts = result.counts
    counts["records"] = int(crawl.stats.get("items", 0))
    counts["pages"] = int(crawl.stats.get("pages", 0))
    counts["browser_pages"] = int(crawl.stats.get("browser_pages", 0))
    counts["errors"] = int(crawl.stats.get("errors", 0))
    counts["record_pages"] = spider.record_pages
    counts["incomplete"] = spider.incomplete
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
