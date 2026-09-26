"""One place to start: say what data you want, or go down to pages and spiders, with settings shared by all.

::

    from wintergrab import WinterGrab

    wg = WinterGrab(network_policy="public")
    plan = wg.plan("Find all laptops under $1000 on shop.example with name, price and rating")
    print(plan.describe())                       # the steps, and what they will cost
    result = wg.run(plan, "laptops.jsonl")       # or wg.run("Find all laptops ...") at once
    print(result.summary())

    result = await wg.arun(plan)                 # the same, from async code

    page = wg.get("https://shop.example/p/1")                  # a Response
    record = wg.extract(page, "product")                       # a typed record, from a template or a schema
    print(wg.sources("https://shop.example/catalog").describe())   # where a page's data is
    survey = wg.inspect("https://shop.example")                # a site's robots.txt, sitemaps and profile

Each method returns what the rest of wintergrab uses (:class:`~wintergrab.goals.GoalPlan`,
:class:`~wintergrab.goals.GoalResult`, :class:`~wintergrab.Response`...): nothing here is a world of its own,
and every lower level stays in reach.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .extraction.engine import ExtractedRecord
    from .fetchers.response import Response
    from .goals import Goal, GoalPlan, GoalResult
    from .intel.sources import DataSources
    from .intel.survey import SiteSurvey

__all__ = ["WinterGrab"]


class WinterGrab:
    """Goals, pages and sites with settings shared by all (see the module docs).

    Args:
        model: A language model to read goals with, and to extract with when asked: ``"provider:name"``
            (``"openai:gpt-4o-mini"``, ``"ollama:llama3.1"``; see :func:`~wintergrab.models.load_model`) or a
            model object. None by default: everything works without one.
        browser: Render pages in a browser when surveying sites and fetching pages (slower; needed for pages
            built with JavaScript, and to record the API calls pages make).
        obey_robots: Obey robots.txt.
        network_policy: Where requests may go: ``"public"`` refuses private, loopback and cloud-metadata
            addresses (see :class:`~wintergrab.NetworkPolicy`).
        cache: An HTTP cache for every fetch: ``True``, a directory or an :class:`~wintergrab.HTTPCache`.
        timeout: Seconds per request.
        log_level: The ``wintergrab`` logger's level during surveys and runs (``None`` leaves logging alone).
        settings: More :class:`~wintergrab.Spider` settings for every survey and run (``concurrency``,
            ``proxies``, ``max_requests``...).
    """

    def __init__(
        self,
        *,
        model: Any = None,
        browser: bool = False,
        obey_robots: bool = True,
        network_policy: Any = None,
        cache: Any = None,
        timeout: float = 20.0,
        log_level: str | None = "WARNING",
        **settings: Any,
    ) -> None:
        self.model = model
        self.browser = browser
        self.obey_robots = obey_robots
        self.network_policy = network_policy
        self.cache = cache
        self.timeout = timeout
        self.log_level = log_level
        self.settings = settings
        self._model: Any = None

    def __repr__(self) -> str:
        shown = {"browser": self.browser, "network_policy": self.network_policy, "model": self.model}
        return "WinterGrab(" + ", ".join(f"{k}={v!r}" for k, v in shown.items() if v) + ")"

    # -- goals ------------------------------------------------------------------------------------- #
    def goal(self, text: str, *, sites: list[str] | None = None) -> Goal:
        """A request in plain words as a :class:`~wintergrab.goals.Goal`, read by the model when there is one."""
        from .goals import model_reader, parse_goal

        model = self._model_object()
        return parse_goal(text, sites=sites, parser=model_reader(model) if model is not None else None)

    def plan(self, goal: str | Goal, *, sites: list[str] | None = None, sample: int = 30) -> GoalPlan:
        """Survey the goal's sites (robots.txt, sitemaps, ``sample`` pages each) and plan the crawl: what to fetch,
        how, and what it will cost. Nothing is collected yet: :meth:`run` the plan."""
        from .goals import plan_goal

        if isinstance(goal, str):
            goal = self.goal(goal, sites=sites)
        elif sites:  # a copy: the caller's goal stays as it was
            goal = dataclasses.replace(goal, sites=list(dict.fromkeys([*goal.sites, *sites])))
        return plan_goal(
            goal,
            sample=sample,
            obey_robots=self.obey_robots,
            browser=self.browser,
            timeout=self.timeout,
            log_level=self.log_level,
            settings=self._spider_settings(),
        )

    def run(
        self,
        goal: str | Goal | GoalPlan,
        output: str | None = None,
        *,
        sites: list[str] | None = None,
        sample: int = 30,
        max_pages: int | None = None,
        **settings: Any,
    ) -> GoalResult:
        """Collect a goal's records into ``output`` (``.jsonl``, ``.csv``, a database URL...; kept in
        ``result.records`` when there is none). A goal that is not planned yet is planned first (:meth:`plan`).
        ``settings``: more :class:`~wintergrab.Spider` settings for this run."""
        from .errors import ConfigurationError
        from .goals import GoalPlan

        if isinstance(goal, GoalPlan) and sites:
            raise ConfigurationError("a plan's sites are those it surveyed: plan the goal again to add sites")
        plan = goal if isinstance(goal, GoalPlan) else self.plan(goal, sites=sites, sample=sample)
        options = {"obey_robots_txt": self.obey_robots, "timeout": self.timeout, **self._spider_settings(), **settings}
        return plan.run(output, max_pages=max_pages, log_level=self.log_level, **options)

    async def arun(
        self,
        goal: str | Goal | GoalPlan,
        output: str | None = None,
        **options: Any,
    ) -> GoalResult:
        """:meth:`run` from async code (the crawl runs in a thread of its own, with its own event loop)."""
        return await asyncio.to_thread(self.run, goal, output, **options)

    # -- pages ------------------------------------------------------------------------------------- #
    def get(self, url: str, **options: Any) -> Response:
        """One page: over HTTP, or rendered in a browser when this WinterGrab uses one (``browser=True``).
        ``options``: :func:`~wintergrab.get` / :func:`~wintergrab.render` options for this page."""
        from .fetchers import get, render

        shared: dict[str, Any] = {"timeout": self.timeout}
        if self.network_policy is not None:
            shared["network_policy"] = self.network_policy
        if self.cache is not None:
            shared["cache"] = self.cache
        return (render if self.browser else get)(url, **{**shared, **options})

    def extract(
        self, page: str | Response, schema: Any, *, all: bool = False
    ) -> ExtractedRecord | list[ExtractedRecord]:
        """A typed record from a page (a URL, fetched with :meth:`get`, or a :class:`~wintergrab.Response`), with
        where each value came from and how sure it is: ``schema`` is a template name (``"product"``), a schema
        file or a :class:`~wintergrab.data.Schema`. ``all=True`` reads every record of a listing page."""
        from .extraction import Extractor

        response = self.get(page) if isinstance(page, str) else page
        extractor = Extractor(schema, model=self._model_object())
        return extractor.extract_all(response) if all else extractor.extract(response)

    def sources(self, page: str | Response) -> DataSources:
        """Where a page's data is (:func:`~wintergrab.intel.sources.data_sources`): its HTML records, JSON-LD,
        embedded JSON and, in a browser, the API calls it makes."""
        from .intel.sources import data_sources

        if not isinstance(page, str):
            return data_sources(page)
        response = self.get(page, capture=True) if self.browser else self.get(page)
        return data_sources(response, recorded=self.browser)

    def inspect(self, url: str, *, pages: int = 30) -> SiteSurvey:
        """A site's robots.txt, sitemaps and ``pages`` pages, with its profile (``survey.profile.describe()``),
        as ``wintergrab inspect`` reads them."""
        from .intel.survey import survey_site

        return survey_site(
            url,
            pages=pages,
            obey_robots=self.obey_robots,
            browser=self.browser,
            timeout=self.timeout,
            log_level=self.log_level,
            **self._spider_settings(),
        )

    # -- shared ------------------------------------------------------------------------------------ #
    def _spider_settings(self) -> dict[str, Any]:
        settings: dict[str, Any] = dict(self.settings)
        if self.network_policy is not None:
            settings.setdefault("network_policy", self.network_policy)
        if self.cache is not None:
            settings.setdefault("cache", self.cache)
        return settings

    def _model_object(self) -> Any:
        if self.model is None or not isinstance(self.model, str):
            return self.model
        if self._model is None:
            from .models import load_model

            self._model = load_model(self.model)
        return self._model

    def configure(self, **changes: Any) -> WinterGrab:
        """A copy with some settings changed: ``wg.configure(browser=True).sources(url)``."""
        known = {"model", "browser", "obey_robots", "network_policy", "cache", "timeout", "log_level"}
        options: dict[str, Any] = {name: getattr(self, name) for name in known}
        settings: Mapping[str, Any] = {**self.settings, **{k: v for k, v in changes.items() if k not in known}}
        options.update({k: v for k, v in changes.items() if k in known})
        return WinterGrab(**options, **settings)
