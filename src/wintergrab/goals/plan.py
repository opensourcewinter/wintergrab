"""Plans: where a goal's records are on a site, how to get them, and what it will cost.

::

    goal = parse_goal("Find all laptops under $1000 on shop.example with name, price and rating")
    plan = plan_goal(goal)              # surveys each site: robots.txt, sitemaps, about 30 pages
    print(plan.describe())              # the steps, the estimates and what they rest on
    plan.save("laptops.plan.json")      # inspect it, edit it, run it later
    result = plan.run("laptops.jsonl")

For each site the planner reads robots.txt and the sitemaps and samples pages
(:func:`~wintergrab.intel.survey.survey_site`, preferring the sitemap URLs that
look like the goal's pages). On the sample it learns which pages hold the
records (classified as, say, product pages, or giving a record with the goal's
fields), their URL patterns, the pages that list them, whether they need a
browser, and how well the fields come out. Then it chooses:

* **sitemap**: the sitemaps list pages of the records' pattern, so those are
  fetched directly (the whole site's worth, and a known number);
* **follow**: otherwise, links are followed from the start page (or from the
  sections the goal names: "laptops") through listing and pagination pages to
  the records' pages; the count is then a lower bound.

The estimates (requests, time, bandwidth, browser pages, records, CPU,
storage) come with the measurements and assumptions behind them
(``estimate.basis``): the sample's latency, page sizes and extraction time,
robots.txt's crawl delay, and the spider's concurrency.
"""

from __future__ import annotations

import importlib.util
import json
import math
import re
import statistics
import time
import urllib.robotparser
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ..data.expressions import Expression
from ..errors import ConfigurationError
from ..extraction import Extractor, PageContext
from ..fetchers.strategy import needs_javascript
from ..intel.classify import classify_page, classify_url
from ..spider import Spider
from .goal import Goal

if TYPE_CHECKING:
    from ..intel.survey import SiteSurvey
    from .run import GoalResult

__all__ = ["Estimate", "GoalPlan", "SitePlan", "path_pattern", "plan_goal"]

_BROWSER_SECONDS = 2.0  # assumed time per page in the browser when none was measured
_SURE = 0.25  # page classifications less confident than this are left out
_GRID_SCORE = 1.0  # detect_records() score of a grid of record cards (breadcrumbs and menus score far less)
_NOT_RECORDS = frozenset({"homepage", "search", "login", "error", "contact", "archive", "category", "listing"})
_FILENAMES = re.compile(r"(?:index|default|home|details?|overview|main)(?:\.\w{2,5})?$", re.I)


def path_pattern(urls: list[str]) -> str:
    """A path pattern covering ``urls``: segments they share stay, the others become ``*``.

    One URL: its last segment is taken for the record's id (``/p/phone-x`` -> ``/p/*``), or the one
    before a file name (``/book/b-1/index.html`` -> ``/book/*/index.html``).
    """
    paths = [[s for s in urlsplit(u).path.split("/") if s] for u in urls]
    if not paths:
        return "/"
    size = Counter(len(p) for p in paths).most_common(1)[0][0]
    same = [p for p in paths if len(p) == size]
    if not size:
        return "/"
    if len(same) == 1:
        segments = list(same[0])
        at = size - 2 if size >= 2 and _FILENAMES.fullmatch(segments[-1]) else size - 1
        segments[at] = "*"
    else:
        segments = [parts[0] if len(set(parts)) == 1 else "*" for parts in zip(*same, strict=True)]
    return "/" + "/".join(segments)


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    """``*`` is one path segment, ``**`` any number of them (``/books/**``: anything under /books)."""
    parts = [s for s in pattern.split("/") if s]
    if parts and parts[-1] == "**":
        body = "/".join(re.escape(part) if part != "*" else "[^/]+" for part in parts[:-1])
        return re.compile(f"^/{body}(?:/.*)?$", re.I)
    body = "/".join("[^/]+" if part == "*" else re.escape(part) for part in parts)
    return re.compile(f"^/{body}/?$", re.I)


def _matches(url: str, patterns: list[str]) -> bool:
    path = urlsplit(url).path or "/"
    return any(_pattern_regex(p).match(path) for p in patterns)


