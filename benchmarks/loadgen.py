"""Minimal keep-alive HTTP/1.1 load generator to measure fastserver capacity.

Raw ``asyncio.Protocol`` clients (no HTTP library), spread over several
processes so the load generator is not the bottleneck. Requests follow the
crawl's mix: one ``/page/<i>`` for every four ``/item/<j>``.

    python benchmarks/loadgen.py http://127.0.0.1:PORT --pages 2000 --items 8000 \
        --processes 3 --connections 64 --duration 5

Prints one JSON line: total requests/s, MB/s and errors.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import time
from urllib.parse import urlsplit


class _Client(asyncio.Protocol):
    def __init__(self, host: bytes, paths: list[bytes], offset: int, state: dict[str, float]) -> None:
        self.host = host
        self.paths = paths
        self.pos = offset
        self.state = state
        self.buf = b""
        self.need = -1
        self.transport: asyncio.Transport | None = None
        self.closed = asyncio.get_running_loop().create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        self._send()

    def _send(self) -> None:
        path = self.paths[self.pos % len(self.paths)]
        self.pos += 1
        assert self.transport is not None
        self.transport.write(b"GET " + path + b" HTTP/1.1\r\nHost: " + self.host + b"\r\nAccept: */*\r\n\r\n")

    def data_received(self, data: bytes) -> None:
        self.buf += data
        while True:
            if self.need < 0:
                end = self.buf.find(b"\r\n\r\n")
                if end < 0:
                    return
                head = self.buf[:end].lower()
                cl = head.find(b"content-length:")
                stop = head.find(b"\r\n", cl)
                length = int(head[cl + 15 : stop if stop >= 0 else len(head)])
                if not head.startswith(b"http/1.1 200"):
                    self.state["errors"] += 1
                self.buf = self.buf[end + 4 :]
                self.need = length
            if len(self.buf) < self.need:
                return
            self.buf = self.buf[self.need :]
            self.state["bytes"] += self.need
            self.need = -1
            if self.state["measuring"]:
                self.state["done"] += 1
            if self.state["stop"]:
                assert self.transport is not None
                self.transport.close()
                return
            self._send()

    def connection_lost(self, exc: BaseException | None) -> None:
        if not self.closed.done():
            self.closed.set_result(None)


def _run_process(
    url: str, paths: list[bytes], connections: int, warmup: float, duration: float, seed: int, out
) -> None:  # type: ignore[no-untyped-def]
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80

    async def main() -> dict[str, float]:
        loop = asyncio.get_running_loop()
        state = {"done": 0, "bytes": 0, "errors": 0, "measuring": False, "stop": False}
        clients = []
        for c in range(connections):
            _, proto = await loop.create_connection(
                lambda c=c: _Client(f"{host}:{port}".encode(), paths, seed * 7919 + c * 131, state), host, port
            )
            clients.append(proto)
        await asyncio.sleep(warmup)
        state["measuring"] = True
        state["bytes"] = 0
        start = time.perf_counter()
        await asyncio.sleep(duration)
        state["measuring"] = False
        elapsed = time.perf_counter() - start
        done, nbytes = state["done"], state["bytes"]
        state["stop"] = True
        await asyncio.wait([p.closed for p in clients], timeout=5)
        return {"done": done, "bytes": nbytes, "errors": state["errors"], "elapsed": elapsed}

    out.put(asyncio.run(main()))


def run_load(
    url: str,
    *,
    pages: int,
    items: int,
    processes: int = 3,
    connections: int = 64,
    warmup: float = 1.0,
    duration: float = 5.0,
) -> dict[str, float]:
    paths: list[bytes] = []
    for k in range(max(pages, items // 4)):
        paths.append(f"/page/{k % pages}".encode())
        paths.extend(f"/item/{(4 * k + d) % items}".encode() for d in range(4))
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    per_proc = max(1, connections // processes)
    procs = [
        ctx.Process(target=_run_process, args=(url, paths, per_proc, warmup, duration, n, queue))
        for n in range(processes)
    ]
    for p in procs:
        p.start()
    results = [queue.get(timeout=warmup + duration + 60) for _ in procs]
    for p in procs:
        p.join()
    elapsed = max(r["elapsed"] for r in results)
    done = sum(r["done"] for r in results)
    return {
        "processes": processes,
        "connections": per_proc * processes,
        "requests": done,
        "seconds": round(elapsed, 3),
        "req_per_s": round(done / elapsed, 1),
        "mb_per_s": round(sum(r["bytes"] for r in results) / elapsed / 1e6, 1),
        "errors": sum(r["errors"] for r in results),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure how many requests/s the benchmark server can serve.")
    parser.add_argument("url")
    parser.add_argument("--pages", type=int, default=2000)
    parser.add_argument("--items", type=int, default=8000)
    parser.add_argument("--processes", type=int, default=3)
    parser.add_argument("--connections", type=int, default=64, help="total connections (split over processes)")
    parser.add_argument("--warmup", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=5.0)
    args = parser.parse_args()
    result = run_load(
        args.url,
        pages=args.pages,
        items=args.items,
        processes=args.processes,
        connections=args.connections,
        warmup=args.warmup,
        duration=args.duration,
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
