"""A tiny local website (and forward proxy) for exercising wintergrab offline."""

from __future__ import annotations

import gzip
import http.client
import json
import threading
import time
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

QUOTES = [
    ("The world as we have created it is a process of our thinking.", "Albert Einstein", ["change", "thinking"]),
    ("It is our choices that show what we truly are.", "J.K. Rowling", ["abilities", "choices"]),
    ("There are only two ways to live your life.", "Albert Einstein", ["inspirational", "life"]),
    ("The person, be it gentleman or lady, who has not pleasure in a good novel...", "Jane Austen", ["books"]),
    ("Imperfection is beauty, madness is genius.", "Marilyn Monroe", ["be-yourself"]),
    ("Try not to become a man of success.", "Albert Einstein", ["adulthood", "success"]),
]
QUOTE_PAGES = 3
BOOK_PAGES = 3
BOOKS_PER_PAGE = 4
RATINGS = ["One", "Two", "Three", "Four", "Five"]
PRODUCT_PAGES = 5
PER_PAGE = 4


def product(i: int) -> dict[str, Any]:
    return {"id": i, "name": f"Product {i}", "price": round(5 + i * 1.25, 2)}


def layout(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body><nav><a href='/'>Home</a> <a href='/quotes/'>Quotes</a></nav>"
        f"{body}</body></html>"
    )


class Site:
    """State shared by all request handlers of one server."""

    def __init__(self) -> None:
        self.hits: Counter[str] = Counter()
        self.counters: defaultdict[str, int] = defaultdict(int)
        self.log: list[tuple[float, str, dict[str, str]]] = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def bump(self, key: str) -> int:
        with self.lock:
            self.counters[key] += 1
            return self.counters[key]


