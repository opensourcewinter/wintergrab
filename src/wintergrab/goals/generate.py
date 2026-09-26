"""Scrapers generated from a goal, and tested before they are kept.

::

    result = generate_scraper("books with title, price and rating on books.example", "scrapers/books")
    print(result.describe())        # each step, what it measured, and the verdict
    if result.accepted:             # then, as often as needed:
        GoalPlan.load("scrapers/books/plan.json").run("books.jsonl")

From a goal (what to collect, from which site: :func:`~wintergrab.goals.parse_goal`)
the generator goes through these steps, and keeps what it made only when
every one of them passes:

1. **plan**: survey the site and plan the crawl (:func:`~wintergrab.goals.plan_goal`),
   which tells which pages hold the records;
2. **generate**: learn selectors for the goal's fields from some of those pages
   (:func:`~wintergrab.extraction.generate_schema`). With ``model=``, a model finds
   what the pages' own data and layout do not give, once, and a selector reads it
   from then on;
3. **lint**: the schema reads back as it was written, its selectors compile, and
   the plan's URL patterns are valid (a selector that depends on an element's
   position is pointed out);
4. **test**: the sample pages become extraction tests (``fixtures/``), expecting
   the values found on them, and the schema must read them all without a model;
5. **sample crawl**: the plan runs with the new schema, as a real crawl (robots.txt,
   rate limits and all), for record pages beyond those it was generated from;
6. **validate**: the quality system measures those records: every required field
   found on nearly every page (``min_completeness``), valid values, and, where the
   goal's own extraction finds a value, the same value (``min_agreement``);
7. **benchmark**: the generated scraper and the goal's own extraction, on the same
   pages: time per page, fields found, confidence. It must find no fewer fields;
8. **accept or reject**, saying why.

Everything is written to one directory: ``plan.json`` (run it with
``wintergrab goal --plan``), ``schema.json``, ``fixtures/`` (run them with
``wintergrab test``), ``sample.jsonl`` (the sample crawl's records),
``quality.json`` (their quality report: a baseline for ``--quality``) and
``report.json`` (every step). A rejected scraper's files are kept too, to be
looked at and fixed by hand.
"""

from __future__ import annotations

import json
import re
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..data.quality import QualityMonitor
from ..data.schema import Schema
from ..errors import ConfigurationError
from ..extraction import Extractor, PageContext
from ..extraction.fixtures import FixtureSuite
from ..extraction.generate import GeneratedSchema, _trusted, generate_schema
from ..extraction.healing import _same
from ..fetchers.response import Response
from ..parser import Selector
from ..urls import normalize_url
from .goal import Goal, parse_goal
from .plan import GoalPlan, plan_goal, survey_for
from .run import _url_regex, run_plan

__all__ = ["GenerationResult", "Stage", "generate_scraper"]

_POSITIONAL = re.compile(r":nth-(?:last-)?(?:of-type|child)\(|^html\b")


@dataclass
class Stage:
    """One step of the generation (see the module docs).

    Attributes:
        name: ``"plan"``, ``"generate"``, ``"lint"``, ``"test"``, ``"sample"``, ``"validate"`` or
            ``"benchmark"``.
        ok: It passed.
        summary: What it did and measured, in a line.
        problems: Why it did not pass.
        warnings: What to look at, though it passed.
        details: The measurements.
    """

    name: str
    ok: bool
    summary: str
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "summary": self.summary,
            "problems": self.problems,
            "warnings": self.warnings,
            "details": self.details,
        }


@dataclass
class GenerationResult:
    """What :func:`generate_scraper` made, each step, and the verdict.

    Attributes:
        directory: Where the files are.
        goal: The goal, as understood.
        stages: The steps taken, in order (the generation stops at the first that cannot go on).
        accepted: Every step passed: the scraper can be used.
        reasons: Why it was rejected.
        plan: The plan (with the generated schema), when there is one.
        generated: The generated schema and what was learned for each field.
    """

    directory: Path
    goal: Goal
    stages: list[Stage] = field(default_factory=list)
    accepted: bool = False
    reasons: list[str] = field(default_factory=list)
    plan: GoalPlan | None = None
    generated: GeneratedSchema | None = None

    def stage(self, name: str) -> Stage | None:
        """The step called ``name``, if it was taken."""
        return next((s for s in self.stages if s.name == name), None)

    def describe(self) -> str:
        """Each step in a line (its problems and warnings under it), then the verdict."""
        lines = []
        for stage in self.stages:
            lines.append(f"{stage.name:<10} {'ok  ' if stage.ok else 'FAIL'}  {stage.summary}")
            lines.extend(f"{'':16}{problem}" for problem in stage.problems)
            lines.extend(f"{'':16}note: {warning}" for warning in stage.warnings)
        if self.accepted:
            lines.append(f"accepted: {self.directory / 'plan.json'}")
        else:
            lines.append("rejected: " + "; ".join(self.reasons))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "goal": self.goal.to_dict(),
            "accepted": self.accepted,
            "reasons": self.reasons,
            "stages": [s.to_dict() for s in self.stages],
            "generated": self.generated.to_dict() if self.generated else None,
        }