def _group_patterns(urls: list[str], limit: int = 5, *, generalize_one: bool = True) -> list[str]:
    """Patterns for URLs of several shapes: one per (number of segments, first segment) group. A group
    of one URL is generalized (its id segment becomes ``*``) unless ``generalize_one`` is false."""
    groups: dict[tuple[int, str], list[str]] = {}
    for url in urls:
        segments = [s for s in urlsplit(url).path.split("/") if s]
        groups.setdefault((len(segments), segments[0] if segments else ""), []).append(url)
    ordered = sorted(groups.values(), key=len, reverse=True)
    patterns = []
    for group in ordered[:limit]:
        if len(set(group)) == 1 and not generalize_one:
            patterns.append(urlsplit(group[0]).path.rstrip("/") or "/")
        else:
            patterns.append(path_pattern(group))
    return list(dict.fromkeys(patterns))


def _under(pattern: str, sections: list[str]) -> bool:
    return any(pattern == s or pattern.startswith(s.rstrip("/") + "/") for s in sections)


@dataclass
class Estimate:
    """What a plan will cost, and what the numbers rest on (``basis``).

    Attributes:
        pages: Pages holding records to fetch.
        exact: ``pages`` is the number the sitemaps list (else a lower bound: the pages seen so far).
        listing_pages: Pages fetched to find them (listings, pagination).
        requests: Every request, robots.txt and sitemaps included.
        browser_pages: Pages expected to need a browser.
        bytes: Bytes to download.
        seconds: Time the crawl should take.
        records: Records expected after the conditions.
        cpu_seconds: Processor time for parsing and extraction.
        storage_bytes: Size of the records as JSON Lines.
    """

    pages: int = 0
    exact: bool = False
    listing_pages: int = 0
    requests: int = 0
    browser_pages: int = 0
    bytes: int = 0
    seconds: float = 0.0
    records: int = 0
    cpu_seconds: float = 0.0
    storage_bytes: int = 0
    basis: list[str] = field(default_factory=list)

    def __add__(self, other: Estimate) -> Estimate:
        return Estimate(
            pages=self.pages + other.pages,
            exact=self.exact and other.exact,
            listing_pages=self.listing_pages + other.listing_pages,
            requests=self.requests + other.requests,
            browser_pages=self.browser_pages + other.browser_pages,
            bytes=self.bytes + other.bytes,
            seconds=max(self.seconds, other.seconds),  # one crawl, the sites side by side
            records=self.records + other.records,
            cpu_seconds=self.cpu_seconds + other.cpu_seconds,
            storage_bytes=self.storage_bytes + other.storage_bytes,
            basis=self.basis + other.basis,
        )

    def describe(self) -> str:
        pages = f"{self.pages:,}" if self.exact else f"at least {self.pages:,}"
        lines = [
            f"pages with records: {pages}"
            + (f", plus {self.listing_pages:,} listing pages" if self.listing_pages else ""),
            f"requests: {self.requests:,}"
            + (f" ({self.browser_pages:,} in a browser)" if self.browser_pages else " (none in a browser)"),
            f"download: {_size(self.bytes)}; time: {_duration(self.seconds)}; CPU: {_duration(self.cpu_seconds)}",
            f"records: about {self.records:,} ({_size(self.storage_bytes)} as JSON Lines)",
        ]
        return "\n".join(lines)


def _size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1000
    return f"{n:,.1f} GB"  # pragma: no cover


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    if seconds < 172_800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


