"""Runs: a record of each crawl, and its replay from what was recorded.

::

    class Shop(Spider):
        record = True                 # keep this crawl in .wintergrab/runs/run-N, its pages and items included

    result = Shop().run()
    result.run_id                                       # "run-7"

    for run in RunRegistry().runs():                    # .wintergrab/runs, newest first
        print(run.describe())
    replayed = replay("run-7", Shop)                    # the same crawl from the recorded pages: no network
    print(replayed.summary())                           # ... and how its items differ from the recorded ones

A spider with ``run_registry`` (``True``: ``.wintergrab`` in the current directory; or a directory)
leaves a record of each run in ``runs/run-N``: ``run.json`` (when, the settings, the status, the
stats, the failures, how to run it again) and ``events.jsonl`` (its :mod:`events
<wintergrab.events>`, the per-response and per-item ones aside). With ``record``, a run also keeps
every response it received (``archive/``: an :class:`~wintergrab.fetchers.cache.HTTPCache`,
robots.txt included, whatever the status), the items it wrote (``items.jsonl``) and each response's
timing (in the events), so that :func:`replay` can crawl again from the archive, without touching
the network, and compare the items. Replaying a recorded crawl after changing a spider or a schema
shows what the change does to the data.

From the command line: ``wintergrab crawl URL ... --record``, ``wintergrab runs``,
``wintergrab runs show run-7``, ``wintergrab replay run-7`` (exit status 1 when the items differ).
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import json
import os
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from .errors import ConfigurationError

if TYPE_CHECKING:
    from .data.versions import DatasetDiff
    from .fetchers.cache import HTTPCache
    from .spider import CrawlResult, Spider

__all__ = ["DEFAULT_WORKSPACE", "ReplayResult", "Run", "RunRecorder", "RunRegistry", "replay"]

#: Where runs are kept by default: ``.wintergrab`` in the current directory.
DEFAULT_WORKSPACE = ".wintergrab"
_ALL_STATUSES = range(100, 600)
_MAX_LIST = 1000  # items of a list setting kept with a run
# settings a replay does not take from the recording (it sets its own, or they are the run's own state)
_NOT_REPLAYED = frozenset({"name", "cache", "cache_mode", "cache_ttl", "run_id"})
_ALL_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
# settings never worth keeping with a run (objects, or noise)
_SKIPPED_SETTINGS = frozenset({"events", "middlewares", "pipelines", "throttle", "run_recipe", "webhooks"})


@dataclass
class Run:
    """One crawl, as the registry keeps it.

    Attributes:
        id: ``"run-7"``.
        number: 7.
        name: The spider's name.
        started, finished: Unix times.
        status: ``"running"`` (or interrupted before it could say), then the crawl's status
            (``"finished"``, ``"limit"``, ``"paused"``, ``"stopped"``) or ``"failed"``.
        recorded: Its responses and items were kept (it can be replayed).
        resumed: It continued a crawl paused earlier (``crawl_dir``).
        stats: The crawl's counters.
        settings: The spider's settings (JSON values; others as their ``repr``).
        recipe: How to run it again: ``{"command": [...]}`` (the command line), ``{"spider":
            "module:Class"}`` or ``{"goal_plan": {...}}``.
        output: Where the items went.
        failures: The failure diagnoses: ``{"signature", "domain", "urls", "cause"}``.
        label: A label given to the run (the project job that ran it).
        error: What stopped it, if it failed.
        directory: Where it is kept.
    """

    id: str
    number: int
    name: str
    started: float
    finished: float | None = None
    status: str = "running"
    recorded: bool = False
    resumed: bool = False
    stats: dict[str, Any] = dataclass_field(default_factory=dict)
    settings: dict[str, Any] = dataclass_field(default_factory=dict)
    recipe: dict[str, Any] = dataclass_field(default_factory=dict)
    output: str | None = None
    failures: list[dict[str, Any]] = dataclass_field(default_factory=list)
    error: str | None = None
    label: str | None = None
    directory: Path = dataclass_field(default=Path("."), repr=False, compare=False)

    @property
    def duration(self) -> float | None:
        return (self.finished - self.started) if self.finished else None

    @property
    def archive(self) -> Path:
        return self.directory / "archive"

    def describe(self) -> str:
        """One line: id, when, status, what it did."""
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started))
        s = self.stats
        facts = f"{s.get('pages', 0):,} pages, {s.get('items', 0):,} items, {s.get('errors', 0):,} errors"
        took = f", {self.duration:.1f} s" if self.duration is not None else ""
        extra = "  [recorded]" if self.recorded else ""
        name = f"{self.label} ({self.name})" if self.label else self.name
        return f"{self.id:<8} {when}  {self.status:<9} {name:<16} {facts}{took}{extra}"

    def details(self) -> str:
        """Several lines: the run, its settings and stats, its failures, its files."""
        lines = [self.describe(), f"  directory: {self.directory}"]
        if self.recipe.get("command"):
            lines.append("  command: wintergrab " + " ".join(_quote(a) for a in self.recipe["command"]))
        elif self.recipe.get("spider"):
            lines.append(f"  spider: {self.recipe['spider']}")
        elif self.recipe.get("goal_plan"):
            lines.append(f"  goal: {self.recipe['goal_plan'].get('goal', {}).get('text', '')}")
        if self.output:
            lines.append(f"  output: {self.output}")
        if self.error:
            lines.append(f"  error: {self.error}")
        shown = ("pages", "items", "requests", "retries", "errors", "bytes", "elapsed_seconds", "duplicates_filtered")
        stats = ", ".join(f"{k} {self.stats[k]:,}" for k in shown if isinstance(self.stats.get(k), (int, float)))
        if stats:
            lines.append(f"  stats: {stats}")
        for failure in self.failures[:5]:
            lines.append(f"  failure: {failure.get('signature')} on {failure.get('domain')}: "
                         f"{failure.get('urls', 0):,} URL(s); {failure.get('cause') or 'cause unknown'}")  # fmt: skip
        for name in ("run.json", "events.jsonl", "items.jsonl", "archive/http.sqlite3"):
            path = self.directory / name
            if path.exists():
                lines.append(f"  {name}: {_size(path.stat().st_size)}")
        return "\n".join(lines)

    def items(self) -> Iterator[dict[str, Any]]:
        """The items the run wrote (recorded runs)."""
        yield from _read_jsonl(self.directory / "items.jsonl")

    def events(self, kinds: str | Sequence[str] | None = None) -> Iterator[dict[str, Any]]:
        """The run's events (as dicts), of ``kinds`` or all."""
        wanted = {kinds} if isinstance(kinds, str) else set(kinds or ())
        for event in _read_jsonl(self.directory / "events.jsonl"):
            if not wanted or event.get("event") in wanted or event.get("kind") in wanted:
                yield event

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("directory")
        return data


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                with contextlib.suppress(ValueError):
                    yield json.loads(line)