class Handler(BaseHTTPRequestHandler):
    server: SiteServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # silence
        pass

    # -- helpers -------------------------------------------------------- #
    def send(
        self, status: int, body: str | bytes, ctype: str = "text/html; charset=utf-8", headers: dict | None = None
    ) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            if isinstance(v, list):
                for item in v:
                    self.send_header(k, item)
            else:
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        self.send(
            200,
            json.dumps(
                {"method": "POST", "path": self.path, "body": body, "content_type": self.headers.get("Content-Type")}
            ),
            "application/json",
        )

    def do_GET(self) -> None:
        site = self.server.site
        with site.lock:
            site.active += 1
            site.max_active = max(site.max_active, site.active)
        try:
            self._route(site)
        finally:
            with site.lock:
                site.active -= 1

    def _route(self, site: Site) -> None:
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        with site.lock:
            site.hits[path] += 1
            site.log.append((time.monotonic(), self.path, dict(self.headers)))
        q = lambda name, default=None: query.get(name, [default])[0]  # noqa: E731

        if path == "/":
            links = "".join(f"<li><a href='/products/page/{n}'>Page {n}</a></li>" for n in range(1, PRODUCT_PAGES + 1))
            return self.send(200, layout("Test shop", f"<h1 id='title'>Test shop</h1><ul class='pages'>{links}</ul>"))
        if path.startswith("/products/page/"):
            n = int(path.rsplit("/", 1)[1] or 1)
            items = "".join(
                f"<div class='product' data-id='{p['id']}'><h2 class='name'><a href='/product/{p['id']}'>{p['name']}</a></h2>"
                f"<span class='price'>${p['price']:.2f}</span></div>"
                for p in (product(i) for i in range((n - 1) * PER_PAGE + 1, n * PER_PAGE + 1))
            )
            nxt = f"<a class='next' href='/products/page/{n + 1}'>Next</a>" if n < PRODUCT_PAGES else ""
            return self.send(200, layout(f"Products {n}", f"<div id='products'>{items}</div>{nxt}"))
        if path.startswith("/product/"):
            p = product(int(path.rsplit("/", 1)[1]))
            return self.send(
                200,
                layout(
                    p["name"],
                    f"<h1>{p['name']}</h1><p class='price'>${p['price']:.2f}</p>"
                    f"<p class='desc'>A very fine product.</p><a href='/product/{p['id']}'>self</a>",
                ),
            )
        if path.startswith("/quotes"):
            seg = [s for s in path.split("/") if s]
            n = int(seg[2]) if len(seg) >= 3 and seg[1] == "page" else 1
            per = len(QUOTES) // QUOTE_PAGES
            chunk = QUOTES[(n - 1) * per : n * per]
            quotes = "".join(
                f"<div class='quote'><span class='text'>“{t}”</span><span>by <small class='author'>{a}</small>"
                f" <a href='/author/{a.replace(' ', '-')}'>(about)</a></span>"
                f"<div class='tags'>Tags: {''.join(f'<a class=tag href=/tag/{g}/>{g}</a>' for g in tags)}</div></div>"
                for t, a, tags in chunk
            )
            nxt = (
                f"<nav><ul class='pager'><li class='next'><a href='/quotes/page/{n + 1}/'>Next</a></li></ul></nav>"
                if n < QUOTE_PAGES
                else ""
            )
            return self.send(200, layout("Quotes to Scrape", f"<div class='col-md-8'>{quotes}{nxt}</div>"))
        if path.startswith("/books/"):
            return self._books(path)
        if path.startswith("/author/"):
            name = path.rsplit("/", 1)[1].replace("-", " ")
            return self.send(
                200, layout(name, f"<h3 class='author-title'>{name}</h3><span class='author-born-date'>1879</span>")
            )
        if path.startswith("/tag/"):
            return self.send(200, layout("tag", "<p>tag page</p>"))
        if path.startswith("/status/"):
            code = int(path.rsplit("/", 1)[1])
            return self.send(code, layout(str(code), f"<p>status {code}</p>"))
        if path.startswith("/flaky/"):
            key = path.rsplit("/", 1)[1]
            fail = int(q("fail", "2"))
            code = int(q("code", "503"))
            n = site.bump("flaky:" + key)
            if n <= fail:
                return self.send(code, layout("err", "<p>temporarily unavailable</p>"))
            return self.send(200, layout("ok", f"<p id='attempt'>{n}</p>"))
        if path.startswith("/ratelimited/"):
            key = path.rsplit("/", 1)[1]
            limit = int(q("limit", "1"))
            n = site.bump("rl:" + key)
            if n <= limit:
                return self.send(429, "slow down", "text/plain", {"Retry-After": q("after", "1")})
            return self.send(200, layout("ok", f"<p id='attempt'>{n}</p>"))
        if path == "/slow":
            time.sleep(float(q("delay", "0.5")))
            return self.send(200, layout("slow", "<p>finally</p>"))
        if path == "/redirect":
            return self.send(302, "", headers={"Location": q("to", "/")})
        if path == "/headers":
            return self.send(200, json.dumps(dict(self.headers)), "application/json")
        if path == "/cookies/set":
            cookies = [f"{k}={v[0]}; Path=/" for k, v in query.items()]
            return self.send(302, "", headers={"Location": "/cookies", "Set-Cookie": cookies})
        if path == "/cookies":
            raw = self.headers.get("Cookie", "")
            jar = dict(c.strip().split("=", 1) for c in raw.split(";") if "=" in c)
            return self.send(200, json.dumps(jar), "application/json")
        if path == "/robots.txt":
            host = self.headers.get("Host", "127.0.0.1")
            body = f"User-agent: *\nDisallow: /private/\nSitemap: http://{host}/sitemap_index.xml\n"
            return self.send(200, body, "text/plain")
        if path == "/sitemap_index.xml":
            host = self.headers.get("Host", "127.0.0.1")
            xml = (
                "<?xml version='1.0' encoding='UTF-8'?><sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                f"<sitemap><loc>http://{host}/sitemap-products.xml.gz</loc></sitemap>"
                f"<sitemap><loc>http://{host}/sitemap-items.xml</loc></sitemap></sitemapindex>"
            )
            return self.send(200, xml, "application/xml")
        if path == "/sitemap-products.xml.gz":
            host = self.headers.get("Host", "127.0.0.1")
            urls = "".join(
                f"<url><loc>http://{host}/product/{i}</loc><lastmod>2026-0{i}-01</lastmod></url>" for i in range(1, 6)
            )
            xml = f"<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>{urls}</urlset>"
            return self.send(200, gzip.compress(xml.encode()), "application/x-gzip")
        if path == "/sitemap-items.xml":
            urls = "".join(f"<url><loc>/item/{i}</loc></url>" for i in range(3))
            xml = f"<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>{urls}</urlset>"
            return self.send(200, xml, "text/xml")
        if path.startswith("/private/"):
            return self.send(200, layout("private", "<p>secret</p>"))
        if path == "/js":
            html = layout(
                "JS page",
                "<div id='app'>loading...</div><script>setTimeout(function(){"
                "document.getElementById('app').innerHTML = '<ul>' + [1,2,3].map(function(i){"
                "return '<li class=\"item\">Item ' + i + '</li>'}).join('') + '</ul>';}, 200);</script>",
            )
            return self.send(200, html)
        if path == "/navigator":
            html = layout(
                "nav",
                "<pre id='out'></pre><script>document.getElementById('out').textContent = JSON.stringify({"
                "webdriver: navigator.webdriver, ua: navigator.userAgent, plugins: navigator.plugins.length,"
                "languages: navigator.languages});</script>",
            )
            return self.send(200, html)
        if path == "/challenge":
            if q("passed"):
                return self.send(200, layout("Welcome", "<h1 id='real'>Real content</h1>"))
            html = (
                "<html><head><title>Just a moment...</title></head><body><p>Checking your browser</p>"
                "<script>setTimeout(function(){location.href='/challenge?passed=1'}, 800);</script></body></html>"
            )
            return self.send(503, html)
        if path.startswith("/etag/"):
            version = q("v", "1")
            etag = f'"v{version}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            return self.send(200, layout("etag", f"<p id='v'>{version}</p>"), headers={"ETag": etag})
        if path == "/maxage":
            n = site.bump("maxage")
            return self.send(200, layout("maxage", f"<p id='n'>{n}</p>"), headers={"Cache-Control": "max-age=60"})
        if path == "/nostore":
            n = site.bump("nostore")
            return self.send(200, layout("nostore", f"<p id='n'>{n}</p>"), headers={"Cache-Control": "no-store"})
        if path == "/api/products":
            page_no = int(q("page", "1"))
            data = {
                "page": page_no,
                "items": [{"id": i, "name": f"API item {i}"} for i in range(page_no * 3, page_no * 3 + 3)],
            }
            return self.send(200, json.dumps(data), "application/json")
        if path == "/spa":
            html = layout(
                "SPA",
                "<div id='root'>loading</div><script>fetch('/api/products?page=1').then(r => r.json()).then(d => {"
                "document.getElementById('root').innerHTML = d.items.map(i => '<p class=\"row\">' + i.name + '</p>').join('');"
                "return fetch('/api/products?page=2');}).then(r => r.json()).then(d => {document.title = 'SPA loaded';});"
                "</script>",
            )
            return self.send(200, html)
        if path.startswith("/rich/"):
            n = int(path.rsplit("/", 1)[1])
            next_link = f"<a rel='next' href='/rich/{n + 1}'>Next ›</a>" if n < 3 else ""
            body = (
                "<script type='application/ld+json'>"
                + json.dumps(
                    {
                        "@context": "https://schema.org",
                        "@type": "Product",
                        "name": f"Rich product {n}",
                        "offers": {"@type": "Offer", "price": f"{n}9.99", "priceCurrency": "USD"},
                    }
                )
                + "</script>"
                f"<div itemscope itemtype='https://schema.org/Product'><span itemprop='name'>Micro {n}</span>"
                "<div itemprop='aggregateRating' itemscope itemtype='https://schema.org/AggregateRating'>"
                "<meta itemprop='ratingValue' content='4.5'></div></div>"
                "<script id='__NEXT_DATA__' type='application/json'>"
                + json.dumps({"props": {"pageProps": {"product": {"id": n, "price": n * 10}}}})
                + "</script>"
                f"<script>window.__INITIAL_STATE__ = {json.dumps({'cart': {'items': [], 'price': 0}})};</script>"
                "<table><thead><tr><th>Spec</th><th>Value</th></tr></thead>"
                "<tbody><tr><td>Weight</td><td>1 kg</td></tr><tr><td>Color</td><td>Blue</td></tr></tbody></table>"
                f"<nav class='pagination'>{next_link}</nav>"
            )
            head = f"<meta property='og:title' content='Rich {n}'><meta property='og:image' content='/img/{n}.png'>"
            html = layout(f"Rich {n}", body).replace("<head>", "<head>" + head, 1)
            return self.send(200, html)
        if path.startswith("/jsgate/"):
            # Content needs a cookie that only a JavaScript-running client gets.
            if "gate=passed" in (self.headers.get("Cookie") or ""):
                n = path.rsplit("/", 1)[1]
                return self.send(200, layout(f"gated {n}", f"<h1 id='gated'>Gated {n}</h1>"))
            html = (
                "<html><head><title>Just a moment...</title></head><body>Checking your browser"
                "<script>document.cookie = 'gate=passed; path=/'; setTimeout(function(){location.reload()}, 300);</script>"
                "</body></html>"
            )
            return self.send(403, html)
        if path == "/guarded":
            if self.headers.get("X-Solved") == "yes":
                return self.send(200, layout("Guarded", "<h1 id='real'>Guarded content</h1>"))
            return self.send(403, "<html><head><title>Just a moment...</title></head><body>cf-chl</body></html>")
        if path == "/blocked":
            return self.send(403, "<html><head><title>Just a moment...</title></head><body>cf-chl-bypass</body></html>")
        if path == "/latin1":
            return self.send(
                200, layout("latin", "<p id='t'>Café crème</p>").encode("latin-1"), "text/html; charset=iso-8859-1"
            )
        if path == "/meta-charset":
            body = "<html><head><meta charset='windows-1251'><title>t</title></head><body><p id='t'>Привет</p></body></html>"
            return self.send(200, body.encode("cp1251"), "text/html")
        if path == "/json":
            return self.send(200, json.dumps({"items": [1, 2, 3], "ok": True}), "application/json")
        if path.startswith("/deep/"):
            n = int(path.rsplit("/", 1)[1])
            return self.send(200, layout(f"deep {n}", f"<a href='/deep/{n + 1}'>deeper</a>"))
        if path == "/links":
            count = int(q("n", "30"))
            links = "".join(f"<a href='/item/{i}'>item {i}</a>" for i in range(count))
            return self.send(
                200,
                layout(
                    "links",
                    links + "<a href='https://elsewhere.invalid/x'>offsite</a><a href='/private/secret'>private</a>",
                ),
            )
        if path.startswith("/item/"):
            i = path.rsplit("/", 1)[1]
            delay = float(q("delay", "0"))
            if delay:
                time.sleep(delay)
            return self.send(200, layout(f"item {i}", f"<h1>Item {i}</h1><a href='/item/{i}'>self</a>"))
        if path == "/sitemap.xml":
            xml = (
                "<?xml version='1.0' encoding='UTF-8'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                "<url><loc>http://example.test/a</loc></url><url><loc>http://example.test/b</loc></url></urlset>"
            )
            return self.send(200, xml, "application/xml")
        return self.send(404, layout("Not found", "<p>nope</p>"))