@dataclass
class SitePlan:
    """How to get a goal's records from one site (see the module docs).

    Attributes:
        site: The site (its start URL).
        strategy: ``"sitemap"`` or ``"follow"``.
        start_urls: Where the crawl starts (``follow``).
        sitemap_urls: The sitemaps to read (``sitemap``).
        target: Path patterns of the pages holding records (``/p/*``).
        follow: Path patterns of the pages to go through to find them (``follow``).
        sections: The parts of the site the goal is about (``/books``); record pages there lead no further.
        page_types: The page types that hold records.
        fetch: ``"http"``, or ``"adaptive"`` when some pages need a browser.
        allowed: robots.txt lets the crawl in.
        crawl_delay: robots.txt's crawl delay, seconds.
        sample: What the sampled pages showed (pages, record pages, records, fields found).
        estimate: What it will cost.
        steps: The plan in words.
        warnings: What may go wrong.
    """

    site: str
    strategy: str
    start_urls: list[str] = field(default_factory=list)
    sitemap_urls: list[str] = field(default_factory=list)
    target: list[str] = field(default_factory=list)
    follow: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    page_types: list[str] = field(default_factory=list)
    fetch: str = "http"
    allowed: bool = True
    crawl_delay: float | None = None
    sample: dict[str, Any] = field(default_factory=dict)
    estimate: Estimate = field(default_factory=Estimate)
    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    js_patterns: list[str] = field(default_factory=list)

    def is_target(self, url: str) -> bool:
        return _matches(url, self.target) if self.target else False

    def is_followed(self, url: str) -> bool:
        return _matches(url, self.follow) if self.follow else False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SitePlan:
        data = dict(data)
        estimate = Estimate(**data.pop("estimate", {}))
        known = {f for f in cls.__dataclass_fields__}
        return cls(estimate=estimate, **{k: v for k, v in data.items() if k in known})


@dataclass
class GoalPlan:
    """A goal and a plan per site: see :func:`plan_goal`."""

    goal: Goal
    sites: list[SitePlan]
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def estimate(self) -> Estimate:
        total = Estimate(exact=True)
        for site in self.sites:
            total = total + site.estimate
        return total

    def describe(self, *, goal: bool = True) -> str:
        """The goal as understood (unless ``goal=False``), then each site's steps and estimates, and
        the warnings."""
        lines = ["Goal: " + self.goal.describe().replace("\n", "\n      ")] if goal else []
        for plan in self.sites:
            if lines:
                lines.append("")
            lines.append(f"{plan.site}  ({plan.strategy})")
            lines.extend(f"  {i}. {step}" for i, step in enumerate(plan.steps, 1))
            lines.append("  Estimates:")
            lines.extend("    " + line for line in plan.estimate.describe().splitlines())
            lines.extend(f"  warning: {w}" for w in plan.warnings)
        if len(self.sites) > 1:
            lines.append("")
            lines.append("All sites:")
            lines.extend("  " + line for line in self.estimate.describe().splitlines())
        return "\n".join(lines)

    def explain(self) -> str:
        """What each estimate rests on."""
        return "\n".join(f"{plan.site}: {basis}" for plan in self.sites for basis in plan.estimate.basis)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "created": self.created,
            "goal": self.goal.to_dict(),
            "sites": [asdict(s) for s in self.sites],
        }

    def save(self, path: str | Path) -> None:
        """Write the plan as JSON (edit it, and run it with :meth:`load` and :meth:`run`)."""
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> GoalPlan:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"cannot read the plan {path}: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoalPlan:
        """A plan from its :meth:`to_dict` form."""
        goal = Goal.from_dict(data.get("goal") or {})
        return cls(
            goal=goal, sites=[SitePlan.from_dict(s) for s in data.get("sites") or ()], created=data.get("created", "")
        )

    def run(self, output: str | None = None, **options: Any) -> GoalResult:
        """Collect the records (see :func:`~wintergrab.goals.run.run_plan`)."""
        from .run import run_plan

        return run_plan(self, output, **options)


# ---------------------------------------------------------------------------------------------- #
# planning
# ---------------------------------------------------------------------------------------------- #
def plan_goal(
    goal: Goal,
    *,
    sample: int = 30,
    obey_robots: bool = True,
    browser: bool = False,
    timeout: float = 20.0,
    surveys: dict[str, SiteSurvey] | None = None,
    log_level: str | None = "WARNING",
) -> GoalPlan:
    """Plan ``goal`` for each of its sites (see the module docs).

    Args:
        sample: Pages to look at per site (robots.txt and sitemaps aside).
        obey_robots: Obey robots.txt while sampling (the plan says what it allows either way).
        browser: Sample with a browser (slower; finds the API calls pages make).
        surveys: Surveys already made, by site URL (they are not made again).
    """
    if not goal.sites:
        raise ConfigurationError("the goal names no site: add one (a URL or a domain such as shop.example)")
    from ..intel.survey import survey_site

    plans = []
    kind = goal.kind
    for site in goal.sites:
        survey = (surveys or {}).get(site)
        if survey is None:
            section = urlsplit(site).path.rstrip("/")
            words = _scope_words(goal)

            def prefer(url: str, section: str = section, words: list[str] = words) -> int:
                """How much a page is worth sampling: in the part of the site given, and like the goal's
                records (or, less, their listings) or its words."""
                path = urlsplit(url).path
                score = 2 if section and (path == section or path.startswith(section + "/")) else 0
                seen_as = classify_url(url).type
                score += 2 if seen_as in kind.page_types else 1 if seen_as in kind.listing_types else 0
                return score + (1 if any(w in url.lower() for w in words) else 0)

            survey = survey_site(
                site,
                pages=sample,
                obey_robots=obey_robots,
                browser=browser,
                timeout=timeout,
                keep_pages=True,
                prefer=prefer,
                log_level=log_level,
            )
        plans.append(_plan_site(goal, survey))
    return GoalPlan(goal=goal, sites=plans)


