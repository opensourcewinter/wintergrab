"""A dashboard: what the crawls of a workspace did, and what one is doing now.

::

    $ wintergrab dashboard
    wintergrab dashboard: http://127.0.0.1:8710/  (.wintergrab; Ctrl+C to stop)

The first page lists the runs (and a project's jobs, when to run next, how they last went). Each
run has its numbers (pages, success, failures, blocked, requests per second, latency, records,
browser pages, data quality) and what lies under them: its failures and their likely causes, its
domains and how each was throttled, its extraction (pages with no complete record, quality,
fields that came or went), what changed since the last run, and its events. A running crawl's
page refreshes itself, from the metrics the crawl keeps every two seconds.

The same as JSON: ``/api/runs``, ``/api/runs/RUN`` and ``/api/jobs``.

It is for this machine: it listens on 127.0.0.1, only answers requests addressed to it (a web page
elsewhere cannot reach it through DNS rebinding), changes nothing, and shows what crawls
collected as text, never as markup or scripts.
"""

from __future__ import annotations

import html
import json
import logging
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from .errors import ConfigurationError
from .redact import redact, redact_argv
from .runs import DEFAULT_WORKSPACE, Run, RunRegistry

__all__ = ["Dashboard", "EventSummary", "serve", "summarize_events"]

log = logging.getLogger("wintergrab.dashboard")

DEFAULT_PORT = 8710
_MAX_EVENT_BYTES = 64 * 1024 * 1024  # the events read per run page (the last ones, past that)
_STALE_AFTER = 30.0  # seconds without new metrics: a "running" run's process has probably stopped
_SAMPLES = 8  # URLs shown per group
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_NONE = "\u2013"  # an en dash: no value
_MISSING = object()


# ---------------------------------------------------------------------------------------------- #
# what a run's events say
# ---------------------------------------------------------------------------------------------- #
@dataclass
class EventSummary:
    """A run's events, summed up (see :func:`summarize_events`)."""

    counts: Counter[str] = field(default_factory=Counter)
    #: ``request_failed`` grouped by (category, kind, status): ``{"category", ..., "count", "urls"}``.
    failures: list[dict[str, Any]] = field(default_factory=list)
    blocked: Counter[str] = field(default_factory=Counter)  # domain -> responses
    backoffs: Counter[str] = field(default_factory=Counter)  # domain -> push-backs
    #: Pages with no complete record: ``{"pages", "fields": {field: pages}, "urls"}``.
    extraction: dict[str, Any] = field(default_factory=dict)
    changes: dict[str, Any] | None = None  # the ``changes_detected`` event
    updated: list[dict[str, Any]] = field(default_factory=list)  # a sample of ``record_updated``
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    quality: list[dict[str, Any]] = field(default_factory=list)  # ``quality_degraded``
    schema: list[dict[str, Any]] = field(default_factory=list)  # ``schema_changed``
    notable: list[dict[str, Any]] = field(default_factory=list)  # budgets, refusals, the browser...
    truncated: bool = False  # only the last events were read

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": dict(self.counts),
            "failures": self.failures,
            "blocked": dict(self.blocked),
            "backoffs": dict(self.backoffs),
            "extraction": self.extraction,
            "changes": self.changes,
            "record_updated": self.updated,
            "record_created": self.created,
            "record_deleted": self.deleted,
            "quality_degraded": self.quality,
            "schema_changed": self.schema,
            "notable": self.notable,
            "truncated": self.truncated,
        }


_NOTABLE = ("budget_exhausted", "policy_refused", "browser_needed", "item_dropped", "pipeline_report")