def generate_scraper(
    goal: str | Goal,
    directory: str | Path,
    *,
    sites: Sequence[str] = (),
    model: Any = None,
    sample: int = 30,
    train: int = 5,
    test: int = 10,
    obey_robots: bool = True,
    browser: bool = False,
    timeout: float = 20.0,
    min_completeness: float = 0.9,
    min_agreement: float = 0.9,
    log_level: str | None = "WARNING",
    on_stage: Callable[[Stage], None] | None = None,
) -> GenerationResult:
    """Generate a scraper for ``goal``, test it, and accept or reject it (see the module docs).

    Args:
        goal: What to collect, in words or as a :class:`~wintergrab.goals.Goal`, from one site.
        directory: Where to write the scraper: a new or empty directory, or an earlier generation's
            (its files are replaced).
        sites: The site, when the goal does not name it.
        model: An extraction model: it finds the values the pages' data and layout do not give while
            generating, and a model provider (:mod:`wintergrab.models`) reads the goal too. The scraper
            runs without it.
        sample: Pages to survey the site with.
        train: Record pages to generate the scraper from.
        test: Record pages beyond those to crawl and test it on.
        obey_robots: Obey robots.txt (the survey and the sample crawl).
        browser: Survey with a browser.
        timeout: Seconds per request.
        min_completeness: Share of the pages tested on on which each required field must be found.
        min_agreement: Share of the values the goal's own extraction finds that the scraper must find too.
        on_stage: Called with each step as it ends (to show progress).

    Raises:
        ConfigurationError: The goal names no site, or several.
    """
    if isinstance(goal, str):
        reader = None
        if callable(getattr(model, "complete", None)):  # a model provider reads the request too
            from .reading import model_reader

            reader = model_reader(model)
        goal = parse_goal(goal, sites=list(sites), parser=reader)
    elif sites:
        goal = replace(goal, sites=list(dict.fromkeys([*goal.sites, *sites])))
    if not goal.sites:
        raise ConfigurationError("which site? Name it in the goal (shop.example) or give it (sites=, --site)")
    if len(goal.sites) > 1:
        raise ConfigurationError(
            f"a scraper is generated for one site, and the goal names {len(goal.sites)}: generate one per site"
        )
    target = Path(directory)
    _check_directory(target)
    target.mkdir(parents=True, exist_ok=True)
    result = GenerationResult(directory=target, goal=goal)

    def done(stage: Stage) -> bool:
        result.stages.append(stage)
        if on_stage is not None:
            on_stage(stage)
        return stage.ok

    try:
        _generate(result, done, model=model, sample=sample, train=train, test=test, obey_robots=obey_robots,
                  browser=browser, timeout=timeout, min_completeness=min_completeness,
                  min_agreement=min_agreement, log_level=log_level)  # fmt: skip
    finally:
        result.accepted = bool(result.stages) and all(s.ok for s in result.stages) and len(result.stages) == 7
        result.reasons = [f"{s.name}: {p}" for s in result.stages if not s.ok for p in (s.problems or [s.summary])]
        (target / "report.json").write_text(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
        )
    return result


