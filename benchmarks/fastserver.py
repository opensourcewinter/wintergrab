"""A very fast local HTTP/1.1 keep-alive server for crawler benchmarks.

It serves a synthetic shop so that a crawler can discover everything from
``/page/0``:

* ``/page/<i>`` (``0 <= i < pages``): ~15-30 KB listing pages with a title,
  nav/footer boilerplate, 20 product cards (name, price, link to
  ``/item/<j>``) and a pager linking to the next 5 pages (``i+1..i+5``)
  plus 5 "jump" links (``5i+1..5i+5``), like paginations that skip ahead.
  The jump links keep the discovery graph ~log5(pages) deep; with only the
  next-5 links, page ``i`` is ``i/5`` hops from ``/page/0`` and a crawl
  under latency measures that critical path (and queue order) rather than
  throughput. ``--no-jump-links`` restores the plain chain.
* ``/item/<j>`` (``0 <= j < items``): ~10 KB detail pages with an ``<h1>``
  name and a ``.price``.

Every response is pre-rendered into memory (status line, headers and body
as one ``bytes`` object), so serving a request is a dict lookup and one
``transport.write``. Several worker processes can share the port through
``SO_REUSEPORT`` (``--workers``). ``--latency`` delays every response to
simulate a real network round trip.

Helper endpoints (not counted as site traffic):

* ``/__stats``  -> JSON ``{"requests": n, "not_found": n}`` summed over workers
* ``/__reset``  -> resets the counters

Usage::

    python benchmarks/fastserver.py --pages 2000 --items 8000 --latency 0 --port 0

The first line printed on stdout is the base URL (e.g. ``http://127.0.0.1:43127``).
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import ctypes
import json
import multiprocessing
import os
import random
import signal
import socket
import sys
from multiprocessing.sharedctypes import RawArray
from typing import Any

CARDS_PER_PAGE = 20
NEXT_PAGES = 5

_ADJ = str.split(
    "Arctic Frosted Polar Winter Snowy Glacial Alpine Crisp Nordic Icy Boreal Chilly Misty Silver Midnight "
    "Cozy Woolen Fleece Thermal Rugged Classic Deluxe Compact Ultra Light"
)
_NOUN = str.split(
    "Parka Mitten Beanie Scarf Boots Sled Thermos Blanket Jacket Gloves Lantern Snowshoe Goggles Skis "
    "Kettle Stove Tent Sweater Socks Backpack Headlamp Shovel Hat Vest"
)
_WORDS = str.split(
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et "
    "dolore magna aliqua enim ad minim veniam quis nostrud exercitation ullamco laboris nisi aliquip ex ea "
    "commodo consequat duis aute irure in reprehenderit voluptate velit esse cillum fugiat nulla pariatur"
)


# --------------------------------------------------------------------------- #
# site rendering
# --------------------------------------------------------------------------- #
def _name(j: int) -> str:
    rnd = random.Random(j * 7919 + 1)
    return f"{rnd.choice(_ADJ)} {rnd.choice(_NOUN)} {j}"


def _price(j: int) -> str:
    rnd = random.Random(j * 104729 + 3)
    return f"${rnd.randint(5, 900)}.{rnd.randint(0, 99):02d}"


def _sentence(rnd: random.Random, n: int) -> str:
    return " ".join(rnd.choice(_WORDS) for _ in range(n)).capitalize() + "."


_STYLE = (
    "<style>"
    + "".join(
        f".c{i}{{margin:{i}px;padding:{i % 7}px;color:#{(i * 99991) % 0xFFFFFF:06x};font-size:{10 + i % 9}px}}"
        for i in range(40)
    )
    + "</style>"
)


def _head(title: str, desc: str) -> str:
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        f"<title>{title}</title>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="description" content="{desc}">'
        '<link rel="stylesheet" href="/static/site.css"><link rel="icon" href="/static/favicon.ico">'
        f"{_STYLE}"
        "<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments)}"
        "gtag('js',new Date());gtag('config','G-XXXXXXX');</script>"
        "</head><body>"
    )


def _nav(pages: int) -> str:
    cats = "".join(
        f'<li class="nav-item"><a class="nav-link" href="/page/{(k * 37) % pages}">Category {k}</a></li>'
        for k in range(30)
    )
    return (
        '<header class="site-header"><div class="logo"><a href="/page/0">Winter Shop</a></div>'
        '<form class="search" action="/search"><input type="text" name="q" placeholder="Search products">'
        '<button type="submit">Search</button></form>'
        f'<nav class="main-nav"><ul class="nav-list">{cats}</ul></nav>'
        '<div class="account"><a href="/login">Sign in</a> | <a href="/cart">Cart (0)</a></div></header>'
    )


def _footer(rnd: random.Random) -> str:
    cols = "".join(
        f'<div class="footer-col"><h4>Section {c}</h4><ul>'
        + "".join(f'<li><a href="https://example.org/s{c}/l{k}">Footer link {c}.{k}</a></li>' for k in range(6))
        + "</ul></div>"
        for c in range(4)
    )
    return (
        f'<footer class="site-footer">{cols}<p class="legal">{_sentence(rnd, 40)}</p>'
        "<p>&copy; 2026 Winter Shop. All rights reserved.</p></footer>"
    )


def render_page(i: int, pages: int, items: int, jump_links: bool = True) -> bytes:
    rnd = random.Random(i)
    cards = []
    for k in range(CARDS_PER_PAGE):
        j = (i * CARDS_PER_PAGE + k) % items
        rating = rnd.randint(1, 5)
        cards.append(
            f'<div class="card product c{k}" data-id="{j}" data-sku="SKU-{j:07d}">'
            f'<div class="thumb"><img src="/img/{j}.jpg" alt="{_name(j)}" loading="lazy" width="240" height="240"></div>'
            f'<h3 class="name"><a class="item-link" href="/item/{j}">{_name(j)}</a></h3>'
            f'<p class="price">{_price(j)}</p>'
            f'<p class="rating" aria-label="{rating} of 5 stars">{"&#9733;" * rating}{"&#9734;" * (5 - rating)}</p>'
            f'<p class="blurb">{_sentence(rnd, rnd.randint(25, 45))}</p>'
            f'<button class="add-to-cart" data-id="{j}">Add to cart</button></div>'
        )
    pager = "".join(f'<a class="next" href="/page/{p}">{p}</a>' for p in range(i + 1, min(pages, i + 1 + NEXT_PAGES)))
    if jump_links:
        first = NEXT_PAGES * i + 1
        pager += "".join(
            f'<a class="jump" href="/page/{p}">{p}</a>'
            for p in range(max(first, i + 1 + NEXT_PAGES), min(pages, first + NEXT_PAGES))
        )
    prev = f'<a class="prev" href="/page/{i - 1}">Previous</a>' if i > 0 else ""
    html = (
        _head(f"Winter gear - page {i}", _sentence(rnd, 20))
        + _nav(pages)
        + f'<main class="listing"><h1>Winter gear, page {i}</h1>'
        + f'<div class="intro">{"".join(f"<p>{_sentence(rnd, 30)}</p>" for _ in range(3))}</div>'
        + f'<section class="grid">{"".join(cards)}</section>'
        + f'<nav class="pager">{prev}{pager}</nav></main>'
        + _footer(rnd)
        + "</body></html>"
    )
    return html.encode()


def render_item(j: int, pages: int) -> bytes:
    rnd = random.Random(1_000_003 + j)
    specs = "".join(f"<tr><th>Spec {k}</th><td>{rnd.choice(_WORDS)} {rnd.randint(1, 999)}</td></tr>" for k in range(12))
    html = (
        _head(f"{_name(j)} - Winter Shop", _sentence(rnd, 20))
        + _nav(pages)
        + '<main class="product-detail">'
        + f'<ol class="breadcrumb"><li><a href="/page/0">Home</a></li><li>{_name(j)}</li></ol>'
        + f'<h1 class="product-title">{_name(j)}</h1>'
        + f'<div class="buy-box"><span class="price">{_price(j)}</span><span class="stock">In stock</span>'
        + f'<button class="add-to-cart" data-id="{j}">Add to cart</button></div>'
        + f'<div class="description">{"".join(f"<p>{_sentence(rnd, 35)}</p>" for _ in range(4))}</div>'
        + f'<table class="specs">{specs}</table></main>'
        + _footer(rnd)
        + "</body></html>"
    )
    return html.encode()


def _http_response(status: str, body: bytes, ctype: str = "text/html; charset=utf-8") -> bytes:
    head = (
        f"HTTP/1.1 {status}\r\nServer: fastserver\r\nContent-Type: {ctype}\r\n"
        f"Content-Length: {len(body)}\r\nConnection: keep-alive\r\n\r\n"
    )
    return head.encode("latin-1") + body


def build_site(pages: int, items: int, jump_links: bool = True) -> dict[bytes, bytes]:
    site: dict[bytes, bytes] = {}
    for i in range(pages):
        site[f"/page/{i}".encode()] = _http_response("200 OK", render_page(i, pages, items, jump_links))
    for j in range(items):
        site[f"/item/{j}".encode()] = _http_response("200 OK", render_item(j, pages))
    return site


NOT_FOUND = _http_response("404 Not Found", b"<html><body><h1>Not found</h1></body></html>")
_CLOSE_HEADER = b"Connection: keep-alive\r\n"


# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #
class _Counters:
    """Per-worker request counters in shared memory (no locks: one writer per slot)."""

    def __init__(self, workers: int) -> None:
        self.requests = RawArray(ctypes.c_longlong, workers)
        self.not_found = RawArray(ctypes.c_longlong, workers)

    def snapshot(self) -> dict[str, int]:
        return {"requests": sum(self.requests), "not_found": sum(self.not_found)}

    def reset(self) -> None:
        for arr in (self.requests, self.not_found):
            for k in range(len(arr)):
                arr[k] = 0


class HTTPProtocol(asyncio.Protocol):
    __slots__ = ("_buf", "_closing", "_pending", "_timer", "server", "transport")

    def __init__(self, server: _Server) -> None:
        self.server = server
        self.transport: asyncio.Transport | None = None
        self._buf = b""
        self._pending: collections.deque[tuple[float, bytes, bool]] = collections.deque()
        self._timer: asyncio.TimerHandle | None = None
        self._closing = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def connection_lost(self, exc: BaseException | None) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self.transport = None

    def data_received(self, data: bytes) -> None:
        buf = self._buf + data if self._buf else data
        server = self.server
        while True:
            end = buf.find(b"\r\n\r\n")
            if end < 0:
                if len(buf) > 65536:
                    self._close()
                    return
                break
            head = buf[:end]
            consumed = end + 4
            low = head.lower()
            cl = low.find(b"\r\ncontent-length:")
            if cl >= 0:
                stop = low.find(b"\r\n", cl + 2)
                length = int(low[cl + 17 : stop if stop >= 0 else len(low)].strip() or 0)
                if len(buf) < consumed + length:
                    break
                consumed += length
            buf = buf[consumed:]
            line_end = head.find(b"\r\n")
            request_line = head if line_end < 0 else head[:line_end]
            parts = request_line.split(b" ")
            if len(parts) != 3:
                self._close()
                return
            path = parts[1]
            q = path.find(b"?")
            if q >= 0:
                path = path[:q]
            close = b"connection: close" in low or (parts[2] == b"HTTP/1.0" and b"keep-alive" not in low)
            self._send(server.respond(path), close)
            if close:
                buf = b""
                break
        self._buf = buf

    def _send(self, response: bytes, close: bool) -> None:
        latency = self.server.latency
        if latency <= 0 and not self._pending:
            self._write(response, close)
            return
        loop = self.server.loop
        self._pending.append((loop.time() + latency, response, close))
        if self._timer is None:
            self._timer = loop.call_at(self._pending[0][0], self._flush)

    def _flush(self) -> None:
        self._timer = None
        loop = self.server.loop
        now = loop.time()
        pending = self._pending
        while pending and pending[0][0] <= now + 0.0005:
            _, response, close = pending.popleft()
            self._write(response, close)
            if self.transport is None:
                pending.clear()
                return
        if pending:
            self._timer = loop.call_at(pending[0][0], self._flush)

    def _write(self, response: bytes, close: bool) -> None:
        transport = self.transport
        if transport is None:
            return
        if close:
            response = response.replace(_CLOSE_HEADER, b"Connection: close\r\n", 1)
            transport.write(response)
            self._close()
        else:
            transport.write(response)

    def _close(self) -> None:
        if self.transport is not None and not self._closing:
            self._closing = True
            self.transport.close()


class _Server:
    def __init__(self, site: dict[bytes, bytes], latency: float, counters: _Counters, index: int) -> None:
        self.site = site
        self.latency = latency
        self.counters = counters
        self.index = index
        self.loop = asyncio.get_running_loop()

    def respond(self, path: bytes) -> bytes:
        found = self.site.get(path)
        counters = self.counters
        idx = self.index
        if found is not None:
            counters.requests[idx] += 1
            return found
        if path.startswith(b"/__"):
            if path == b"/__reset":
                counters.reset()
            body = json.dumps(counters.snapshot()).encode()
            return _http_response("200 OK", body, "application/json")
        counters.requests[idx] += 1
        counters.not_found[idx] += 1
        return NOT_FOUND


def _listen_socket(host: str, port: int, reuse_port: bool) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if reuse_port:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind((host, port))
    sock.listen(4096)
    sock.setblocking(False)
    return sock


def _worker(
    index: int,
    host: str,
    port: int,
    reuse_port: bool,
    site: dict[bytes, bytes],
    latency: float,
    counters: _Counters,
    ready: Any,
    sock: socket.socket | None = None,
) -> None:
    if sock is None:
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # forked worker: the parent handles Ctrl+C

    async def main() -> None:
        listen = sock or _listen_socket(host, port, reuse_port)
        server = _Server(site, latency, counters, index)
        srv = await asyncio.get_running_loop().create_server(lambda: HTTPProtocol(server), sock=listen, backlog=4096)
        if ready is not None:
            ready.set()
        async with srv:
            await srv.serve_forever()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover
        pass


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pages", type=int, default=2000, help="number of /page/<i> listing pages")
    parser.add_argument("--items", type=int, default=8000, help="number of /item/<j> detail pages")
    parser.add_argument("--latency", type=float, default=0.0, help="per-response delay in milliseconds")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    parser.add_argument("--workers", type=int, default=1, help="server processes sharing the port (SO_REUSEPORT)")
    parser.add_argument(
        "--no-jump-links", action="store_true", help="pager links only to the next 5 pages (deep graph)"
    )
    args = parser.parse_args(argv)
    if args.pages < 1 or args.items < 1:
        parser.error("--pages and --items must be >= 1")

    site = build_site(args.pages, args.items, not args.no_jump_links)  # rendered once, shared with forked workers
    latency = args.latency / 1000.0
    workers = max(1, args.workers)
    counters = _Counters(workers)
    ctx = multiprocessing.get_context("fork")

    if workers == 1:
        sock = _listen_socket(args.host, args.port, reuse_port=False)
        port = sock.getsockname()[1]
        print(f"http://{args.host}:{port}", flush=True)
        _print_info(args, site)
        _worker(0, args.host, port, False, site, latency, counters, None, sock)
        return

    # Reserve a port, then let every worker bind its own SO_REUSEPORT socket so the
    # kernel spreads connections across them.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    probe.bind((args.host, args.port))
    port = probe.getsockname()[1]
    procs = []
    for index in range(workers):
        ready = ctx.Event()
        proc = ctx.Process(
            target=_worker,
            args=(index, args.host, port, True, site, latency, counters, ready),
            daemon=True,
        )
        proc.start()
        ready.wait(30)
        procs.append(proc)
    probe.close()  # bound but never listening: it received no connections
    print(f"http://{args.host}:{port}", flush=True)
    _print_info(args, site)

    def stop(*_: Any) -> None:
        for proc in procs:
            proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for proc in procs:
        proc.join()


def _print_info(args: argparse.Namespace, site: dict[bytes, bytes]) -> None:
    page_sizes = [len(v) for k, v in site.items() if k.startswith(b"/page/")]
    item_sizes = [len(v) for k, v in site.items() if k.startswith(b"/item/")]
    print(
        f"# pages={args.pages} (avg {sum(page_sizes) / len(page_sizes) / 1024:.1f} KB) "
        f"items={args.items} (avg {sum(item_sizes) / len(item_sizes) / 1024:.1f} KB) "
        f"latency={args.latency}ms workers={args.workers} pid={os.getpid()}",
        file=sys.stderr,
        flush=True,
    )


if __name__ == "__main__":
    main()