def summarize_events(path: Path, *, max_bytes: int = _MAX_EVENT_BYTES) -> EventSummary:
    """Sum up a run's ``events.jsonl`` (its last ``max_bytes`` when it is bigger)."""
    summary = EventSummary()
    failures: dict[tuple[Any, ...], dict[str, Any]] = {}
    missing: Counter[str] = Counter()
    failed_urls: list[str] = []
    try:
        handle = path.open("rb")
    except OSError:
        return summary
    with handle:
        size = path.stat().st_size
        if size > max_bytes:
            handle.seek(size - max_bytes)
            handle.readline()  # a partial line
            summary.truncated = True
        for raw in handle:
            if raw.startswith(b'{"event": "response"'):  # one per page: counted, not read
                summary.counts["response"] += 1
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            kind = str(event.get("event"))
            summary.counts[kind] += 1
            if kind == "request_failed":
                key = (event.get("category"), event.get("kind"), event.get("status"), event.get("error"))
                group = failures.setdefault(key, {"category": key[0], "kind": key[1], "status": key[2],
                                                  "error": key[3], "count": 0, "urls": []})  # fmt: skip
                group["count"] += 1
                if len(group["urls"]) < _SAMPLES:
                    group["urls"].append(event.get("url"))
            elif kind == "blocked":
                summary.blocked[str(event.get("domain"))] += 1
            elif kind == "throttle_backoff":
                summary.backoffs[str(event.get("domain"))] += 1
            elif kind == "extraction_failed":
                missing.update(event.get("missing") or ["?"])
                if len(failed_urls) < _SAMPLES:
                    failed_urls.append(event.get("url"))
            elif kind == "changes_detected":
                summary.changes = {k: v for k, v in event.items() if k not in ("event", "time", "origin")}
            elif kind == "record_updated" and len(summary.updated) < 50:
                summary.updated.append({"url": event.get("url"), "kinds": event.get("kinds"),
                                        "details": event.get("details")})  # fmt: skip
            elif kind == "record_created" and len(summary.created) < 50:
                summary.created.append(event.get("url"))
            elif kind == "record_deleted" and len(summary.deleted) < 50:
                summary.deleted.append(event.get("url"))
            elif kind == "quality_degraded":
                summary.quality.append({k: event.get(k) for k in ("dataset", "field", "code", "message", "severity")})
            elif kind == "schema_changed":
                summary.schema.append({k: event.get(k) for k in ("dataset", "added", "removed", "retyped")})
            elif kind in _NOTABLE and len(summary.notable) < 200:
                summary.notable.append(event)
    summary.failures = sorted(failures.values(), key=lambda g: -g["count"])
    if summary.counts["extraction_failed"]:
        summary.extraction = {"pages": summary.counts["extraction_failed"], "fields": dict(missing.most_common()),
                              "urls": failed_urls}  # fmt: skip
    return summary


