from __future__ import annotations

import time

import pytest

import wintergrab as wg
from wintergrab import AsyncFetcher, Fetcher, FetchError, HTTPStatusError
from wintergrab.fetchers.response import Headers, Response, detect_encoding


def test_get_parses_page(site) -> None:
    page = wg.get(site.url + "/products/page/1")
    assert page.status == 200 and page.ok
    assert page.title == "Products 1"
    assert page.css(".product .name a::text").getall() == [f"Product {i}" for i in range(1, 5)]
    assert page.source == "http"
    assert page.elapsed >= 0


def test_browser_like_headers_by_default(site) -> None:
    headers = {k.lower(): v for k, v in wg.get(site.url + "/headers").json().items()}
    assert "Chrome/" in headers["user-agent"]
    assert "sec-ch-ua" in headers
    plain = {k.lower(): v for k, v in wg.get(site.url + "/headers", impersonate=None).json().items()}
    assert "sec-ch-ua" not in plain


def test_custom_headers_and_referer(site) -> None:
    echoed = wg.get(site.url + "/headers", headers={"X-Test": "1"}, referer="google").json()
    assert echoed["X-Test"] == "1"
    assert echoed["Referer"] == "https://www.google.com/"


def test_encodings(site) -> None:
    assert wg.get(site.url + "/latin1").css("#t::text").get() == "Café crème"
    meta = wg.get(site.url + "/meta-charset")
    assert meta.encoding == "cp1251"
    assert meta.css("#t::text").get() == "Привет"


def test_json_and_post(site) -> None:
    assert wg.get(site.url + "/json").json() == {"items": [1, 2, 3], "ok": True}
    form = wg.post(site.url + "/post", data={"a": "1"}).json()
    assert form["body"] == "a=1" and "form-urlencoded" in form["content_type"]
    body = wg.post(site.url + "/post", json={"a": 1}).json()
    assert body["body"] == '{"a":1}'


def test_params_are_merged(site) -> None:
    page = wg.get(site.url + "/item/7", params={"q": "x y"})
    assert page.url.endswith("/item/7?q=x+y")


def test_session_keeps_cookies_and_follows_redirects(site) -> None:
    with Fetcher() as fetcher:
        page = fetcher.get(site.url + "/cookies/set?token=abc")
        assert page.json() == {"token": "abc"}
        assert page.history == [site.url + "/cookies/set?token=abc"]
        assert fetcher.session_cookies == {"token": "abc"}
        assert fetcher.get(site.url + "/cookies").json() == {"token": "abc"}


def test_retries_on_retryable_status(fresh_site) -> None:
    page = wg.get(fresh_site.url + "/flaky/x?fail=2", retries=2, backoff=0.01)
    assert page.status == 200
    assert page.css("#attempt::text").get() == "3"


def test_gives_up_after_retries(fresh_site) -> None:
    page = wg.get(fresh_site.url + "/flaky/y?fail=5", retries=1, backoff=0.01)
    assert page.status == 503
    assert fresh_site.site.hits["/flaky/y"] == 2


def test_retry_after_is_respected(fresh_site) -> None:
    started = time.monotonic()
    page = wg.get(fresh_site.url + "/ratelimited/z?limit=1&after=1", retries=1, backoff=0.01)
    assert page.status == 200
    assert time.monotonic() - started >= 0.9


def test_status_errors(site) -> None:
    page = wg.get(site.url + "/status/404")
    assert page.status == 404 and not page.ok
    with pytest.raises(HTTPStatusError):
        page.raise_for_status()
    with pytest.raises(HTTPStatusError):
        wg.get(site.url + "/status/500", raise_for_status=True, retries=0)


def test_network_errors_raise_fetch_error() -> None:
    with pytest.raises(FetchError) as info:
        wg.get("http://127.0.0.1:9/", retries=1, backoff=0.01, timeout=5)
    assert info.value.url == "http://127.0.0.1:9/"
    assert info.value.retryable


def test_scheme_is_added() -> None:
    with pytest.raises(FetchError) as info:
        wg.get("127.0.0.1:9/x", retries=0, timeout=5)
    assert info.value.url == "https://127.0.0.1:9/x"