def _generate(
    result: GenerationResult,
    done: Callable[[Stage], bool],
    *,
    model: Any,
    sample: int,
    train: int,
    test: int,
    obey_robots: bool,
    browser: bool,
    timeout: float,
    min_completeness: float,
    min_agreement: float,
    log_level: str | None,
) -> None:
    goal, target = result.goal, result.directory
    site = goal.sites[0]
    # -- 1. plan -------------------------------------------------------------------------------- #
    survey = survey_for(goal, site, sample=sample, obey_robots=obey_robots, browser=browser, timeout=timeout,
                        log_level=log_level)  # fmt: skip
    plan = plan_goal(goal, surveys={site: survey})
    site_plan = plan.sites[0]
    records = [p for p in survey.pages if p.is_html and site_plan.is_target(p.url)]
    kind = goal.kind.name
    stage = Stage("plan", True, "", details={"pages": len(survey.pages), "record_pages": len(records),
                                             "target": site_plan.target, "strategy": site_plan.strategy})  # fmt: skip
    if not site_plan.allowed and obey_robots:
        stage.ok, stage.problems = False, ["robots.txt keeps crawlers out of the site"]
    elif not records:
        stage.ok, stage.problems = False, [f"none of the {len(survey.pages)} pages sampled holds a {kind}"]
    training = _spread(records, train)
    stage.summary = (
        f"{len(records)} of {len(survey.pages)} pages sampled hold a {kind} ({', '.join(site_plan.target) or '-'});"
        f" generating from {len(training)}"
    )
    stage.warnings = list(site_plan.warnings)
    result.plan = plan
    if not done(stage):
        return
    # -- 2. generate ---------------------------------------------------------------------------- #
    generated = generate_schema(training, goal.schema(), model=model)
    result.generated = generated
    learned = [f"{name} {generated.fields[name].selector}" for name in generated.learned]
    stage = Stage("generate", True, f"selectors for {len(learned)} of {len(generated.fields)} fields"
                  + (f": {', '.join(learned)}" if learned else ""),
                  details={name: f.to_dict() for name, f in generated.fields.items()})  # fmt: skip
    stage.warnings = [
        f"{name}: {f.status}: {f.note}"
        for name, f in generated.fields.items()
        if f.status in ("not learned", "not found") and name in goal.fields
    ]
    if generated.model:
        tokens = generated.usage.get("input", 0) + generated.usage.get("output", 0)
        stage.details["model"] = {"name": generated.model, **generated.usage}
        if generated.usage:
            stage.summary += f"; the model used {tokens:,} tokens on {len(training)} pages"
    schema_path = target / "schema.json"
    generated.schema.save(schema_path)
    plan.schema, plan.directory = "schema.json", target.resolve()
    plan.save(target / "plan.json")
    if not done(stage):
        return
    # -- 3. lint -------------------------------------------------------------------------------- #
    if not done(_lint(generated.schema, plan)):
        return
    # -- 4. unit tests -------------------------------------------------------------------------- #
    if not done(_unit_tests(generated, training, target / "fixtures", schema_path)):
        return
    # -- 5. sample crawl ------------------------------------------------------------------------ #
    seen = {normalize_url(p.url) for p in training}
    output = target / "sample.jsonl"
    wanted = test + len(training)
    stage = Stage("sample", True, "")
    try:
        crawled = run_plan(plan, str(output), max_items=wanted, max_pages=wanted * 4 + 20, keep_pages=True,
                           log_level=log_level, progress=False, obey_robots_txt=obey_robots)  # fmt: skip
    except Exception as exc:  # the crawl could not run: the report says why
        stage.ok, stage.summary, stage.problems = False, "the sample crawl failed", [f"{type(exc).__name__}: {exc}"]
        done(stage)
        return
    pages = [p for p in crawled.pages if p.is_html]
    unseen = [p for p in pages if normalize_url(p.url) not in seen][:test]
    judged = unseen or pages[:test]
    stage.details = {"pages": crawled.counts["pages"], "record_pages": len(pages), "unseen": len(unseen),
                     "records": crawled.counts["records"], "errors": crawled.counts["errors"]}  # fmt: skip
    stage.summary = (
        f"{crawled.counts['pages']} pages crawled, {len(pages)} with a {kind}, {len(unseen)} not among the samples;"
        f" {crawled.counts['records']} records -> {output.name}"
    )
    if not pages:
        where = ", ".join(site_plan.target) or "-"
        through = f" through {', '.join(site_plan.follow)}" if site_plan.follow else ""
        stage.ok = False
        stage.problems = [f"the crawl found no {kind} page: the plan looks for {where}{through}; check the plan"]
    elif not unseen:
        stage.warnings.append("no page beyond those it was generated from: it is judged on those")
    if not done(stage):
        return
    # -- 6. validate, 7. benchmark --------------------------------------------------------------- #
    done(_validate(goal, generated, judged, target, min_completeness, min_agreement))
    done(_benchmark(goal, generated, judged, len(training)))