# ---------------------------------------------------------------------------------------------- #
# the data behind the pages
# ---------------------------------------------------------------------------------------------- #
class Dashboard:
    """What the dashboard shows: a workspace's runs, and a project's jobs.

    Args:
        workspace: The workspace (``.wintergrab``).
        project: A :class:`~wintergrab.project.Project` (its jobs are shown), or ``None``.
    """

    def __init__(self, workspace: str | Path = DEFAULT_WORKSPACE, project: Any = None) -> None:
        self.workspace = Path(workspace)
        self.registry = RunRegistry(self.workspace)
        self.project = project

    def runs(self, limit: int = 200) -> list[Run]:
        return self.registry.runs(limit=limit)

    def run(self, ref: str) -> Run:
        return self.registry.get(ref)

    def state(self, run: Run) -> str:
        """The run's status; ``"not responding"`` for a run that says it runs but keeps no metrics."""
        if run.status != "running":
            return run.status
        path = run.directory / "metrics.json"
        try:
            last = path.stat().st_mtime
        except OSError:
            last = run.started
        return "running" if time.time() - last < _STALE_AFTER else "not responding"

    def jobs(self) -> list[dict[str, Any]]:
        """The project's jobs: what each does, its schedule, when it runs next, and its last run."""
        if self.project is None:
            return []
        from .project import Scheduler

        scheduler = Scheduler(self.project, webhooks=[])
        now = scheduler.now()
        plan = dict((job.name, when) for job, when in scheduler.plan())
        rows = []
        for job in self.project.jobs.values():
            state = scheduler.state.get(job.name) or {}
            when = plan.get(job.name)
            rows.append({
                "job": job.name,
                "kind": job.kind,
                "target": redact(job.target),
                "schedule": str(job.schedule) if job.schedule is not None else None,
                "trigger": job.trigger(),
                "enabled": job.enabled,
                "next": None if when is None else ("now" if when <= now else when.isoformat(timespec="minutes")),
                "last_run": state.get("last_run"),
                "last_status": state.get("last_status"),
                "run": state.get("run"),
            })  # fmt: skip
        return rows

    def summary(self, run: Run) -> dict[str, Any]:
        """A run's record without credentials (the ones kept before runs left them out, too)."""
        data = redact(run.to_dict())
        recipe = dict(data.get("recipe") or {})
        if isinstance(recipe.get("command"), list):
            recipe["command"] = redact_argv(run.recipe["command"])
        data["recipe"] = recipe
        data["state"] = self.state(run)
        return data  # type: ignore[no-any-return]

    def run_data(self, run: Run) -> dict[str, Any]:
        """Everything about a run, as JSON values (the run page, and ``/api/runs/RUN``)."""
        data = self.summary(run)
        data["metrics"] = run.metrics()
        data["events"] = summarize_events(run.directory / "events.jsonl").to_dict()
        data["output_bytes"] = self._output_size(run)
        return data

    def _output_size(self, run: Run) -> int | None:
        if not run.output:
            return None
        path = Path(run.output)
        for candidate in (path, self.workspace.resolve().parent / path):
            try:
                return candidate.stat().st_size
            except OSError:
                continue
        return None


# ---------------------------------------------------------------------------------------------- #
# HTML
# ---------------------------------------------------------------------------------------------- #
def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _link(url: Any, text: Any = None) -> str:
    """A link to a crawled URL (http and https only), or the text."""
    shown = esc(text if text is not None else url)
    if isinstance(url, str) and url.lower().startswith(("http://", "https://")):
        return f'<a href="{esc(url)}" rel="noreferrer noopener nofollow">{shown}</a>'
    return shown


def _num(value: Any) -> str:
    if value is None:
        return _NONE
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{int(value):,}"


def _pct(value: float | None) -> str:
    return _NONE if value is None else f"{value * 100:.1f}%"


def _share(part: Any, whole: Any) -> float | None:
    return (float(part or 0) / float(whole)) if whole else None


def _bytes(n: Any) -> str:
    if n is None:
        return _NONE
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _seconds(value: Any) -> str:
    if value is None:
        return _NONE
    s = float(value)
    if s < 60:
        return f"{s:.1f} s"
    if s < 3600:
        return f"{int(s // 60)} min {int(s % 60)} s"
    return f"{int(s // 3600)} h {int(s % 3600 // 60)} min"


def _when(stamp: Any) -> str:
    if not stamp:
        return _NONE
    if isinstance(stamp, str):
        try:
            return datetime.fromisoformat(stamp).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return stamp
    return datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d %H:%M")


def _badge(status: str) -> str:
    tone = {"finished": "ok", "running": "live", "failed": "bad", "stopped": "bad", "not responding": "bad",
            "limit": "warn", "paused": "warn"}.get(status, "")  # fmt: skip
    return f'<span class="badge {tone}">{esc(status)}</span>'


def _table(headers: Iterable[str], rows: Iterable[Iterable[str]], empty: str = "none", *, names: bool = False) -> str:
    """A table; the cells are HTML already (escape what goes in). ``names``: the first column holds
    names, kept on one line."""
    first = '<td class="name">' if names else "<td>"
    body = "".join(
        "<tr>" + "".join((first if i == 0 else "<td>") + f"{cell}</td>" for i, cell in enumerate(row)) + "</tr>"
        for row in rows
    )
    if not body:
        return f'<p class="muted">{esc(empty)}</p>'
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _json(value: Any) -> str:
    return esc(json.dumps(value, ensure_ascii=False, default=str))