def _scope_words(goal: Goal) -> list[str]:
    words = []
    for phrase in goal.scope:
        for word in phrase.split():
            words.append(word)
            if word.endswith("s") and len(word) > 3:
                words.append(word[:-1])  # "laptops" -> "laptop"
    return words


def _robots(survey: SiteSurvey) -> urllib.robotparser.RobotFileParser | None:
    if not survey.robots_text:
        return None
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(survey.robots_text.splitlines())
    return parser


def _plan_site(goal: Goal, survey: SiteSurvey) -> SitePlan:
    kind = goal.kind
    schema = goal.schema()
    extractor = Extractor(schema)
    identity = next((f for f in goal.fields if f in ("name", "title")), goal.fields[0])
    wanted = [f for f in goal.fields if f not in ("url", "currency")]
    conditions = [Expression(f.expression) for f in goal.filters]
    warnings: list[str] = []
    plan = SitePlan(site=survey.url, strategy="follow", page_types=list(kind.page_types))

    # -- what the sample shows ------------------------------------------------------------------ #
    records: list[dict[str, Any]] = []
    record_urls: list[str] = []
    listing_urls: list[str] = []
    js_urls: list[str] = []
    found: Counter[str] = Counter()
    extract_seconds: list[float] = []
    parse_seconds: list[float] = []
    sizes = [len(page.body) for page in survey.pages]
    for page in survey.pages:
        started = time.perf_counter()
        ctx = PageContext(page)
        classified = classify_page(ctx)
        page_type = classified.type if classified.confidence >= _SURE else "unknown"  # a weak verdict decides nothing
        # A list of records: classified as one, or a grid of cards (a strong repeating group; a
        # breadcrumb or a small table repeats too, weakly). The classifier's record verdict wins.
        lists = page_type in kind.listing_types or (
            page_type not in kind.page_types
            and any(len(g.elements) >= 3 and g.score >= _GRID_SCORE for g in ctx.selector.detect_records())
        )
        parse_seconds.append(time.perf_counter() - started)
        if page.source != "browser" and needs_javascript(page):
            js_urls.append(page.url)
        if lists:
            listing_urls.append(page.url)
        started = time.perf_counter()
        record = extractor.extract(ctx)
        extract_seconds.append(time.perf_counter() - started)
        values = {k: v for k, v in record.data.items() if v not in (None, "", [])}
        filled = [f for f in wanted if f in values]
        # A record page: classified as one; or, for a page that lists nothing, one giving a record: its
        # name and half the other fields (every page has a title, so the name alone proves nothing).
        others = [f for f in wanted if f != identity]
        gives = identity in values and (not others or 2 * sum(1 for f in others if f in values) >= len(others))
        if page_type in kind.page_types or (page_type not in _NOT_RECORDS and not lists and gives):
            record_urls.append(page.url)
            records.append({"url": page.url, **values})
            found.update(filled)
    # -- where the records are ---------------------------------------------------------------- #
    plan.target = _group_patterns(record_urls)
    plan.js_patterns = _group_patterns(js_urls) if js_urls else []
    listed = [e.loc for e in survey.sitemap_entries]
    listed_targets = [u for u in listed if _matches(u, plan.target)] if plan.target else []
    if not plan.target and listed:  # nothing sampled: the URL rules may know
        listed_targets = [u for u in listed if classify_url(u).type in kind.page_types]
        plan.target = _group_patterns(listed_targets)
    sections = _scope_sections(goal, survey)
    start_path = urlsplit(survey.url).path.rstrip("/")
    if start_path and not sections:
        sections = [start_path]  # a site given with a path: that part of the site
    if goal.scope and not sections:
        warnings.append(
            f"no section of the site is called {' or '.join(repr(s) for s in goal.scope)}: the plan covers all its {kind.name}s"
        )
    if sections:
        plan.sections = sections
        scoped = [p for p in plan.target if _under(p, sections)]
        plan.target = scoped or plan.target  # records may live elsewhere (/p/*), reached from the section
        listed_targets = [u for u in listed_targets if _under(urlsplit(u).path, sections)]
    # what the sample says of the record pages the plan is about
    in_scope = [r for r in records if not plan.target or _matches(r["url"], plan.target)] or records
    found = Counter(f for r in in_scope for f in wanted if f in r)
    passing = [r for r in in_scope if all(_holds(c, r) for c in conditions)]
    pass_rate = len(passing) / len(in_scope) if in_scope else 1.0
    plan.sample = {
        "pages": len(survey.pages),
        "record_pages": len(in_scope),
        "listing_pages": len(listing_urls),
        "javascript_pages": len(js_urls),
        "javascript_urls": js_urls[:20],
        "fields": {f: found[f] for f in wanted},
        "passing": len(passing),
        "examples": in_scope[:3],
    }
    records = in_scope
    robots = _robots(survey)
    robots_info = (survey.profile.crawlability or {}).get("robots") or {}
    plan.crawl_delay = robots_info.get("crawl_delay")
    if robots_info.get("disallow_all"):
        plan.allowed = False
        warnings.append("robots.txt asks crawlers to stay out of the whole site")
    if robots is not None and listed_targets:
        refused = sum(1 for u in listed_targets[:500] if not robots.can_fetch("*", u))
        if refused:
            warnings.append(
                f"robots.txt forbids {refused} of the first {min(500, len(listed_targets))} record pages: they will be skipped"
            )
    if listed_targets:
        plan.strategy = "sitemap"
        plan.sitemap_urls = list(survey.sitemaps.roots)
        if sections and not plan.target:
            plan.target = [f"{s.rstrip('/')}/**" for s in sections]
    else:
        plan.strategy = "follow"
        plan.start_urls = [_section_url(survey, s) for s in sections] if sections else [survey.url]
        listings = [u for u in listing_urls if not sections or _under(urlsplit(u).path.rstrip("/") or "/", sections)]
        within = [p for p in _group_patterns(listings, generalize_one=False) if not _under(p, sections)]
        plan.follow = within + [f"{s.rstrip('/')}/**" for s in sections]  # "/books/**" covers /books/page-2.html
    if not record_urls:
        warnings.append(
            f"none of the {len(survey.pages)} sampled pages looked like a {kind.name} page: the crawl will classify every page it visits"
        )
    for f in wanted:
        if records and found[f] == 0:
            warnings.append(f"{f!r} was not found on any sampled {kind.name} page")
    if js_urls:
        plan.fetch = "adaptive"
        if importlib.util.find_spec("playwright") is None:
            warnings.append(
                'some pages need a browser, and Playwright is not installed: pip install "wintergrab[browser]"'
            )
    if survey.sitemaps.truncated:
        warnings.append("the sitemaps were read in part (10 sitemaps, 50,000 pages): there are more pages than counted")

    # -- what it costs ------------------------------------------------------------------------ #
    plan.estimate = _estimate(
        goal, plan, survey, sizes, parse_seconds, extract_seconds, records, pass_rate, listed_targets
    )
    plan.steps = _steps(goal, plan, survey, listed_targets, found, sections)
    plan.warnings = warnings
    return plan


