"""Profile the wintergrab benchmark crawl and group the hotspots by layer.

    python benchmarks/profile_wintergrab.py --concurrency 64 --latency 0 --out /tmp/wintergrab.prof

Starts ``fastserver.py``, runs ``bench_wintergrab``'s spider in-process under
cProfile, then prints where the time went: wintergrab's own code vs
curl_cffi vs lxml/cssselect vs asyncio vs the rest of the stdlib.

cProfile inflates code that makes many small Python calls, so the same
report can also be built from a sampling profiler's collapsed stacks::

    py-spy record --native -r 250 -f raw -o stacks.txt -- python benchmarks/bench_wintergrab.py --url ... --concurrency 64
    python benchmarks/profile_wintergrab.py --collapsed stacks.txt
"""

from __future__ import annotations

import argparse
import cProfile
import pstats
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def categorize(filename: str, name: str) -> str:
    """Layer of a cProfile entry (C builtins are recognised by their names).

    lxml is Cython without profiling hooks: its parse/XPath time shows up as the
    *self* time of the wintergrab function that called it (``parse_document``,
    ``_xpath_raw``). Use ``--collapsed`` with ``py-spy --native`` for the real split.
    """
    where = f"{filename} {name}"
    path = filename.replace("\\", "/")
    if "/src/wintergrab/" in path or "/site-packages/wintergrab/" in path:
        return "wintergrab"
    if "curl_cffi" in where or "_cffi_backend" in where or "libcurl" in where:
        return "curl_cffi"
    if "lxml" in where or "cssselect" in where or "libxml" in where:
        return "lxml/cssselect"
    if "/asyncio/" in filename or "selectors.py" in filename or "epoll" in name or "_asyncio" in where:
        return "asyncio"
    if filename.endswith("urllib/parse.py"):
        return "urllib.parse"
    if filename == "~" or "/lib/python3" in filename or filename.startswith("<frozen"):
        return "other stdlib"
    return "other"


def _short(filename: str) -> str:
    for marker in ("/src/wintergrab/", "/site-packages/", "/lib/python3.11/", "/lib/python3.12/", "/lib/python3.13/"):
        if marker in filename:
            return filename.split(marker, 1)[1]
    return filename


def report_pstats(stats: pstats.Stats, top: int) -> None:
    raw = stats.stats  # type: ignore[attr-defined]
    total = sum(entry[2] for entry in raw.values())
    by_cat: dict[str, float] = defaultdict(float)
    for (filename, _line, name), (_cc, _nc, tt, _ct, _callers) in raw.items():
        by_cat[categorize(filename, name)] += tt
    print(f"\n## Self time by layer (cProfile, total {total:.2f}s)\n")
    print("| Layer | Self time s | Share |\n|---|---:|---:|")
    for cat in sorted(by_cat, key=by_cat.get, reverse=True):  # type: ignore[arg-type]
        print(f"| {cat} | {by_cat[cat]:.2f} | {100 * by_cat[cat] / total:.1f}% |")

    rows = [
        (tt, ct, nc, categorize(f, n), f"{_short(f)}:{line}({n})" if f != "~" else n)
        for (f, line, n), (_cc, nc, tt, ct, _callers) in raw.items()
    ]
    print(f"\n## Top {top} functions by self time\n")
    print("| Self s | Cum s | Calls | Layer | Function |\n|---:|---:|---:|---|---|")
    for tt, ct, nc, cat, label in sorted(rows, reverse=True)[:top]:
        print(f"| {tt:.3f} | {ct:.3f} | {nc} | {cat} | `{label}` |")

    print(f"\n## Top {top} wintergrab functions by cumulative time\n")
    print("| Cum s | Self s | Calls | Function |\n|---:|---:|---:|---|")
    own = [r for r in rows if r[3] == "wintergrab"]
    for tt, ct, nc, _cat, label in sorted(own, key=lambda r: r[1], reverse=True)[:top]:
        print(f"| {ct:.3f} | {tt:.3f} | {nc} | `{label}` |")


# Native source files seen in ``py-spy --native`` stacks, by library.
_CURL_SOURCES = frozenset(
    str.split(
        "multi.c multi_ev.c http.c http1.c http2.c http_chunks.c http_digest.c transfer.c cw-out.c cw-pause.c url.c "
        "urlapi.c sendf.c sendf.h mprintf.c cf-socket.c cf-setup.c cf-dns.c cf-ip-happy.c cfilters.c connect.c "
        "conncache.c cshutdn.c dynbuf.c bufq.c request.c setopt.c easy.c slist.c llist.c hash.c splay.c strparse.c "
        "strequal.c strcase.c getenv.c getinfo.c progress.c peer.c timeval.c proxy.c digest.c headers.c sigpipe.h "
        "select.c cookie.c content_encoding.c hostip.c vtls.c curl_trc.c curl_cffi._wrapper.c"
    )
)
_LIBUV_SOURCES = frozenset(
    str.split("loop.c core.c loop-watcher.c queue.h linux.c poll.c timer.c heap-inl.h stream.c tcp.c")
)