def _book(i: int) -> dict[str, Any]:
    return {
        "title": f"Book number {i}",
        "price": f"\u00a3{10 + i * 1.5:.2f}",
        "rating": RATINGS[i % 5],
        "stock": 3 + i,
        "upc": f"upc{i:04d}",
        "category": "Poetry" if i % 2 else "Travel",
    }


def _books_page(handler: Handler, path: str) -> None:
    """A miniature of books.toscrape.com (same markup, fewer books)."""
    if path in ("/books/", "/books/index.html") or path.startswith("/books/catalogue/page-"):
        n = 1 if not path.startswith("/books/catalogue/page-") else int(path.rsplit("-", 1)[1].split(".")[0])
        prefix = "catalogue/" if not path.startswith("/books/catalogue/") else ""
        pods = "".join(
            f"<li><article class='product_pod'><div class='image_container'><a href='{prefix}book-{i}/index.html'>"
            f"<img src='media/{i}.jpg' alt='{b['title']}' class='thumbnail'></a></div>"
            f"<p class='star-rating {b['rating']}'></p><h3><a href='{prefix}book-{i}/index.html' title='{b['title']}'>"
            f"{b['title'][:10]}...</a></h3><div class='product_price'><p class='price_color'>{b['price']}</p>"
            f"<p class='instock availability'><i class='icon-ok'></i> In stock</p></div></article></li>"
            for i, b in ((i, _book(i)) for i in range((n - 1) * BOOKS_PER_PAGE + 1, n * BOOKS_PER_PAGE + 1))
        )
        nxt = f"<li class='next'><a href='{prefix}page-{n + 1}.html'>next</a></li>" if n < BOOK_PAGES else ""
        return handler.send(
            200, layout("All products | Books to Scrape", f"<ol class='row'>{pods}</ol><ul class='pager'>{nxt}</ul>")
        )
    if path.startswith("/books/catalogue/book-"):
        i = int(path.split("book-")[1].split("/")[0])
        b = _book(i)
        body = (
            f"<ul class='breadcrumb'><li><a href='../../index.html'>Home</a></li><li><a href='#'>Books</a></li>"
            f"<li><a href='#'>{b['category']}</a></li><li class='active'>{b['title']}</li></ul>"
            f"<div class='col-sm-6 product_main'><h1>{b['title']}</h1><p class='price_color'>{b['price']}</p>"
            f"<p class='instock availability'><i class='icon-ok'></i> In stock ({b['stock']} available)</p>"
            f"<p class='star-rating {b['rating']}'></p></div>"
            f"<table class='table table-striped'><tr><th>UPC</th><td>{b['upc']}</td></tr>"
            f"<tr><th>Product Type</th><td>Books</td></tr></table>"
        )
        return handler.send(200, layout(f"{b['title']} | Books to Scrape", body))
    return handler.send(404, layout("Not found", "<p>nope</p>"))


Handler._books = _books_page  # type: ignore[attr-defined]


class SiteServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.site = Site()
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def start(self) -> SiteServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class ProxyHandler(BaseHTTPRequestHandler):
    """Forward proxy for plain-HTTP absolute-form requests. Tags responses."""

    server: ProxyServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self.server.used += 1
        if self.server.broken:
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        target = urlsplit(self.path)
        conn = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=10)
        path = target.path + (f"?{target.query}" if target.query else "")
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("proxy-connection", "connection")}
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                self.send_header(k, v)
        self.send_header("X-Via-Proxy", self.server.name)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        conn.close()


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, name: str, broken: bool = False) -> None:
        super().__init__(("127.0.0.1", 0), ProxyHandler)
        self.name = name
        self.broken = broken
        self.used = 0
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def start(self) -> ProxyServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


if __name__ == "__main__":  # manual poking: python tests/testsite.py
    server = SiteServer().start()
    print("serving on", server.url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