def _section_url(survey: SiteSurvey, path: str) -> str:
    """A real URL for a section path (``/books`` may only answer as ``/books/``): the start URL, or one
    of the sampled pages or their links, when their path is the section's."""
    candidates = [survey.url, *(page.url for page in survey.pages)]
    for page in survey.pages:
        if page.is_html:
            candidates.extend(page.links(same_domain=True))
    for url in candidates:
        if (urlsplit(url).path.rstrip("/") or "/") == path.rstrip("/"):
            return url.split("#")[0]
    return survey.origin + path


def _holds(condition: Expression, record: dict[str, Any]) -> bool:
    try:
        return bool(condition(record))
    except Exception:
        return False


def _scope_sections(goal: Goal, survey: SiteSurvey) -> list[str]:
    """The sections the goal's scope words name: the highest ones (not the pages inside them)."""
    topology = survey.profile.topology
    if topology is None or not goal.scope:
        return []
    nodes: list[Any] = []
    for phrase in goal.scope:
        words = [phrase, *_scope_words(Goal(text="", entity=goal.entity, fields=[], scope=[phrase]))]
        for word in dict.fromkeys(words):
            nodes.extend(n for n in topology.find(word) if "{" not in n.path and n.path != "/")
    kept: list[str] = []
    for node in sorted(nodes, key=lambda n: (n.path.count("/"), -n.urls)):
        if node.urls >= 2 and not any(node.path == k or node.path.startswith(k + "/") for k in kept):
            kept.append(node.path)
    return kept[:5]


