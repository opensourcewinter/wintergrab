"""Projects: one file that says what to crawl, when, and whom to tell.

::

    # wintergrab.yaml
    defaults:                               # options for every job
      concurrency: 8
    webhooks:
      - url: https://hooks.example/wintergrab
        events: [job_finished, job_failed, record_updated]
        secret: ${WINTERGRAB_WEBHOOK_SECRET}
    jobs:
      prices:
        crawl: https://shop.example/        # like: wintergrab crawl https://shop.example/ --extract ...
        extract: product.schema.json
        follow: [a.product]
        paginate: true
        output: data/prices.jsonl
        history: data/prices.history      # what changed since the last run (record_* events)
        schedule: every 2 hours
      books:
        goal: books rated 4 stars or more on books.example
        output: data/books.jsonl
        schedule: daily at 06:00
      shop:
        spider: spiders/shop.py:Shop        # your own spider
        set: {max_pages: 1000}              # any spider setting
        schedule: "*/30 * * * *"

``wintergrab run`` runs the jobs now (or those named), ``wintergrab schedule`` runs them on their
schedules until stopped (see :mod:`wintergrab.schedules`), ``wintergrab init`` writes a project to
start from.

A job is a command line written as a mapping: ``crawl: URL`` (or ``goal: TEXT``, or
``spider: FILE.py:Class``) and the command's options, ``follow: [a, b]`` for ``--follow a --follow b``,
``paginate: true`` for ``--paginate``; ``set:`` holds spider settings (``--set``). Each job runs in
a process of its own, in the project's directory, and its run is kept in the project's workspace
(``.wintergrab``, see :mod:`wintergrab.runs`). ``defaults`` are options every job has unless it
says otherwise. ``${NAME}`` in a webhook is the environment variable ``NAME``: keep secrets there,
not in the file (a job's options are its command line, which others on the machine can read, so
they take no variables). The file can be YAML (``wintergrab.yaml``), TOML or JSON.

Webhooks (:mod:`wintergrab.webhooks`) get the jobs' events (``job_started``, ``job_finished``,
``job_failed``, from whatever runs the jobs) and their crawls' (``crawl_finished``,
``record_updated``..., from each job's own process).
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .files import read_structured
from .redact import redact_argv
from .runs import DEFAULT_WORKSPACE, Run, RunRegistry
from .schedules import Cron, Schedule, parse_duration, parse_schedule
from .utils import replace_file
from .webhooks import Webhook

__all__ = ["PROJECT_FILES", "Job", "JobResult", "Project", "Scheduler", "find_project", "starter_project"]

log = logging.getLogger("wintergrab.project")

#: The files a project is looked for in, in this order.
PROJECT_FILES = ("wintergrab.yaml", "wintergrab.yml", "wintergrab.toml", "wintergrab.json")
_KINDS = ("crawl", "goal", "spider")
_JOB_KEYS = frozenset({*_KINDS, "schedule", "timezone", "description", "enabled", "set", "start_within"})
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: Any, where: str) -> Any:
    """``${NAME}`` in strings: the environment variable (an error when it is not set)."""
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigurationError(f"{where}: the environment variable {name} is not set")
            return os.environ[name]

        return _VARIABLE.sub(replace, value)
    if isinstance(value, Mapping):
        return {k: _expand(v, where) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, where) for v in value]
    return value


@dataclass
class Job:
    """One job of a project (see the module docs).

    Attributes:
        name: The job's name in the file.
        kind: ``"crawl"``, ``"goal"`` or ``"spider"``.
        target: The URL, the request, or ``FILE.py:Class``.
        options: The command's options (``{"follow": [...], "output": ...}``).
        settings: Spider settings (``set:``).
        schedule: When it runs (``None``: only when asked).
        enabled: ``False`` keeps it from its schedule (``wintergrab run JOB`` still runs it).
        start_within: Skip a time the job would start later than this after it (default: a missed
            time is made up for, once, as soon as the scheduler runs).
    """

    name: str
    kind: str
    target: str
    options: dict[str, Any] = dataclass_field(default_factory=dict)
    settings: dict[str, Any] = dataclass_field(default_factory=dict)
    schedule: Schedule | None = None
    enabled: bool = True
    start_within: timedelta | None = None
    description: str = ""

    def command(self, *, workspace: str | None = None, project: str | None = None) -> list[str]:
        """The ``wintergrab`` command line that runs the job."""
        args = ["goal", self.target, "--yes"] if self.kind == "goal" else ["crawl", self.target]
        for key, value in self.options.items():
            flag = "--" + key.replace("_", "-")
            if value is True:
                args.append(flag)
            elif value is False or value is None:
                continue
            elif isinstance(value, (list, tuple)):
                for item in value:
                    args.extend([flag, str(item)])
            else:
                args.extend([flag, str(value)])
        for key, value in self.settings.items():
            args.extend(["--set", f"{key}={json.dumps(value)}"])
        if workspace:
            args.extend(["--workspace", workspace])
        if project:
            args.extend(["--project", project, "--job", self.name])
        return args


@dataclass
class JobResult:
    """What running a job gave."""

    job: Job
    status: str  # "finished" (exit 0), "failed", or the run's status ("limit", "paused"...)
    exit_code: int
    started: float
    finished: float
    run: Run | None = None
    log: Path | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def describe(self) -> str:
        took = f"{self.finished - self.started:.1f} s"
        facts = ""
        if self.run is not None:
            s = self.run.stats
            facts = f", {s.get('pages', 0):,} pages, {s.get('items', 0):,} items ({self.run.id})"
        failed = f" (exit status {self.exit_code})" if not self.ok else ""
        return f"{self.job.name}: {self.status}{failed} in {took}{facts}"


class Project:
    """A project file (see the module docs)."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).resolve()
        self.directory = self.path.parent
        data = read_structured(self.path)
        if not isinstance(data, Mapping):
            raise ConfigurationError(f"{self.path}: a project is a mapping (jobs:, webhooks:...)")
        known = {"workspace", "defaults", "jobs", "webhooks", "timezone", "name"}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigurationError(
                f"{self.path}: unknown section(s) {', '.join(unknown)} (known: {', '.join(sorted(known))})"
            )
        self.name = str(data.get("name") or self.directory.name)
        self.workspace = self.directory / str(data.get("workspace") or DEFAULT_WORKSPACE)
        self.timezone = data.get("timezone")
        defaults = data.get("defaults") or {}
        if not isinstance(defaults, Mapping):
            raise ConfigurationError(f"{self.path}: defaults is a mapping of options")
        either = {**_command_options("crawl"), **_command_options("goal")}
        for key in defaults:
            if key not in either:
                close = difflib.get_close_matches(key, list(either), n=1)
                hint = f" (did you mean {close[0]!r}?)" if close else ""
                raise ConfigurationError(f"{self.path.name}, defaults: no command has the option {key!r}{hint}")
        self.webhook_settings = list(data.get("webhooks") or [])
        jobs = data.get("jobs") or {}
        if not isinstance(jobs, Mapping) or not jobs:
            raise ConfigurationError(f"{self.path}: no jobs (jobs: {{name: {{crawl: URL, ...}}}})")
        self.jobs: dict[str, Job] = {}
        for name, spec in jobs.items():
            self.jobs[str(name)] = self._job(str(name), spec, defaults)

    def _job(self, name: str, spec: Any, defaults: Mapping[str, Any]) -> Job:
        where = f"{self.path.name}, job {name!r}"
        if not isinstance(spec, Mapping):
            raise ConfigurationError(f"{where}: a job is a mapping (crawl: URL, ...)")
        kinds = [k for k in _KINDS if k in spec]
        if len(kinds) != 1:
            raise ConfigurationError(
                f"{where}: say what it does, with one of crawl: URL, goal: TEXT, spider: FILE.py:Class"
            )
        kind = kinds[0]
        command = "goal" if kind == "goal" else "crawl"
        known = _command_options(command)
        # defaults apply where the job's command has the option (a goal has no --concurrency)
        options = {k: v for k, v in defaults.items() if k in known and k not in _JOB_KEYS}
        options.update({k: v for k, v in spec.items() if k not in _JOB_KEYS})
        _check_options(where, command, options)
        settings = dict(spec.get("set") or {})
        if _VARIABLE.search(json.dumps([spec[kind], options, settings], default=str)):
            raise ConfigurationError(
                f"{where}: ${{NAME}} is read in webhooks only: a job's options are its command line, which "
                "others on the machine can read (keep secrets in files the job reads, like --proxy-file)"
            )
        schedule = start_within = None
        try:
            if spec.get("schedule") is not None:
                schedule = parse_schedule(spec["schedule"], timezone=spec.get("timezone") or self.timezone)
            if spec.get("start_within") is not None:
                if not isinstance(schedule, Cron):
                    raise ConfigurationError("start_within is for schedules at set times (cron, 'daily at 06:00')")
                start_within = parse_duration(spec["start_within"])
        except ConfigurationError as exc:
            raise ConfigurationError(f"{where}: {exc}") from exc
        return Job(name=name, kind=kind, target=str(spec[kind]), options=options, settings=settings, schedule=schedule,
                   enabled=bool(spec.get("enabled", True)), start_within=start_within,
                   description=str(spec.get("description") or ""))  # fmt: skip

    def webhooks(self) -> list[Webhook]:
        """The project's webhooks (``${NAME}`` read from the environment now)."""
        return [Webhook.coerce(_expand(w, f"{self.path.name}, webhooks")) for w in self.webhook_settings]

    def select(self, names: Iterable[str] | None) -> list[Job]:
        wanted = list(names or [])
        if not wanted:
            return list(self.jobs.values())
        missing = [n for n in wanted if n not in self.jobs]
        if missing:
            raise ConfigurationError(f"no job {', '.join(missing)} in {self.path.name} (jobs: {', '.join(self.jobs)})")
        return [self.jobs[n] for n in wanted]

    # -- running ------------------------------------------------------------------------------- #
    def run_job(self, job: Job, *, log_file: Path | None = None, runner: Callable[..., int] | None = None) -> JobResult:
        """Run ``job`` in a process of its own (its output to ``log_file``, or through)."""
        command = job.command(workspace=str(self.workspace), project=str(self.path))
        started = time.time()
        if runner is not None:
            code = runner(command, cwd=self.directory, log_file=log_file)
        else:
            code = _run_command(command, cwd=self.directory, log_file=log_file)
        finished = time.time()
        run = self._run_of(job, started)
        status = "failed" if code != 0 else (run.status if run is not None else "finished")
        return JobResult(job, status, code, started, finished, run, log_file)

    def _run_of(self, job: Job, since: float) -> Run | None:
        """The run the job made (the newest run with its label, started since ``since``)."""
        for run in RunRegistry(self.workspace).runs(limit=20):
            if run.label == job.name and run.started >= since - 1:
                return run
        return None

    def __repr__(self) -> str:
        return f"Project({str(self.path)!r}, jobs={list(self.jobs)})"