def _quote(arg: str) -> str:
    return arg if arg and all(c.isalnum() or c in "-_./:=,@%+" for c in arg) else json.dumps(arg)


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _jsonable(value: Any, depth: int = 0) -> Any:
    """``value`` as JSON; what JSON cannot hold is marked (``{"__repr__": ...}``, ``{"__truncated__": ...}``),
    so that a replay never takes it for the value."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if depth < 6 and isinstance(value, Mapping):
        return {str(k): _jsonable(v, depth + 1) for k, v in value.items()}
    if depth < 6 and isinstance(value, (list, tuple, set, frozenset, range)):
        items = [_jsonable(v, depth + 1) for v in list(value)[:_MAX_LIST]]
        return items if len(value) <= _MAX_LIST else {"__truncated__": items, "count": len(value)}
    return {"__repr__": repr(value)}


def _plain(value: Any) -> bool:
    """Whether a recorded setting is the setting itself (not marked by :func:`_jsonable`)."""
    if isinstance(value, dict):
        return "__repr__" not in value and "__truncated__" not in value and all(_plain(v) for v in value.values())
    if isinstance(value, list):
        return all(_plain(v) for v in value)
    return True


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class RunRegistry:
    """The runs kept in a workspace directory (``runs/run-N``; see the module docs).

    Args:
        directory: The workspace (``.wintergrab`` in the current directory by default).
    """

    def __init__(self, directory: str | os.PathLike[str] = DEFAULT_WORKSPACE) -> None:
        self.directory = Path(directory)
        self.runs_dir = self.directory / "runs"

    @classmethod
    def coerce(cls, value: Any) -> RunRegistry | None:
        """A spider's ``run_registry`` setting: ``True`` (the default workspace), a directory, or a registry."""
        if value is None or value is False:
            return None
        if isinstance(value, RunRegistry):
            return value
        if value is True:
            return cls()
        if isinstance(value, (str, os.PathLike)):
            return cls(value)
        raise ConfigurationError(
            f"run_registry must be True, a directory or a RunRegistry, not {value!r}", key="run_registry"
        )

    def create(
        self,
        name: str,
        *,
        recorded: bool = False,
        resumed: bool = False,
        settings: Mapping[str, Any] | None = None,
        recipe: Mapping[str, Any] | None = None,
        label: str | None = None,
    ) -> Run:
        """A new run, numbered after the last one (safe with several processes: the directory decides)."""
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        number = max((n for n, _ in self._numbered()), default=0) + 1
        while True:
            directory = self.runs_dir / f"run-{number}"
            try:
                directory.mkdir()
                break
            except FileExistsError:
                number += 1
        run = Run(f"run-{number}", number, name, time.time(), recorded=recorded, resumed=resumed,
                  settings=dict(settings or {}), recipe=dict(recipe or {}), label=label, directory=directory)  # fmt: skip
        self.save(run)
        return run

    def save(self, run: Run) -> None:
        _atomic_write(run.directory / "run.json", json.dumps(run.to_dict(), indent=1, default=str))

    def _numbered(self) -> Iterator[tuple[int, Path]]:
        if not self.runs_dir.exists():
            return
        for path in self.runs_dir.iterdir():
            if path.is_dir() and path.name.startswith("run-") and path.name[4:].isdigit():
                yield int(path.name[4:]), path

    def _load(self, directory: Path) -> Run | None:
        try:
            data = json.loads((directory / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        known = {k: v for k, v in data.items() if k in Run.__dataclass_fields__ and k != "directory"}
        return Run(**known, directory=directory)

    def get(self, ref: str | int) -> Run:
        """A run by id (``"run-7"``), number (``7``, ``"7"``), or ``"last"``."""
        text = str(ref).strip()
        if text in ("last", "latest"):
            runs = self.runs(limit=1)
            if not runs:
                raise ConfigurationError(f"no run in {self.runs_dir} yet")
            return runs[0]
        number = text[4:] if text.startswith("run-") else text
        run = self._load(self.runs_dir / f"run-{number}") if number.isdigit() else None
        if run is None:
            raise ConfigurationError(f"no run {text!r} in {self.runs_dir}")
        return run

    def runs(self, limit: int | None = None) -> list[Run]:
        """The runs, newest first."""
        found = []
        for _, directory in sorted(self._numbered(), reverse=True):
            run = self._load(directory)
            if run is not None:
                found.append(run)
                if limit is not None and len(found) >= limit:
                    break
        return found

    def remove(self, ref: str | int) -> Run:
        """Delete a run and everything it kept."""
        import shutil

        run = self.get(ref)
        shutil.rmtree(run.directory)
        return run

    def __repr__(self) -> str:
        return f"RunRegistry({str(self.directory)!r})"


# ---------------------------------------------------------------------------------------------- #
# recording (what the engine does for a registered run)
# ---------------------------------------------------------------------------------------------- #
def settings_of(spider: Spider) -> dict[str, Any]:
    """A spider's settings, as JSON values (the ones that are objects as their ``repr``)."""
    names = [n for cls in reversed(type(spider).__mro__) for n in getattr(cls, "__annotations__", {})]
    out: dict[str, Any] = {}
    for name in dict.fromkeys(names):
        if name.startswith("_") or name in _SKIPPED_SETTINGS or not hasattr(spider, name):
            continue
        value = getattr(spider, name)
        if callable(value) and not isinstance(value, type):
            continue
        out[name] = _jsonable(value)
    return out


def recipe_of(spider: Spider) -> dict[str, Any]:
    """How to run ``spider`` again: its ``run_recipe``, or its class when it can be imported."""
    if spider.run_recipe:
        return dict(spider.run_recipe)
    cls = type(spider)
    module = cls.__module__
    if module.startswith("wintergrab_user_"):  # loaded from a file by the command line
        with contextlib.suppress(TypeError, OSError):
            return {"spider": f"{inspect.getfile(cls)}:{cls.__qualname__}"}
    if module != "__main__" and "<locals>" not in cls.__qualname__:
        return {"spider": f"{module}:{cls.__qualname__}"}
    return {}


class RunRecorder:
    """Keeps a run's record while the engine crawls (used by the engine; see the module docs)."""

    def __init__(self, registry: RunRegistry, spider: Spider, *, record: bool) -> None:
        self.registry = registry
        self.spider = spider
        self.record = record
        self.run: Run | None = None
        self.archive: HTTPCache | None = None
        self._items: TextIO | None = None

    def start(self, *, resumed: bool) -> Run:
        spider = self.spider
        self.run = self.registry.create(
            spider.name or type(spider).__name__,
            recorded=self.record,
            resumed=resumed,
            settings=settings_of(spider),
            recipe=recipe_of(spider),
            label=spider.run_label,
        )
        if self.record:
            from .fetchers.cache import HTTPCache

            # every response, whatever its status or caching headers: the run's own copy
            self.archive = HTTPCache(self.run.archive, mode="refresh", statuses=_ALL_STATUSES, methods=_ALL_METHODS,
                                     respect_no_store=False)  # fmt: skip
            spider.cache = self.archive
            spider._http_cache = None
            self._items = (self.run.directory / "items.jsonl").open("w", encoding="utf-8")
        return self.run

    def event_kinds(self) -> frozenset[str]:
        """The events worth keeping: per-response ones only when recording (their timings)."""
        from .events import EVENT_KINDS, HIGH_VOLUME

        kinds = EVENT_KINDS - HIGH_VOLUME
        return kinds | {"response"} if self.record else kinds

    def item(self, item: Any) -> None:
        if self._items is not None:
            from .spider.exporters import to_dict

            self._items.write(json.dumps(to_dict(item), ensure_ascii=False, default=str) + "\n")

    def finish(self, result: CrawlResult | None, *, status: str, error: BaseException | None = None) -> None:
        run = self.run
        if run is None:
            return
        if self._items is not None:
            self._items.close()
            self._items = None
        if self.archive is not None:
            self.archive.close()
        run.finished = time.time()
        run.status = "failed" if error is not None else status
        run.error = f"{type(error).__name__}: {error}" if error is not None else None
        if result is not None:
            run.stats = _jsonable(dict(result.stats))
            run.failures = [
                {"signature": d.signature, "domain": d.domain, "urls": d.affected_urls,
                 "cause": d.confirmed_cause or d.likely_cause}
                for d in result.failures[:20]
            ]  # fmt: skip
        output = self.spider.output
        run.output = None if output in (None, "-") else str(output)
        self.registry.save(run)


# ---------------------------------------------------------------------------------------------- #
# replay
# ---------------------------------------------------------------------------------------------- #
@dataclass
class ReplayResult:
    """What replaying a recorded run gave.

    Attributes:
        run: The run replayed.
        crawl: The replay's :class:`~wintergrab.spider.CrawlResult`.
        output: Where the replay's items are (JSON Lines).
        diff: How they differ from the recorded ones (:class:`~wintergrab.data.DatasetDiff`).
        missing: Requests whose response is not in the recording: the replayed crawl went further
            than the run (expected when the run stopped at a limit), or elsewhere.
    """

    run: Run
    crawl: CrawlResult | None
    output: Path
    diff: DatasetDiff
    missing: int = 0

    @property
    def limited(self) -> bool:
        """The run stopped at a limit (``max_pages``...): it did not fetch every page it found."""
        return self.run.status == "limit"

    @property
    def same(self) -> bool:
        """The replay wrote the items the run wrote, and (unless the run stopped at a limit) asked for
        no page the run had not fetched."""
        return not self.diff and (self.limited or not self.missing)

    def summary(self) -> str:
        pages = self.crawl.stats.get("responses", 0) if self.crawl is not None else 0  # misses aside
        head = f"replayed {self.run.id} ({pages:,} page(s) from the recording): "
        beyond = (
            f"; {self.missing:,} request(s) went beyond the recording (the run stopped at "
            f"{self.run.stats.get('limit_reason') or 'a limit'})"
            if self.missing and self.limited
            else ""
        )
        if self.same:
            return head + f"the same {self.diff.unchanged:,} item(s) as recorded" + beyond
        lines = [head + self.diff.summary() + " (recorded -> replayed)"]
        if self.missing and not self.limited:
            lines.append(f"{self.missing:,} request(s) asked for pages the recording does not have")
        if self.diff:
            lines.append(self.diff.describe())
        return "\n".join(lines)


def load_spider(reference: str) -> type[Spider]:
    """A spider class from ``"module:Class"`` or ``"path/to/file.py:Class"``."""
    target, _, name = reference.rpartition(":")
    if not target or not name:
        raise ConfigurationError(f"a spider is 'module:Class' or 'file.py:Class', not {reference!r}")
    if target.endswith(".py"):
        from .cli import load_spider_class

        return load_spider_class(f"{target}:{name}")
    module = importlib.import_module(target)
    found: Any = module
    for part in name.split("."):
        found = getattr(found, part)
    return found  # type: ignore[no-any-return]


def replay(
    run: Run | str | int,
    spider: type[Spider] | None = None,
    *,
    registry: RunRegistry | str | os.PathLike[str] | None = None,
    output: str | os.PathLike[str] | None = None,
    key: str | Sequence[str] | None = None,
    **settings: Any,
) -> ReplayResult:
    """Crawl again from what ``run`` recorded, without touching the network, and compare the items.

    Args:
        run: The run (or its id).
        spider: The spider class to replay with (by default, what the run says: its command line, its
            spider class, or its goal plan).
        registry: Where the run is (``.wintergrab`` by default).
        output: Where to write the replay's items (by default ``replay-<time>.jsonl`` in the run's directory).
        key: The field(s) identifying an item, to compare (by default the spider's ``unique_key``, else
            ``url`` when every item has one).
        settings: More settings for the replay's spider.
    """
    from .data.versions import diff_records
    from .fetchers.cache import HTTPCache

    reg = registry if isinstance(registry, RunRegistry) else RunRegistry(registry or DEFAULT_WORKSPACE)
    the_run = run if isinstance(run, Run) else reg.get(run)
    if not the_run.recorded or not (the_run.archive / "http.sqlite3").exists():
        raise ConfigurationError(f"{the_run.id} was not recorded (record=True): there is nothing to replay from")
    target = Path(output) if output else _free(the_run.directory / f"replay-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    archive = HTTPCache(the_run.archive, mode="offline", statuses=_ALL_STATUSES, methods=_ALL_METHODS,
                        respect_no_store=False)  # fmt: skip
    overrides: dict[str, Any] = {
        "cache": archive,
        "output": str(target),
        "keep_items": False,
        "crawl_dir": None,
        "history": None,
        "skip_fresh": False,
        "profile": None,
        "event_log": None,
        "record": False,
        "run_registry": None,
        "run_recipe": None,
        "run_label": None,
        "webhooks": (),  # a replay tells no one
        "retry_dead_letters": False,
        "network_policy": None,  # nothing reaches the network: no address to check
        # the recording says which pages: no page limits (max_items stays: it shapes the output)
        "max_pages": None,
        "max_requests": None,
        "max_bytes": None,
        "max_runtime": None,
        "progress": False,
        "log_level": "WARNING",
        **settings,
    }
    for name in ("adaptive_fetch", "optimize"):  # learn afresh, without writing the run's files
        if isinstance(the_run.settings.get(name), str):
            overrides.setdefault(name, True)
    try:
        crawl = _replay_crawl(the_run, spider, overrides)
    finally:
        archive.close()
    recorded = list(the_run.items())
    replayed = list(_read_jsonl(target))
    if key is None:
        unique = the_run.settings.get("unique_key")
        if isinstance(unique, (str, list)) and unique:
            key = unique
        elif recorded and all(isinstance(r, dict) and r.get("url") for r in [*recorded, *replayed]):
            key = "url"
    diff = diff_records(recorded, replayed, key=key)
    # pages asked for that the recording does not have (offline: CacheMiss, category "cache")
    missing = sum(d.affected_urls for d in (crawl.failures if crawl is not None else []) if d.category == "cache")
    return ReplayResult(the_run, crawl, target, diff, missing)


def _free(path: Path) -> Path:
    """``path``, or ``path`` with ``-2``, ``-3``... when it exists (two replays in the same second)."""
    candidate, n = path, 1
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
    return candidate


def _recorded_settings(run: Run) -> dict[str, Any]:
    """The run's settings a replay can take again (plain values, not marked ones)."""
    return {k: v for k, v in run.settings.items() if k not in _NOT_REPLAYED and _plain(v)}


def _replay_crawl(run: Run, spider: type[Spider] | None, overrides: dict[str, Any]) -> CrawlResult | None:
    recipe = run.recipe
    if spider is not None:
        return spider(**{**_recorded_settings(run), **overrides}).run(resume=False)
    if recipe.get("command"):
        from .cli import spider_from_command

        cls, settings = spider_from_command(list(recipe["command"]))
        return cls(**{**settings, **overrides}).run(resume=False)
    if recipe.get("goal_plan"):
        from .goals.plan import GoalPlan
        from .goals.run import run_plan

        plan = GoalPlan.from_dict(recipe["goal_plan"])
        options = {k: v for k, v in overrides.items() if k not in ("output", "keep_items", "log_level", "progress")}
        if "obey_robots_txt" in run.settings:
            options.setdefault("obey_robots_txt", run.settings["obey_robots_txt"])
        if isinstance(run.settings.get("optimize"), bool):
            options.setdefault("optimize", run.settings["optimize"])
        result = run_plan(plan, overrides["output"], keep_items=False, log_level=overrides.get("log_level"),
                          progress=False, **options)  # fmt: skip
        return result.crawl
    if recipe.get("spider"):
        cls = load_spider(str(recipe["spider"]))
        return cls(**{**_recorded_settings(run), **overrides}).run(resume=False)
    raise ConfigurationError(
        f"{run.id} does not say how to run it again (a spider defined in a script): pass its class, "
        f"replay({run.id!r}, MySpider)"
    )