_STYLE = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e3e3de;--accent:#2457c5;
--ok:#1f7a3d;--warn:#9a6700;--bad:#b42318;--live:#2457c5}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ececea;--muted:#9d9d97;--card:#1f1f1d;
--line:#33332f;--accent:#8fb0ff;--ok:#5cc07f;--warn:#e0b04a;--bad:#ff7a6e;--live:#8fb0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1200px;margin:0 auto;padding:20px 16px 48px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:15px;letter-spacing:.12em;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}
.muted{color:var(--muted)}.top{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin:16px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.tile .label{color:var(--muted);font-size:12px}.tile .value{font-size:22px;font-variant-numeric:tabular-nums}
.tile .note{color:var(--muted);font-size:12px}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);
vertical-align:top}th{font-size:12px;color:var(--muted);font-weight:600}tr:last-child td{border-bottom:0}
td{font-variant-numeric:tabular-nums;overflow-wrap:break-word}td a,td code{overflow-wrap:anywhere}
td:first-child a{overflow-wrap:normal;white-space:nowrap}
.badge{display:inline-block;padding:0 8px;border-radius:10px;border:1px solid currentColor;font-size:12px;
white-space:nowrap}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.live{color:var(--live)}
nav.sections{display:flex;flex-wrap:wrap;gap:4px 14px}td.name{white-space:nowrap}
details{margin-top:8px}summary{cursor:pointer;color:var(--muted)}code{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;
overflow-wrap:anywhere}pre{white-space:pre-wrap;margin:0}
"""


def _page(title: str, body: str, *, refresh: bool = False) -> str:
    meta = '<meta http-equiv="refresh" content="3">' if refresh else ""
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
        f"<title>{esc(title)}</title><style>{_STYLE}</style></head><body><main>{body}</main></body></html>"
    )


def render_index(dashboard: Dashboard, limit: int = 200) -> str:
    runs = dashboard.runs(limit)
    states = {run.id: dashboard.state(run) for run in runs}
    rows = []
    for run in runs:
        s = run.stats or {}
        live = run.metrics() if states[run.id] in ("running", "not responding") else {}
        pages = live.get("pages", s.get("pages"))
        items = live.get("items", s.get("items"))
        failed = live.get("failed", s.get("failed", 0))
        name = f"{run.label} ({run.name})" if run.label else run.name
        success = live.get("success_rate") if live else (1 - float(s.get("failed") or 0) / pages if pages else None)
        took = run.duration if run.duration is not None else live.get("elapsed_seconds")
        rows.append([
            f'<a href="/runs/{esc(run.id)}">{esc(run.id)}</a>' + (' <span class="muted">rec</span>' if run.recorded else ""),
            esc(_when(run.started)), esc(name), _badge(states[run.id]), _num(pages), _num(items), _num(failed),
            _pct(success), esc(_seconds(took)),
        ])  # fmt: skip
    jobs = dashboard.jobs()
    job_rows = []
    for job in jobs:
        last = _NONE
        if job["run"]:
            last = f'<a href="/runs/{esc(job["run"])}">{esc(job["run"])}</a> {_badge(str(job["last_status"]))}'
        elif job["last_status"]:
            last = _badge(str(job["last_status"]))
        next_time = esc(job["next"] or ("off" if not job["enabled"] else _NONE)).replace("T", " ")
        job_rows.append([esc(job["job"]), f'{esc(job["kind"])} <code>{esc(job["target"])}</code>',
                         esc(job["trigger"]), next_time, last])  # fmt: skip
    body = (
        f'<div class="top"><div><h1>WINTERGRAB</h1><div class="muted">{esc(dashboard.workspace.resolve())}</div></div>'
        f'<div class="muted">{len(runs)} run(s) · <a href="/api/runs">JSON</a></div></div>'
    )
    if jobs:
        body += "<h2>Jobs</h2>" + _table(["Job", "What", "When", "Next", "Last run"], job_rows)
    body += "<h2>Runs</h2>" + _table(
        ["Run", "Started", "Name", "Status", "Pages", "Items", "Failed", "Success", "Took"],
        rows,
        empty="no run yet: crawls run with --workspace (or once .wintergrab exists) are kept here",
    )
    return _page("wintergrab", body, refresh=any(s == "running" for s in states.values()))


def _tiles(data: Mapping[str, Any]) -> str:
    stats = data.get("stats") or {}
    m = data.get("metrics") or {}
    running = data["state"] == "running"
    pages = m.get("pages", stats.get("pages", 0))
    requests = m.get("requests", stats.get("requests", 0))
    failed = m.get("failed", stats.get("failed", 0))
    blocked = m.get("blocked", stats.get("blocked", 0))
    elapsed = m.get("elapsed_seconds") or stats.get("elapsed_seconds")
    rates = m.get("rates") or {}
    rps = rates.get("pages_per_second") if running else (pages / elapsed if elapsed else None)
    latency = m.get("latency") or {}
    items = m.get("items", stats.get("items", 0))
    quality = [q.get("score") for q in data.get("quality") or [] if q.get("score") is not None]
    success = m.get("success_rate")
    if success is None and pages:
        success = 1 - failed / pages

    def tile(label: str, value: str, note: str = "") -> str:
        note_html = f'<div class="note">{esc(note)}</div>' if note else ""
        return f'<div class="tile"><div class="label">{esc(label)}</div><div class="value">{esc(value)}</div>{note_html}</div>'

    mean = latency.get("mean")
    tiles = [
        tile("Pages", _num(pages), f"{_num(requests)} requests"),
        tile("Success", _pct(success)),
        tile("Failed", _pct(_share(failed, pages)), f"{_num(failed)} given up"),
        tile("Blocked", _pct(_share(blocked, pages)), f"{_num(blocked)} responses"),
        tile("Pages/sec", _NONE if rps is None else f"{rps:,.1f}", "now" if running else "on average"),
        tile("Latency", _NONE if mean is None else f"{mean * 1000:,.0f} ms",
             f"p50 {(latency.get('p50') or 0) * 1000:,.0f} · p90 {(latency.get('p90') or 0) * 1000:,.0f} ms"
             if mean is not None else ""),
        tile("Records", _num(items), _bytes(data.get("output_bytes")) if data.get("output_bytes") is not None else ""),
        tile("Browser pages", _num(m.get("browser_pages", stats.get("browser_pages", 0)))),
    ]  # fmt: skip
    if quality:
        tiles.append(
            tile("Data quality", _pct(min(quality)), ", ".join(str(q.get("dataset")) for q in data["quality"]))
        )
    if running:
        tiles.append(tile("Queued", _num(m.get("queued")), f"{_num(m.get('in_flight'))} in flight"))
    return '<div class="tiles">' + "".join(tiles) + "</div>"


def render_run(dashboard: Dashboard, run: Run) -> str:
    data = dashboard.run_data(run)
    events = data["events"]
    metrics = data["metrics"]
    state = data["state"]
    name = f"{run.label} ({run.name})" if run.label else run.name
    took = run.duration if run.duration is not None else metrics.get("elapsed_seconds")
    command = (data["recipe"] or {}).get("command")
    header = (
        f'<p><a href="/">← runs</a></p><div class="top"><div><h1>RUN {esc(run.id)}</h1>'
        f"<div>{esc(name)} · {_badge(state)} · {esc(_when(run.started))} · {esc(_seconds(took))}"
        f"{' · recorded' if run.recorded else ''}</div></div>"
        f'<div class="muted"><a href="/api/runs/{esc(run.id)}">JSON</a></div></div>'
    )
    if command:
        header += f'<p class="muted"><code>wintergrab {esc(" ".join(command))}</code></p>'
    if run.error:
        header += f'<p class="bad">{esc(run.error)}</p>'
    if state == "not responding":
        header += (f'<p class="bad">No news from this crawl for {esc(_seconds(_STALE_AFTER))} or more: its process '
                   "has probably stopped without finishing its record.</p>")  # fmt: skip
    nav = (
        '<nav class="sections"><a href="#failures">Failures</a><a href="#domains">Domains</a>'
        '<a href="#extraction">Extraction</a><a href="#changes">Changes</a><a href="#events">Events</a>'
        '<a href="#settings">Settings</a></nav>'
    )
    body = header + _tiles(data) + nav

    # failures
    body += '<h2 id="failures">Failures</h2>'
    diagnoses = [[esc(f.get("signature")), esc(f.get("domain")), _num(f.get("urls")), esc(f.get("cause"))]
                 for f in run.failures]  # fmt: skip
    if diagnoses:
        body += _table(["What", "Domain", "URLs", "Cause"], diagnoses)
    groups = [[esc(g["category"]), esc(g["kind"]), esc(g["status"]), esc(g["error"]), _num(g["count"]),
               "<br>".join(_link(u) for u in g["urls"])] for g in events["failures"]]  # fmt: skip
    body += _table(["Category", "Kind", "Status", "Error", "Requests", "For example"], groups,
                   empty="no request failed") if groups or not diagnoses else ""  # fmt: skip
    if events["blocked"]:
        body += (
            "<p>Blocked responses: " + ", ".join(f"{esc(d)} {_num(n)}" for d, n in events["blocked"].items()) + "</p>"
        )

    # domains
    body += '<h2 id="domains">Domains</h2>'
    domains = [[esc(d.get("domain")), esc(d.get("mode")), _num(d.get("requests")),
                f"{_num(d.get('concurrency'))} / {_num(d.get('max_concurrency'))}",
                esc(_seconds(d.get("delay"))), esc(_seconds(d.get("avg_latency"))), _num(d.get("backoffs"))]
               for d in metrics.get("domains") or []]  # fmt: skip
    body += _table(["Domain", "Mode", "Requests", "Concurrency", "Delay", "Latency", "Backoffs"], domains,
                   empty="no per-domain metrics kept for this run")  # fmt: skip

    # extraction
    body += '<h2 id="extraction">Extraction</h2>'
    extraction = events["extraction"]
    if extraction:
        fields = ", ".join(f"{esc(f)} ({_num(n)})" for f, n in extraction["fields"].items())
        body += (f'<p class="warn">{_num(extraction["pages"])} page(s) gave no complete record: missing {fields}. '
                 f"For example: {', '.join(_link(u) for u in extraction['urls'])}</p>")  # fmt: skip
    for report in data.get("quality") or []:
        facts = ", ".join(f"{esc(k)} {_pct(v)}" for k, v in (report.get("metrics") or {}).items()
                          if isinstance(v, (int, float)) and k != "median_age_seconds")  # fmt: skip
        body += f"<p>Quality of {esc(report.get('dataset'))} ({_num(report.get('records'))} records): {facts}</p>"
    issues = [[esc(q["dataset"]), esc(q["field"]), esc(q["code"]), f'<span class="{"bad" if q["severity"] == "error" else "warn"}">'
               f"{esc(q['message'])}</span>"] for q in events["quality_degraded"]]  # fmt: skip
    if issues:
        body += "<p>Against the last run:</p>" + _table(["Dataset", "Field", "Code", "What"], issues)
    for change in events["schema_changed"]:
        parts = [f"+{f}" for f in change.get("added") or []] + [f"\u2212{f}" for f in change.get("removed") or []]
        parts += [f"{f}: {a} → {b}" for f, (a, b) in (change.get("retyped") or {}).items()]
        body += f'<p class="warn">The fields of {esc(change.get("dataset"))} changed: {esc(", ".join(parts))}</p>'
    if not (extraction or data.get("quality") or issues or events["schema_changed"]):
        body += '<p class="muted">nothing to say: no page without a complete record, no quality measured</p>'

    # changes
    body += '<h2 id="changes">Changes</h2>'
    changes = events["changes"]
    if changes:
        kinds = ", ".join(f"{esc(k)} {_num(v)}" for k, v in (changes.get("kinds") or {}).items())
        body += (f"<p>Since the last run: +{_num(changes.get('added'))} new, \u2212{_num(changes.get('removed'))} gone, "
                 f"~{_num(changes.get('modified'))} modified{' (' + kinds + ')' if kinds else ''}, "
                 f"{_num(changes.get('unchanged'))} unchanged.</p>")  # fmt: skip
        updated = [[_link(u["url"]), esc(", ".join(u.get("kinds") or [])), f"<code>{_json(u.get('details'))}</code>"]
                   for u in events["record_updated"]]  # fmt: skip
        if updated:
            body += _table(["Page", "What changed", "Details"], updated)
        for label, urls in (("New", events["record_created"]), ("Gone", events["record_deleted"])):
            if urls:
                body += (
                    f"<p>{label}: "
                    + ", ".join(_link(u) for u in urls[:_SAMPLES])
                    + ("…" if len(urls) > _SAMPLES else "")
                    + "</p>"
                )
    else:
        body += '<p class="muted">no history kept (crawl with --history FILE to see what changes between runs)</p>'

    # events
    body += '<h2 id="events">Events</h2>'
    counts = [[f'<a href="/runs/{esc(run.id)}/events?kind={quote(kind)}">{esc(kind)}</a>', _num(n)]
              for kind, n in sorted(events["counts"].items(), key=lambda kv: -kv[1])]  # fmt: skip
    body += _table(["Event", "Count"], counts, empty="no events kept")
    if events["truncated"]:
        body += '<p class="muted">(the last 64 MB of events)</p>'

    # settings
    body += '<h2 id="settings">Settings</h2>'
    changed, defaults = _settings_split(data["settings"] or {})
    body += _table(["Setting", "Value"], _setting_rows(changed), empty="every setting at its default", names=True)
    if defaults:
        body += (f"<details><summary>{len(defaults)} more, at their defaults</summary>"
                 + _table(["Setting", "Value"], _setting_rows(defaults), names=True) + "</details>")  # fmt: skip
    files = [f"record: {run.directory}"] + ([f"output: {run.output}"] if run.output else [])
    body += "<p class='muted'>" + " · ".join(esc(f) for f in files) + "</p>"
    return _page(f"{run.id} · wintergrab", body, refresh=state == "running")


def _settings_split(settings: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The settings that differ from a spider's defaults, and the ones that do not."""
    from .spider import Spider

    changed: dict[str, Any] = {}
    defaults: dict[str, Any] = {}
    for name, value in sorted(settings.items()):
        default = getattr(Spider, name, _MISSING)
        if isinstance(default, (set, frozenset)):  # kept as a list, in any order
            same = isinstance(value, list) and sorted(map(repr, default)) == sorted(map(repr, value))
        else:
            same = default is not _MISSING and json.loads(json.dumps(redact(default, name), default=repr)) == value
        (defaults if same else changed)[name] = value
    return changed, defaults


