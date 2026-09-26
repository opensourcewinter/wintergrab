"""How many requests a shared frontier (PostgreSQL) hands out per second, to one process and to several.

    .venv/bin/python benchmarks/bench_shared_frontier.py postgresql://user@127.0.0.1:5432/db [--requests 20000]

Queues ``--requests`` requests over ``--sites`` sites with no delay, then has 1, 2 and 4 processes take
(pop), and acknowledge, all of them at once, each as fast as it can, and prints what that came to per
second. The crawl it uses is emptied afterwards. It measures the frontier alone: a crawl's own pace
(its sites' delays) and network come on top.
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import time
import uuid

from wintergrab import Request
from wintergrab.spider.shared import SharedScheduler
from wintergrab.spider.throttle import AutoThrottle


def _work(crawl: str, start: object, out: object) -> None:
    frontier = SharedScheduler(crawl, None)
    throttle = AutoThrottle(enabled=False, base_delay=0.0, max_concurrency=10**6)
    start.wait()  # type: ignore[attr-defined]
    began, taken = time.perf_counter(), 0
    while True:
        request, _ = frontier.pop_ready(throttle, time.monotonic())
        if request is None:
            if not len(frontier):
                break
            continue
        frontier.ack(request)
        taken += 1
        if taken % 200 == 0:
            frontier.commit()  # (as a crawl does about every second)
    frontier.commit()
    out.put((taken, time.perf_counter() - began))  # type: ignore[attr-defined]
    frontier.close()


def run(url: str, processes: int, requests: int, sites: int) -> tuple[int, float]:
    crawl = f"{url}{'&' if '?' in url else '?'}crawl=bench_{uuid.uuid4().hex[:8]}"
    seed = SharedScheduler(crawl, None)
    for i in range(requests):
        seed.push(Request(f"https://site{i % sites}.example/p/{i}", priority=i % 7))
    seed.commit()
    start, out = multiprocessing.Event(), multiprocessing.Queue()
    workers = [multiprocessing.Process(target=_work, args=(crawl, start, out)) for _ in range(processes)]
    for worker in workers:
        worker.start()
    time.sleep(1.0)  # (all connected)
    began = time.perf_counter()
    start.set()
    for worker in workers:
        worker.join()
    wall = time.perf_counter() - began
    taken = sum(out.get()[0] for _ in workers)
    seed.close(finished=True)  # (nothing left: the crawl's seen URLs and sites are emptied)
    return taken, wall


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("url", help="a PostgreSQL database (postgresql://user@host:port/db)")
    parser.add_argument("--requests", type=int, default=20_000)
    parser.add_argument("--sites", type=int, default=50)
    args = parser.parse_args()
    print(f"{os.cpu_count()} CPUs; {args.requests:,} requests over {args.sites} sites")
    for processes in (1, 2, 4):
        taken, wall = run(args.url, processes, args.requests, args.sites)
        print(f"{processes} process(es): {taken:,} taken and acknowledged in {wall:.1f} s = {taken / wall:,.0f}/s")


if __name__ == "__main__":
    main()
