"""Run the crawler benchmark matrix and print a markdown table.

For every latency scenario a fresh ``fastserver.py`` is started; then every
tool crawls the whole site ``--repetitions`` times at every concurrency, each
run in its own subprocess. Results (all runs + medians) go to ``results.json``.

    python benchmarks/run.py                                   # wintergrab only
    python benchmarks/run.py --tools wintergrab,scrapy,crawlee \
        --competitor-python /path/to/other-venv/bin/python     # vs. Scrapy/Crawlee

See benchmarks/README.md for the methodology.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# name -> (script, extra args, python family)
TOOLS: dict[str, tuple[str, list[str], str]] = {
    "wintergrab": ("bench_wintergrab.py", ["--impersonate", "chrome"], "wintergrab"),
    "wintergrab-noimp": ("bench_wintergrab.py", ["--impersonate", "none"], "wintergrab"),
    "scrapy": ("bench_scrapy.py", ["--reactor", "asyncio"], "scrapy"),
    "scrapy-epoll": ("bench_scrapy.py", ["--reactor", "epoll"], "scrapy"),
    "crawlee": ("bench_crawlee.py", [], "crawlee"),
    # event-loop variants (side experiment): wintergrab uses uvloop by default when installed
    "wintergrab-asyncio": ("bench_wintergrab.py", ["--impersonate", "chrome", "--loop", "asyncio"], "wintergrab"),
    "scrapy-uvloop": ("bench_scrapy.py", ["--reactor", "asyncio", "--loop", "uvloop"], "scrapy"),
    "crawlee-uvloop": ("bench_crawlee.py", ["--loop", "uvloop"], "crawlee"),
}

_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _int_list(text: str) -> list[int]:
    return [int(x) for x in text.split(",") if x.strip()]


def _float_list(text: str) -> list[float]:
    return [float(x) for x in text.split(",") if x.strip()]


def clean_env() -> dict[str, str]:
    """The environment for crawler subprocesses: no proxy variables.

    Everything talks to 127.0.0.1, and some clients (Scrapy's HttpProxyMiddleware)
    re-scan the whole environment for ``no_proxy`` on every request when a proxy
    variable is present, which would penalise them for the host's settings.
    """
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("PYTHONPROFILEIMPORTTIME", None)
    return env


def http_get_json(url: str) -> dict[str, Any]:
    with _NO_PROXY_OPENER.open(url, timeout=10) as resp:
        return json.loads(resp.read())


class Server:
    def __init__(
        self, python: str, pages: int, items: int, latency_ms: float, workers: int, jump_links: bool = True
    ) -> None:
        self.args = [
            python,
            str(HERE / "fastserver.py"),
            "--pages", str(pages),
            "--items", str(items),
            "--latency", str(latency_ms),
            "--workers", str(workers),
            "--port", "0",
        ]  # fmt: skip
        if not jump_links:
            self.args.append("--no-jump-links")
        self.proc: subprocess.Popen[str] | None = None
        self.url = ""

    def __enter__(self) -> Server:
        self.proc = subprocess.Popen(
            self.args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=clean_env()
        )
        assert self.proc.stdout is not None and self.proc.stderr is not None
        self.url = self.proc.stdout.readline().strip()
        if not self.url.startswith("http"):
            raise RuntimeError(f"server did not start: {self.url!r} {self.proc.stderr.read()}")
        self.info = self.proc.stderr.readline().strip()
        for _ in range(100):
            try:
                http_get_json(self.url + "/__reset")
                break
            except OSError:
                time.sleep(0.1)
        return self

    def __exit__(self, *exc: object) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def reset(self) -> None:
        http_get_json(self.url + "/__reset")

    def stats(self) -> dict[str, int]:
        return http_get_json(self.url + "/__stats")  # type: ignore[return-value]


def run_one(python: str, tool: str, url: str, concurrency: int, timeout: float) -> dict[str, Any]:
    script, extra, _ = TOOLS[tool]
    cmd = [python, str(HERE / script), "--url", url, "--concurrency", str(concurrency), *extra]
    started = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=clean_env(), cwd=str(HERE))
    except subprocess.TimeoutExpired:
        return {"tool": tool, "error": f"timed out after {timeout:.0f}s"}
    process_wall = time.perf_counter() - started
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("RESULT "):
            result = json.loads(line[7:])
            result["tool"] = tool
            result["process_wall_s"] = round(process_wall, 3)
            return result
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
    missing = any("ModuleNotFoundError" in line or "ImportError" in line for line in tail)
    return {"tool": tool, "error": " | ".join(tail) or f"exit code {proc.returncode}", "missing": missing}


def summarize(runs: list[dict[str, Any]], expected: dict[str, int]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, float, int], list[dict[str, Any]]] = {}
    for run in runs:
        groups.setdefault((run["tool"], run["latency_ms"], run["concurrency"]), []).append(run)
    rows = []
    for (tool, latency, conc), group in groups.items():
        good = [r for r in group if "error" not in r]
        if not good:
            rows.append({"tool": tool, "latency_ms": latency, "concurrency": conc, "error": group[0]["error"]})
            continue
        rates = [r["pages_per_s"] for r in good]
        rows.append(
            {
                "tool": tool,
                "latency_ms": latency,
                "concurrency": conc,
                "runs": len(group),
                "correct_runs": sum(1 for r in good if r["correct"]),
                "pages_per_s_median": round(statistics.median(rates), 1),
                "pages_per_s_min": min(rates),
                "pages_per_s_max": max(rates),
                "wall_s_median": round(statistics.median(r["wall_s"] for r in good), 3),
                "cpu_s_median": round(statistics.median(r["cpu_s"] for r in good), 3),
                "cpu_ms_per_page": round(
                    1000 * statistics.median(r["cpu_s"] / max(1, r["responses"]) for r in good), 3
                ),
                "peak_rss_mb_median": round(statistics.median(r["peak_rss_mb"] for r in good), 1),
                "responses": sorted({r["responses"] for r in good}),
                "items": sorted({r["items"] for r in good}),
                "server_requests": sorted({r.get("server_requests", -1) for r in good}),
                "expected_responses": expected["responses"],
                "versions": good[0].get("versions", {}),
            }
        )
    return rows


def markdown_table(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| Tool | Latency | Concurrency | Pages/s (median) | min-max | Wall s | CPU s | CPU ms/page | Peak RSS MB | Correct |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in sorted(rows, key=lambda r: (r["latency_ms"], r["concurrency"], -r.get("pages_per_s_median", 0))):
        latency = f"{r['latency_ms']:g} ms"
        if "error" in r:
            lines.append(f"| {r['tool']} | {latency} | {r['concurrency']} | error: {r['error'][:80]} | | | | | | |")
            continue
        lines.append(
            f"| {r['tool']} | {latency} | {r['concurrency']} | **{r['pages_per_s_median']:.0f}** "
            f"| {r['pages_per_s_min']:.0f}-{r['pages_per_s_max']:.0f} | {r['wall_s_median']:.2f} "
            f"| {r['cpu_s_median']:.2f} | {r['cpu_ms_per_page']:.2f} | {r['peak_rss_mb_median']:.0f} "
            f"| {r['correct_runs']}/{r['runs']} |"
        )
    return "\n".join(lines)


def git_state() -> dict[str, Any]:
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    diff = git("diff", "HEAD", "--", "src") + git("ls-files", "--others", "--exclude-standard", "--", "src")
    return {
        "commit": git("rev-parse", "--short", "HEAD"),
        "src_dirty": bool(diff),
        # identifies the exact uncommitted src/ state that was measured
        "src_diff_sha1": hashlib.sha1(diff.encode()).hexdigest()[:12] if diff else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pages", type=int, default=2000)
    parser.add_argument("--items", type=int, default=8000)
    parser.add_argument("--concurrency", type=_int_list, default=[16, 64, 256], help="comma-separated, e.g. 16,64,256")
    parser.add_argument("--latency", type=_float_list, default=[0.0, 20.0], help="server latency scenarios in ms")
    parser.add_argument("--repetitions", "-r", type=int, default=3)
    parser.add_argument("--tools", default="wintergrab,wintergrab-noimp", help=f"comma-separated: {', '.join(TOOLS)}")
    parser.add_argument(
        "--python",
        action="append",
        default=[],
        metavar="FAMILY=PATH",
        help="interpreter per tool family (wintergrab, scrapy, crawlee), e.g. scrapy=/venv/bin/python",
    )
    parser.add_argument("--competitor-python", help="interpreter for scrapy and crawlee (shortcut for --python)")
    parser.add_argument("--server-workers", type=int, default=1)
    parser.add_argument("--no-jump-links", action="store_true", help="deep chain site graph (see fastserver.py)")
    parser.add_argument("--server-check", action="store_true", help="measure server capacity with loadgen.py first")
    parser.add_argument("--timeout", type=float, default=900, help="seconds per crawl run")
    parser.add_argument("--out", default=str(HERE / "results.json"))
    args = parser.parse_args()

    tools = [t.strip() for t in args.tools.split(",") if t.strip()]
    unknown = [t for t in tools if t not in TOOLS]
    if unknown:
        parser.error(f"unknown tools: {', '.join(unknown)} (choose from {', '.join(TOOLS)})")
    pythons = {"wintergrab": sys.executable, "scrapy": sys.executable, "crawlee": sys.executable}
    if args.competitor_python:
        pythons["scrapy"] = pythons["crawlee"] = args.competitor_python
    for spec in args.python:
        family, _, path = spec.partition("=")
        if family not in pythons or not path:
            parser.error(f"bad --python {spec!r}; use FAMILY=PATH with FAMILY in {', '.join(pythons)}")
        pythons[family] = path

    expected = {"listing_pages": args.pages, "items": min(args.items, args.pages * 20)}
    expected["responses"] = expected["listing_pages"] + expected["items"]
    meta: dict[str, Any] = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cpu_count": os.cpu_count(),
        "platform": platform.platform(),
        "processor": _cpu_model(),
        "runner_python": platform.python_version(),
        "pythons": pythons,
        "pages": args.pages,
        "items": args.items,
        "expected": expected,
        "repetitions": args.repetitions,
        "server_workers": args.server_workers,
        "jump_links": not args.no_jump_links,
        "wintergrab_git": git_state(),
        "server_capacity": {},
    }
    runs: list[dict[str, Any]] = []
    skipped: set[str] = set()
    total = len(args.latency) * len(args.concurrency) * args.repetitions * len(tools)
    done = 0
    for latency in args.latency:
        server_cm = Server(sys.executable, args.pages, args.items, latency, args.server_workers, not args.no_jump_links)
        with server_cm as server:
            print(f"# server {server.url} {server.info}", file=sys.stderr, flush=True)
            if args.server_check:
                meta["server_capacity"][f"{latency:g}ms"] = _check_server(server, args, max(args.concurrency))
            for conc in args.concurrency:
                for rep in range(args.repetitions):
                    order = tools[rep % len(tools) :] + tools[: rep % len(tools)]  # rotate to spread drift
                    for tool in order:
                        done += 1
                        if tool in skipped:
                            continue
                        server.reset()
                        result = run_one(pythons[TOOLS[tool][2]], tool, server.url, conc, args.timeout)
                        result.update(latency_ms=latency, concurrency=conc, repetition=rep)
                        if "error" in result:
                            print(f"[{done}/{total}] {tool} failed: {result['error']}", file=sys.stderr, flush=True)
                            if result.get("missing"):
                                print(f"  -> skipping {tool} (not installed for that interpreter)", file=sys.stderr)
                                skipped.add(tool)
                            runs.append(result)
                            continue
                        srv = server.stats()
                        result["server_requests"] = srv["requests"]
                        result["server_not_found"] = srv["not_found"]
                        result["correct"] = (
                            result["listing_pages"] == expected["listing_pages"]
                            and result["items"] == expected["items"]
                            and result["bad_items"] == 0
                            and srv["requests"] == expected["responses"]
                            and srv["not_found"] == 0
                        )
                        runs.append(result)
                        print(
                            f"[{done}/{total}] {tool:17s} latency={latency:g}ms conc={conc:<3d} "
                            f"{result['pages_per_s']:7.1f} pages/s  wall={result['wall_s']:6.2f}s "
                            f"cpu={result['cpu_s']:6.2f}s rss={result['peak_rss_mb']:5.0f}MB "
                            f"pages={result['listing_pages']} items={result['items']} server={srv['requests']}"
                            f"{'' if result['correct'] else '  <-- MISMATCH'}",
                            file=sys.stderr,
                            flush=True,
                        )
    rows = summarize(runs, expected)
    table = markdown_table(rows)
    print(table)
    Path(args.out).write_text(json.dumps({"meta": meta, "summary": rows, "runs": runs}, indent=2) + "\n")
    print(f"\nwrote {args.out}", file=sys.stderr)


def _check_server(server: Server, args: argparse.Namespace, connections: int) -> dict[str, Any]:
    from loadgen import run_load

    result = run_load(
        server.url,
        pages=args.pages,
        items=args.items,
        processes=min(3, os.cpu_count() or 1),
        connections=connections,
        warmup=1.0,
        duration=4.0,
    )
    print(f"# server capacity: {json.dumps(result)}", file=sys.stderr, flush=True)
    return result


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor()


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    main()