def _estimate(
    goal: Goal,
    plan: SitePlan,
    survey: SiteSurvey,
    sizes: list[int],
    parse_seconds: list[float],
    extract_seconds: list[float],
    records: list[dict[str, Any]],
    pass_rate: float,
    listed_targets: list[str],
) -> Estimate:
    basis = []
    sample = max(1, len(survey.pages))
    if plan.strategy == "sitemap":
        pages, exact = len(listed_targets), not survey.sitemaps.truncated
        basis.append(f"record pages: {pages:,} listed in the sitemaps match {', '.join(plan.target)}")
    else:
        topology = survey.profile.topology
        known = 0
        if topology is not None and plan.target:
            known = sum(1 for n in topology.root.walk() if n is not topology.root and _pattern_is(n.path, plan.target))
        seen = int(survey.stats.get("pages", 0))
        linked = _linked_targets(survey, plan.target)
        pages, exact = max(plan.sample["record_pages"], linked, known), False
        basis.append(
            f"record pages: at least {pages:,} (seen or linked in the {seen} sampled pages); the site may have more"
        )
    if goal.limit and pass_rate > 0:
        needed = math.ceil(goal.limit / max(pass_rate, 0.05))
        if needed < pages:
            basis.append(f"the goal wants {goal.limit:,} records: about {needed:,} record pages should do")
            pages = needed
    listing = 0
    if plan.strategy == "follow":
        per_listing = _targets_per_listing(survey, plan.target)
        listing = math.ceil(pages / per_listing) if per_listing else max(1, plan.sample["listing_pages"])
        basis.append(
            f"listing pages: about one per {per_listing or 'unknown number of'} record pages (sampled listings)"
        )
    extra = 1 + (survey.sitemaps.sitemaps if plan.strategy == "sitemap" else 0)
    requests = pages + listing + extra
    js_share = plan.sample["javascript_pages"] / sample
    browser_pages = round((pages + listing) * js_share)
    if js_share:
        basis.append(f"browser pages: {js_share:.0%} of the sampled pages needed JavaScript")
    average = statistics.fmean(sizes) if sizes else 50_000.0
    basis.append(f"download: {_size(average)} per page on average (sampled pages)")
    latency = survey.profile.average_latency or 1.0
    concurrency = int(getattr(Spider, "concurrency_per_domain", 8) or 8)
    rate = concurrency / latency
    rate_basis = f"{concurrency} requests at a time and {latency:.2f} s per response (sampled)"
    if plan.crawl_delay:
        rate = min(rate, 1 / plan.crawl_delay)
        rate_basis = f"robots.txt's crawl delay of {plan.crawl_delay:g} s"
    browser_rate = min(concurrency, 8) / _BROWSER_SECONDS
    seconds = (requests - browser_pages) / rate + (browser_pages / browser_rate if browser_pages else 0.0)
    basis.append(f"time: {rate:.2f} requests per second ({rate_basis})")
    if browser_pages:
        basis.append(
            f"time: browser pages assumed to take {_BROWSER_SECONDS:g} s each, {min(concurrency, 8)} at a time"
        )
    per_page = (statistics.fmean(parse_seconds) if parse_seconds else 0.003) + (
        statistics.fmean(extract_seconds) if extract_seconds else 0.007
    )
    basis.append(f"CPU: {per_page * 1000:.1f} ms per page to classify and extract (measured on the sample)")
    record_rate = (len(records) / plan.sample["record_pages"]) if plan.sample["record_pages"] else 1.0
    expected = round(pages * min(1.0, record_rate) * pass_rate)
    if goal.limit:
        expected = min(expected, goal.limit)
    if records:
        basis.append(f"records: {pass_rate:.0%} of the sampled records meet the goal's conditions")
    record_size = statistics.fmean(len(json.dumps(r, default=str)) for r in records) if records else 300.0
    return Estimate(
        pages=pages,
        exact=exact,
        listing_pages=listing,
        requests=requests,
        browser_pages=browser_pages,
        bytes=int(requests * average),
        seconds=round(seconds, 1),
        records=expected,
        cpu_seconds=round((pages + listing) * per_page, 1),
        storage_bytes=int(expected * (record_size + 1)),
        basis=basis,
    )