def test_follow_builds_requests(site) -> None:
    page = wg.get(site.url + "/products/page/1")
    req = page.follow(page.css("a.next")[0])
    assert req.url == site.url + "/products/page/2"
    reqs = page.follow_all(".product .name")
    assert [r.url for r in reqs] == [site.url + f"/product/{i}" for i in range(1, 5)]
    assert page.follow_all(urls=["/a", "/b"])[1].url == site.url + "/b"


def test_markdown_and_text_of_a_response(site) -> None:
    page = wg.get(site.url + "/product/3")
    assert "# Product 3" in page.markdown()
    assert "A very fine product." in page.get_text()


def test_save_body(site, tmp_path) -> None:
    path = wg.get(site.url + "/json").save(tmp_path / "out" / "data.json")
    assert path.read_bytes().startswith(b'{"items"')


def test_headers_mapping() -> None:
    headers = Headers([("Set-Cookie", "a=1"), ("set-cookie", "b=2"), ("Content-Type", "text/html")])
    assert headers["content-type"] == "text/html"
    assert headers.get_list("SET-COOKIE") == ["a=1", "b=2"]
    assert "CONTENT-TYPE" in headers and len(headers) == 2


def test_detect_encoding() -> None:
    assert detect_encoding("text/html; charset=ISO-8859-1", b"") == "iso8859-1"
    assert detect_encoding(None, b'<meta charset="windows-1252">') == "cp1252"
    assert detect_encoding(None, b"\xef\xbb\xbfhello") == "utf-8"
    assert detect_encoding("text/html; charset=bogus", b"") == "utf-8"


def test_xml_response_uses_xml_parser(site) -> None:
    sitemap = wg.get(site.url + "/sitemap.xml")
    assert sitemap.selector.type == "xml"
    sitemap.selector.remove_namespaces()
    assert sitemap.xpath("//loc/text()").getall() == ["http://example.test/a", "http://example.test/b"]


def test_response_without_network() -> None:
    page = Response("https://x.test/a", body=b"<html><body><a href='b'>b</a></body></html>")
    assert page.links() == ["https://x.test/b"]
    assert page.meta == {}


async def test_async_fetcher_get_many(site) -> None:
    async with AsyncFetcher() as fetcher:
        pages = await fetcher.get_many([f"{site.url}/product/{i}" for i in range(1, 9)], concurrency=4)
        assert [p.css("h1::text").get() for p in pages] == [f"Product {i}" for i in range(1, 9)]
        results = await fetcher.get_many([site.url + "/product/1", "http://127.0.0.1:9/"], retries=0)
        assert isinstance(results[1], FetchError)


async def test_async_iter_many_yields_as_completed(site) -> None:
    # 0.3 s between finishes: slow CI runners can start one connection ~0.1 s late.
    urls = [f"{site.url}/item/{i}?delay={0.3 * (3 - i):.1f}" for i in range(3)]
    async with AsyncFetcher() as fetcher:
        order = [r.url async for r in fetcher.iter_many(urls, concurrency=3)]
    assert order == list(reversed(urls))


async def test_aget(site) -> None:
    page = await wg.aget(site.url + "/")
    assert page.css("#title::text").get() == "Test shop"
    posted = await wg.apost(site.url + "/post", json={"x": 1})
    assert posted.json()["body"] == '{"x":1}'


async def test_no_proactor_warning_from_curl_cffi(site, monkeypatch) -> None:
    # On Windows curl_cffi warns that the Proactor loop needs a helper thread.
    # Simulate that anywhere and check the warning doesn't reach the user.
    import warnings

    import curl_cffi.aio

    real = curl_cffi.aio.get_selector

    def windows_like(loop):
        warnings.warn("\n    Proactor event loop does not implement add_reader ...", UserWarning, stacklevel=2)
        return real(loop)

    monkeypatch.setattr(curl_cffi.aio, "get_selector", windows_like)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        async with AsyncFetcher() as http:
            assert (await http.get(site.url + "/products/page/1")).status == 200
    assert not [w for w in caught if "Proactor" in str(w.message)]