def _check_directory(target: Path) -> None:
    """Refuse a directory the generator would overwrite others' files in: it must be new, empty, or
    an earlier generation's (its ``report.json`` says so)."""
    if not target.exists():
        return
    if not target.is_dir():
        raise ConfigurationError(f"{target} is a file: give a directory for the scraper")
    if not any(target.iterdir()):
        return
    try:
        earlier = json.loads((target / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        earlier = None
    if not (isinstance(earlier, dict) and "stages" in earlier and "goal" in earlier):
        raise ConfigurationError(
            f"{target} holds files of its own (no earlier generation's report.json): give a new directory"
        )


def _spread(pages: list[Response], count: int) -> list[Response]:
    """``count`` pages spread evenly across ``pages`` (different parts of the site, likelier different)."""
    if count <= 0 or not pages:
        return []
    if count >= len(pages):
        return list(pages)
    return [pages[round(i * (len(pages) - 1) / max(1, count - 1))] for i in range(count)] if count > 1 else pages[:1]


def _lint(schema: Schema, plan: GoalPlan) -> Stage:
    """The schema reads back as written, its selectors compile, the plan's patterns are valid."""
    stage = Stage("lint", True, "")
    written = json.loads(json.dumps(schema.to_dict()))
    try:
        if Schema.from_dict(written).to_dict() != written:
            stage.problems.append("the schema does not read back as it was written")
    except Exception as exc:
        stage.problems.append(f"the schema does not read back: {exc}")
    page = Selector("<html><body></body></html>")
    selectors = 0
    for f in schema.fields:
        for query in f.selectors:
            selectors += 1
            try:
                page.select(query)
            except Exception as exc:
                stage.problems.append(f"{f.name}: {query!r} does not compile: {exc}")
            if _POSITIONAL.search(query):
                stage.warnings.append(f"{f.name}: {query!r} depends on the element's position")
    patterns = [p for site in plan.sites for p in (*site.target, *site.follow)]
    for pattern in patterns:
        try:
            re.compile(_url_regex(pattern))
        except re.error as exc:
            stage.problems.append(f"the URL pattern {pattern!r} is not valid: {exc}")
    try:
        GoalPlan.from_dict(json.loads(json.dumps(plan.to_dict(embed_schema=True)))).extraction_schema()
    except Exception as exc:
        stage.problems.append(f"the plan does not read back: {exc}")
    stage.ok = not stage.problems
    stage.summary = f"{selectors} selector(s) compile, {len(patterns)} URL pattern(s) valid" if stage.ok else "invalid"
    return stage


def _unit_tests(generated: GeneratedSchema, pages: list[Response], directory: Path, schema_path: Path) -> Stage:
    """The sample pages as extraction tests, run with the generated schema (and no model)."""
    if directory.exists():  # the tests of an earlier generation
        for old in [*directory.glob("[0-9][0-9][0-9][0-9]-*.json"), *directory.glob("[0-9][0-9][0-9][0-9]-*.html")]:
            old.unlink()
        (directory / "suite.json").unlink(missing_ok=True)
    suite = FixtureSuite(directory)
    for i, page in enumerate(pages):
        expected = generated.expected(i)
        if expected:
            suite.add(page, schema=str(schema_path), expect=expected, only=True)
    report = suite.run(str(schema_path))
    checks = sum(len(r.checks) for r in report.results)
    stage = Stage("test", report.ok, f"{report.passed}/{len(report.results)} pages give the expected values"
                  f" ({checks} values; {directory.name}/)", details=report.to_dict())  # fmt: skip
    if not report.results:
        stage.ok, stage.problems = False, ["no value to expect on the sample pages"]
    for r in report.results:
        if r.error:
            stage.problems.append(f"{r.fixture.name}: {r.error}")
        stage.problems.extend(
            f"{r.fixture.name}: {c.field} is {c.got!r}, expected {c.expected!r}" for c in r.failed[:3]
        )
    return stage


def _validate(
    goal: Goal,
    generated: GeneratedSchema,
    pages: list[Response],
    directory: Path,
    min_completeness: float,
    min_agreement: float,
) -> Stage:
    """The quality of what the scraper reads on the pages tested on, and its agreement with the goal's
    own extraction there."""
    schema, own = generated.schema, generated.base
    monitor = QualityMonitor(schema, save_to=None)
    ours, theirs = Extractor(schema), Extractor(own)
    compared = agreed = 0
    differences: list[str] = []
    for page in pages:
        record = ours.extract(page)
        monitor.observe(record.to_dict())
        reference = theirs.extract(page)
        for name in goal.fields:
            fv = reference.fields.get(name)
            if fv is None or not _trusted(fv, 0.7) or name == "url":
                continue
            compared += 1
            got = record.fields[name].value
            if got not in (None, "", []) and _same(got, fv.value):
                agreed += 1
            elif len(differences) < 5:
                differences.append(f"{name} on {page.url}: {got!r}, the goal's extraction {fv.value!r}")
    report = monitor.report()
    report.save(directory / "quality.json")
    stage = Stage("validate", True, "", details={"quality": report.to_dict(), "compared": compared, "agreed": agreed})
    found = []
    for f in schema.fields:
        if f.name not in goal.fields:
            continue
        quality = report.fields.get(f.name) or {}
        completeness = float(quality.get("completeness") or 0)
        found.append(f"{f.name} {completeness:.0%}")
        if f.required and completeness < min_completeness:
            stage.problems.append(f"{f.name} (required) found on {completeness:.0%} of the pages")
        elif not completeness and f.name not in ("currency", "url"):  # those two the goal adds by itself
            stage.problems.append(f"the goal asks for {f.name}: found on no page")
        validity = quality.get("validity")
        if validity is not None and validity < 0.9:
            stage.problems.append(f"{f.name}: {1 - validity:.0%} of its values are not valid")
    agreement = agreed / compared if compared else 1.0
    if agreement < min_agreement:
        stage.problems.append(
            f"it reads {compared - agreed} of {compared} values differently from the goal's extraction"
        )
        stage.problems.extend(differences)
    stage.ok = not stage.problems
    stage.summary = (
        f"{len(pages)} page(s): found {', '.join(found)}; the same values as the goal's own extraction:"
        f" {agreed}/{compared}; quality score {report.score if report.score is not None else '-'}"
    )
    return stage


def _benchmark(goal: Goal, generated: GeneratedSchema, pages: list[Response], trained_on: int) -> Stage:
    """The scraper against the goal's own extraction on the same pages: time, fields found, confidence."""
    measured: dict[str, dict[str, float]] = {}
    for label, schema in (("generated", generated.schema), ("goal", generated.base)):
        extractor = Extractor(schema)
        times, filled, confidence = [], [], []
        for page in pages:
            runs = []
            for _ in range(3):  # a fresh context each time: nothing the other extractor read is reused
                started = time.perf_counter()
                record = extractor.extract(PageContext(page))
                runs.append(time.perf_counter() - started)
            times.append(statistics.median(runs))
            filled.append(sum(1 for name in goal.fields if record.data.get(name) not in (None, "", [])))
            confidence.append(record.confidence)
        measured[label] = {
            "ms_per_page": round(1000 * statistics.fmean(times), 3) if times else 0.0,
            "fields_per_page": round(statistics.fmean(filled), 3) if filled else 0.0,
            "confidence": round(statistics.fmean(confidence), 4) if confidence else 0.0,
        }
    ours, theirs = measured["generated"], measured["goal"]
    stage = Stage("benchmark", True, "", details={"pages": len(pages), **measured})
    stage.summary = (
        f"{ours['ms_per_page']:.1f} ms/page (goal's extraction {theirs['ms_per_page']:.1f});"
        f" {ours['fields_per_page']:.1f} fields/page ({theirs['fields_per_page']:.1f});"
        f" confidence {ours['confidence']:.2f} ({theirs['confidence']:.2f})"
    )
    if generated.model and generated.usage and trained_on:
        tokens = generated.usage.get("input", 0) + generated.usage.get("output", 0)
        stage.details["model_tokens_per_page"] = round(tokens / trained_on, 1)
        stage.summary += f"; no model (it used {tokens / trained_on:,.0f} tokens/page while generating)"
    if ours["fields_per_page"] < theirs["fields_per_page"]:
        stage.ok = False
        stage.problems.append(
            f"it finds fewer fields per page ({ours['fields_per_page']}) than the goal's extraction"
            f" ({theirs['fields_per_page']})"
        )
    return stage