def _pattern_is(path: str, patterns: list[str]) -> bool:
    """A topology section path (``/p/{id}``) that one of the patterns covers."""
    generic = re.sub(r"\{[a-z]+\}", "x", path)
    return any(_pattern_regex(p).match(generic) for p in patterns)


def _linked_targets(survey: SiteSurvey, patterns: list[str]) -> int:
    if not patterns:
        return 0
    links: set[str] = set()
    for page in survey.pages:
        if page.is_html:
            links.update(link for link in page.links(same_domain=True) if _matches(link, patterns))
    return len(links)


def _targets_per_listing(survey: SiteSurvey, patterns: list[str]) -> float:
    counts = []
    for page in survey.pages:
        if page.is_html and not _matches(page.url, patterns):
            n = sum(1 for link in page.links(same_domain=True) if _matches(link, patterns))
            if n >= 2:
                counts.append(n)
    return round(statistics.median(counts), 1) if counts else 0.0


def _steps(
    goal: Goal, plan: SitePlan, survey: SiteSurvey, listed_targets: list[str], found: Counter[str], sections: list[str]
) -> list[str]:
    kind = goal.kind
    steps = []
    robots = (survey.profile.crawlability or {}).get("robots") or {}
    rules = (
        "no robots.txt"
        if not robots.get("found")
        else "robots.txt forbids crawling"
        if robots.get("disallow_all")
        else "robots.txt allows crawling" + (f" (crawl delay {plan.crawl_delay:g} s)" if plan.crawl_delay else "")
    )
    read = survey.sitemaps
    maps = f"{read.sitemaps} sitemap(s) list {len(read.entries):,} pages" if read.sitemaps else "no sitemap found"
    steps.append(f"{rules}; {maps}.")
    patterns = ", ".join(plan.target) or "(unknown yet)"
    how = (
        "adaptive fetching: HTTP, and a browser for the pages that need JavaScript"
        if plan.fetch == "adaptive"
        else "over HTTP"
    )
    if plan.strategy == "sitemap":
        where = f" under {', '.join(sections)}" if sections else ""
        steps.append(
            f"Fetch the {len(listed_targets):,} {kind.name} pages the sitemaps list{where} ({patterns}), {how}."
        )
    else:
        start = ", ".join(urlsplit(u).path or "/" for u in plan.start_urls)
        via = ", ".join(plan.follow) or "every page"
        steps.append(
            f"Follow links from {start} through {via} and pagination to the {kind.name} pages ({patterns}), {how}."
        )
    sampled = plan.sample["record_pages"]
    if sampled:
        fill = ", ".join(f"{f} {n}/{sampled}" for f, n in plan.sample["fields"].items())
        steps.append(f"Extract {', '.join(goal.fields)}: sampled {kind.name} pages gave {fill}.")
    else:
        steps.append(f"Extract {', '.join(goal.fields)} from the pages classified as {kind.name} pages.")
    keep = [f.expression for f in goal.filters]
    if keep:
        steps.append("Keep the records where " + " and ".join(f"({k})" for k in keep) + ".")
    if goal.dedupe:
        steps.append("Remove duplicates: records with the same URL (pages that name their canonical URL count once).")
    if goal.limit:
        steps.append(f"Stop at {goal.limit:,} records.")
    if goal.monitor:
        steps.append("Record the pages' history, so the next run reports what changed.")
    return steps