def categorize_frame(frame: str) -> str | None:
    """Layer of one ``name (file:line)`` / ``name (lib.so)`` frame, or ``None`` if it says nothing
    (libc, libpython...), in which case the caller's frame decides."""
    _name, _, loc = frame.rpartition(" (")
    loc = loc.rstrip(")")
    source = loc.rsplit(":", 1)[0] if ".py:" in loc or ".c:" in loc or ".h:" in loc else loc
    if "lxml" in source or "libxml" in source or "cssselect" in source:
        return "lxml/cssselect"
    if "curl_cffi" in source or "_cffi_backend" in source or "libcurl" in source or source in _CURL_SOURCES:
        return "curl_cffi"
    if "uvloop" in source or "_asyncio" in source or source.startswith("asyncio/") or source in _LIBUV_SOURCES:
        return "asyncio"
    if source.endswith(".py") or source.startswith("<frozen"):
        if source.startswith("wintergrab/") or "/src/wintergrab/" in source:
            return "wintergrab"
        if source.endswith("urllib/parse.py"):
            return "urllib.parse"
        if source.startswith(("bench_", "_common", "benchmarks/")):
            return "benchmark script"
        return "other stdlib"
    if "_hashlib" in source or "libcrypto" in source:
        return "other stdlib"
    return None


def report_collapsed(path: Path, top: int) -> None:
    """Self samples by layer from collapsed stacks (``py-spy record [--native] -f raw``).

    A sample belongs to the innermost frame that identifies a layer, so time in
    malloc or the interpreter is charged to the Python/C code that caused it.
    """
    by_cat: dict[str, int] = defaultdict(int)
    by_leaf: dict[str, int] = defaultdict(int)
    inclusive: dict[str, int] = defaultdict(int)  # per wintergrab function, once per stack
    total = 0
    for line in path.read_text().splitlines():
        stack, _, count_text = line.rpartition(" ")
        if not stack or not count_text.isdigit():
            continue
        count = int(count_text)
        frames = stack.split(";")
        category = "unattributed"
        for frame in reversed(frames):
            found = categorize_frame(frame)
            if found is not None:
                category = found
                by_leaf[f"[{found}] {frame}"] += count
                break
        by_cat[category] += count
        seen: set[str] = set()
        for frame in frames:
            if categorize_frame(frame) == "wintergrab":
                key = frame.rsplit(":", 1)[0] + ")"  # merge lines of one function
                if key not in seen:
                    seen.add(key)
                    inclusive[key] += count
        total += count
    print(f"\n## Self samples by layer ({path.name}, {total} samples)\n")
    print("| Layer | Samples | Share |\n|---|---:|---:|")
    for cat in sorted(by_cat, key=by_cat.get, reverse=True):  # type: ignore[arg-type]
        print(f"| {cat} | {by_cat[cat]} | {100 * by_cat[cat] / total:.1f}% |")
    print(f"\n## Top {top} frames by self samples\n")
    for leaf, count in sorted(by_leaf.items(), key=lambda kv: kv[1], reverse=True)[:top]:
        print(f"- {100 * count / total:5.1f}%  `{leaf}`")
    print(f"\n## Top {top} wintergrab functions (inclusive)\n")
    for fn, count in sorted(inclusive.items(), key=lambda kv: kv[1], reverse=True)[:top]:
        print(f"- {100 * count / total:5.1f}%  `{fn}`")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pages", type=int, default=2000)
    parser.add_argument("--items", type=int, default=8000)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--latency", type=float, default=0.0, help="server latency in ms")
    parser.add_argument("--impersonate", default="chrome")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "wintergrab-bench.prof"))
    parser.add_argument("--collapsed", type=Path, help="only analyse a collapsed-stacks file (py-spy -f raw)")
    parser.add_argument("--load", type=Path, help="only analyse an existing .prof file")
    args = parser.parse_args()

    if args.collapsed:
        report_collapsed(args.collapsed, args.top)
        return
    if args.load:
        report_pstats(pstats.Stats(str(args.load)), args.top)
        return

    from bench_wintergrab import make_spider
    from run import Server

    impersonate = None if args.impersonate.lower() == "none" else args.impersonate
    with Server(sys.executable, args.pages, args.items, args.latency, workers=1) as server:
        spider = make_spider(server.url, args.concurrency, impersonate)
        profiler = cProfile.Profile()
        profiler.enable()
        result = spider.run()
        profiler.disable()
    profiler.dump_stats(args.out)
    print(
        f"# crawl: {result.status}, {spider.listing_pages} listing pages, {spider.item_count} items, "
        f"stats pages={result.stats.get('pages')} elapsed={result.stats.get('elapsed_seconds')}s; profile: {args.out}"
    )
    report_pstats(pstats.Stats(args.out), args.top)


if __name__ == "__main__":
    main()