def _run_command(command: Sequence[str], *, cwd: Path, log_file: Path | None) -> int:
    argv = [sys.executable, "-m", "wintergrab", *command]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if log_file is None:
        return subprocess.run(argv, cwd=cwd, env=env, check=False).returncode
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab") as out:
        out.write(f"$ wintergrab {' '.join(redact_argv(command))}\n".encode())
        out.flush()
        return subprocess.run(argv, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT, check=False).returncode


_OPTIONS: dict[str, dict[str, bool]] = {}


def _command_options(command: str) -> dict[str, bool]:
    """The options of ``wintergrab COMMAND``: name -> whether it takes a value."""
    if command not in _OPTIONS:
        from .cli import build_parser

        parser = build_parser()
        sub: Any = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
        found: dict[str, bool] = {}
        for action in sub.choices[command]._actions:
            for flag in action.option_strings:
                if flag.startswith("--"):
                    found[flag[2:].replace("-", "_")] = action.nargs != 0
        _OPTIONS[command] = found
    return _OPTIONS[command]


def _check_options(where: str, command: str, options: Mapping[str, Any]) -> None:
    known = _command_options(command)
    for key in options:
        if key in ("project", "job", "workspace", "help"):
            raise ConfigurationError(f"{where}: {key!r} is set by the project itself")
        if key not in known:
            close = difflib.get_close_matches(key, list(known), n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else f" (see wintergrab {command} --help)"
            raise ConfigurationError(f"{where}: wintergrab {command} has no option {key!r}{hint}")


def find_project(path: str | os.PathLike[str] | None = None) -> Project:
    """The project in ``path`` (a file, or a directory holding ``wintergrab.yaml``...), or the
    current directory's."""
    target = Path(path) if path else Path.cwd()
    if target.is_file():
        return Project(target)
    for name in PROJECT_FILES:
        if (target / name).is_file():
            return Project(target / name)
    raise ConfigurationError(f"no project in {target}: create {PROJECT_FILES[0]} (wintergrab init) or name the file")


# ---------------------------------------------------------------------------------------------- #
# the scheduler
# ---------------------------------------------------------------------------------------------- #
class Scheduler:
    """Runs a project's jobs on their schedules (see the module docs).

    Args:
        project: The project.
        now: The clock (``datetime.now`` by default; tests give their own).
        sleep: Waits (``time.sleep`` by default).
        runner: Runs a job's command line (``runner(command, cwd=..., log_file=...) -> exit code``);
            by default a process of its own.
    """

    def __init__(
        self,
        project: Project,
        *,
        now: Callable[[], datetime] = datetime.now,
        sleep: Callable[[float], None] = time.sleep,
        runner: Callable[..., int] | None = None,
        webhooks: Sequence[Webhook] | None = None,
    ) -> None:
        self.project = project
        self.now = now
        self.sleep = sleep
        self.runner = runner
        self.webhooks = list(webhooks) if webhooks is not None else project.webhooks()
        self.state_path = project.workspace / "schedule.json"
        self.state: dict[str, dict[str, Any]] = self._load()
        self.started = now()
        stamp = self.started.isoformat()
        for job in project.jobs.values():  # when each job was first scheduled: its cron times count from then
            if job.schedule is not None:
                self.state.setdefault(job.name, {}).setdefault("first_seen", stamp)
        self.results: list[JobResult] = []
        self._stop = False

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return dict(data.get("jobs") or {})
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"jobs": self.state}, indent=1), encoding="utf-8")
        replace_file(tmp, self.state_path)  # (the dashboard may be reading it)

    def last_run(self, job: Job) -> datetime | None:
        text = (self.state.get(job.name) or {}).get("last_run")
        return datetime.fromisoformat(text) if text else None

    def due(self, job: Job, now: datetime) -> datetime | None:
        """When ``job`` runs next (``None``: never). An interval job runs right away the first time,
        and at once when overdue. A cron job (a time of day...) runs at its times from when it last
        ran (or was first scheduled). A time that went by while nothing ran (the machine was off, a
        long job ran) is made up for once, as soon as the scheduler runs, unless the job would
        start more than its ``start_within`` late: then it waits for its next time."""
        if job.schedule is None or not job.enabled:
            return None
        last = self.last_run(job)
        if not isinstance(job.schedule, Cron):
            return job.schedule.next(after=now, last=last)
        seen = (self.state.get(job.name) or {}).get("first_seen")
        since = last or (datetime.fromisoformat(seen) if seen else self.started)
        when = job.schedule.next(after=since)
        if when is None or when > now:
            return when
        if job.start_within is not None and now - when > job.start_within:
            when = job.schedule.next(after=now - job.start_within)  # the first time not too late for
            if when is None or when > now:
                return when
        return now

    def plan(self) -> list[tuple[Job, datetime | None]]:
        """Every scheduled job and when it runs next, soonest first."""
        now = self.now()
        rows = [(job, self.due(job, now)) for job in self.project.jobs.values() if job.schedule is not None]
        return sorted(rows, key=lambda r: (r[1] is None, r[1] or now))

    def run_due(self) -> list[JobResult]:
        """Run the jobs that are due now, one after the other."""
        ran = []
        for job, when in self.plan():
            if when is None or when > self.now():
                continue
            ran.append(self.run(job))
        self._save()
        return ran

    def run(self, job: Job, *, logged: bool = True) -> JobResult:
        """Run ``job`` now (its output in the workspace's ``logs/``, or through with ``logged=False``),
        tell the webhooks, and remember when."""
        stamp = self.now()
        log_file = self.project.workspace / "logs" / f"{job.name}-{stamp:%Y%m%d-%H%M%S}.log" if logged else None
        self._tell("job_started", job=job.name, command=redact_argv(job.command()))
        result = self.project.run_job(job, log_file=log_file, runner=self.runner)
        entry = self.state.setdefault(job.name, {})
        entry.update(last_run=stamp.isoformat(), last_status=result.status, exit_code=result.exit_code,
                     run=result.run.id if result.run else None)  # fmt: skip
        self._save()
        facts = {"job": job.name, "status": result.status, "exit_code": result.exit_code,
                 "seconds": round(result.finished - result.started, 3), "run": result.run.id if result.run else None,
                 "log": str(log_file) if log_file else None}  # fmt: skip
        if result.run is not None:
            facts["stats"] = {k: result.run.stats.get(k) for k in ("pages", "items", "errors") if k in result.run.stats}
        self._tell("job_finished" if result.ok else "job_failed", **facts)
        log.info("%s", result.describe())
        self.results.append(result)
        return result

    def _tell(self, kind: str, **data: Any) -> None:
        from .events import Event

        event = Event(kind, data, origin=self.project.name)
        for hook in self.webhooks:
            kinds = hook.kinds()
            if kinds is None or kind in kinds:
                hook(event)

    def loop(self, *, until: Callable[[], bool] | None = None) -> None:
        """Run jobs as they fall due, until stopped (Ctrl+C, :meth:`stop`, or ``until()``)."""
        try:
            while not self._stop and not (until is not None and until()):
                self.run_due()
                upcoming = [when for _, when in self.plan() if when is not None]
                if not upcoming:
                    log.warning("no job is scheduled any more")
                    return
                wait = (min(upcoming) - self.now()).total_seconds()
                self.sleep(max(1.0, min(wait, 60.0)))  # wake at least every minute: the clock may jump
        finally:
            for hook in self.webhooks:
                hook.close()

    def stop(self) -> None:
        self._stop = True


def starter_project() -> str:
    """The ``wintergrab.yaml`` that ``wintergrab init`` writes."""
    return """\
# A wintergrab project: what to crawl, when, and whom to tell.
# Run the jobs now:           wintergrab run [JOB...]
# Run them on their schedule: wintergrab schedule
# Every run is kept in .wintergrab (wintergrab runs).

defaults:                         # options for every job (those of wintergrab crawl)
  concurrency: 8

# webhooks:                       # events posted as JSON (see docs/projects.md)
#   - url: https://hooks.example/wintergrab
#     events: [job_finished, job_failed, record_updated]
#     secret: ${WINTERGRAB_WEBHOOK_SECRET}

jobs:
  example:
    crawl: https://books.toscrape.com/      # like: wintergrab crawl URL --follow ... -o ...
    follow: [".product_pod h3 a", ".next a"]
    auto: true
    max_pages: 50
    output: data/example.jsonl
    history: data/example.history          # what changed since the last run
    schedule: daily at 06:00                # or "every 2 hours", or cron: "0 */2 * * *"
"""