def _setting_rows(settings: Mapping[str, Any]) -> list[list[str]]:
    return [[esc(name), f"<code>{_json(value)}</code>"] for name, value in settings.items()]


def render_events(dashboard: Dashboard, run: Run, kind: str, limit: int = 500) -> str:
    found: list[dict[str, Any]] = []
    for event in run.events(kind):
        found.append(event)
        if len(found) > limit:
            found.pop(0)  # the last ones
    rows = [[esc(e.get("time")), "<code>" + _json({k: v for k, v in e.items() if k not in ("event", "time", "origin")}) + "</code>"]
            for e in found]  # fmt: skip
    body = (f'<p><a href="/runs/{esc(run.id)}">← {esc(run.id)}</a></p><h1>{esc(kind)}</h1>'
            f'<p class="muted">the last {len(found)} of this kind</p>' + _table(["Time", "Data"], rows))  # fmt: skip
    return _page(f"{kind} · {run.id}", body)


# ---------------------------------------------------------------------------------------------- #
# the server
# ---------------------------------------------------------------------------------------------- #
_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "wintergrab-dashboard"

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s " + format, self.address_string(), *args)

    def do_HEAD(self) -> None:
        self.do_GET(head=True)

    def do_GET(self, head: bool = False) -> None:
        if not self._host_allowed():
            self._send(HTTPStatus.FORBIDDEN, "text/plain", b"not for this host\n", head)
            return
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        segments = [s for s in parts.path.split("/") if s]
        dashboard = self.server.dashboard
        try:
            if not segments:
                limit = _int(query.get("limit"), 200)
                self._html(render_index(dashboard, limit), head)
            elif segments[0] == "runs" and len(segments) == 2:
                self._html(render_run(dashboard, dashboard.run(segments[1])), head)
            elif segments[0] == "runs" and len(segments) == 3 and segments[2] == "events":
                kind = (query.get("kind") or [""])[0]
                self._html(
                    render_events(dashboard, dashboard.run(segments[1]), kind, _int(query.get("limit"), 500)), head
                )
            elif segments[:2] == ["api", "runs"] and len(segments) == 2:
                self._json([dashboard.summary(run) for run in dashboard.runs(_int(query.get("limit"), 200))], head)
            elif segments[:2] == ["api", "runs"] and len(segments) == 3:
                self._json(dashboard.run_data(dashboard.run(segments[2])), head)
            elif segments == ["api", "jobs"]:
                self._json(dashboard.jobs(), head)
            else:
                self._send(HTTPStatus.NOT_FOUND, "text/plain", b"not found\n", head)
        except ConfigurationError as exc:  # no such run
            self._send(HTTPStatus.NOT_FOUND, "text/plain", f"{exc}\n".encode(), head)
        except Exception:
            log.exception("dashboard: %s failed", self.path)
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, "text/plain", b"something went wrong (see the log)\n", head)

    def do_POST(self) -> None:
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, "text/plain", b"the dashboard changes nothing\n", False)

    do_PUT = do_DELETE = do_PATCH = do_POST

    def _host_allowed(self) -> bool:
        if not self.server.loopback:
            return True  # listening beyond this machine was asked for
        host = self.headers.get("Host", "")
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host[1:].split("]", 1)[0]
        return name.lower() in _LOOPBACK

    def _html(self, text: str, head: bool) -> None:
        self._send(HTTPStatus.OK, "text/html; charset=utf-8", text.encode("utf-8"), head)

    def _json(self, value: Any, head: bool) -> None:
        body = json.dumps(value, ensure_ascii=False, indent=1, default=str).encode("utf-8")
        self._send(HTTPStatus.OK, "application/json; charset=utf-8", body, head)

    def _send(self, status: HTTPStatus, kind: str, body: bytes, head: bool) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for name, value in _HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)


def _int(values: list[str] | None, default: int) -> int:
    try:
        return max(1, min(int((values or [""])[0]), 10_000))
    except ValueError:
        return default


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], dashboard: Dashboard) -> None:
        super().__init__(address, _Handler)
        self.dashboard = dashboard
        self.loopback = address[0] in _LOOPBACK or address[0].startswith("127.")

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        name = host.decode() if isinstance(host, bytes) else str(host)
        shown = f"[{name}]" if ":" in name else name
        return f"http://{shown}:{port}/"


def serve(
    workspace: str | Path = DEFAULT_WORKSPACE,
    *,
    project: Any = None,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
) -> _Server:
    """A dashboard server for ``workspace`` (not started: call ``serve_forever()``, or run it in a thread)."""
    server = _Server((host, port), Dashboard(workspace, project))
    if not server.loopback:
        log.warning("the dashboard listens on %s: anyone who can reach it sees what your crawls collected", host)
    return server
