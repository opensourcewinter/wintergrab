from __future__ import annotations

import json

import pytest

import wintergrab as wg
from wintergrab import AsyncBrowserFetcher, BrowserFetcher

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def browser():
    with BrowserFetcher() as b:
        yield b


def test_renders_javascript(site, browser) -> None:
    plain = wg.get(site.url + "/js")
    assert plain.css(".item") == []
    page = browser.get(site.url + "/js", wait_for=".item")
    assert page.source == "browser"
    assert page.css(".item::text").getall() == ["Item 1", "Item 2", "Item 3"]


def test_the_browser_does_not_hide_that_it_is_automated(site, browser) -> None:
    data = json.loads(browser.get(site.url + "/navigator").css("#out::text").get())
    assert data["webdriver"] is True  # what a browser driven by a program says of itself
    assert data["languages"][0] == "en-US"
    with pytest.raises(TypeError, match="stealth"):
        BrowserFetcher(stealth=True)  # gone: nothing patches a page to pass for a person


def test_waits_out_challenge_pages(site, browser) -> None:
    page = browser.get(site.url + "/challenge")
    assert page.status == 200
    assert page.css("#real::text").get() == "Real content"


def test_captures_the_whole_page_after_a_challenge_reloads(site, browser) -> None:
    # pypi.org's check reloads the page, which then streams in (1.2 MB there).
    # The capture must wait for all of it, not stop once the title has arrived.
    for route in ("/challenge-reload", "/challenge-rewrite"):
        page = browser.get(site.url + route)
        assert page.css("title::text").get() == "Real page", route
        assert len(page.css(".item")) == 400, route


def test_non_html_bodies_and_encoding(site, browser) -> None:
    assert browser.get(site.url + "/json").json() == {"items": [1, 2, 3], "ok": True}
    assert browser.get(site.url + "/latin1").css("#t::text").get() == "Café crème"


def test_page_action_and_screenshot(site, browser, tmp_path) -> None:
    async def action(page) -> None:
        await page.evaluate("document.body.insertAdjacentHTML('beforeend', '<p id=added>hi</p>')")

    shot = tmp_path / "shot.png"
    page = browser.get(site.url + "/product/1", page_action=action, screenshot=shot)
    assert page.css("#added::text").get() == "hi"
    assert shot.stat().st_size > 1000


def test_status_codes_are_reported(site, browser) -> None:
    assert browser.get(site.url + "/status/404", retries=0).status == 404


def test_render_shortcut(site) -> None:
    assert wg.render(site.url + "/js", wait_for=".item").css(".item").texts == ["Item 1", "Item 2", "Item 3"]


async def test_async_browser_fetcher(site) -> None:
    async with AsyncBrowserFetcher(max_pages=2) as browser:
        pages = await browser.get_many([f"{site.url}/product/{i}" for i in range(1, 4)])
    assert [p.css("h1::text").get() for p in pages] == ["Product 1", "Product 2", "Product 3"]


def test_non_get_is_rejected(site, browser) -> None:
    with pytest.raises(ValueError):
        browser._run(lambda: browser._async.request("POST", site.url))


def test_capture_api_calls(site, browser) -> None:
    page = browser.get(site.url + "/spa", wait_for=".row", capture=True, wait_until="networkidle")
    assert page.css(".row::text").getall() == ["API item 3", "API item 4", "API item 5"]
    urls = [c.url for c in page.captured]
    assert any("page=1" in u for u in urls) and any("page=2" in u for u in urls)
    first = page.captured_json("page=1")[0]
    assert first["items"][0]["name"] == "API item 3"
    assert page.captured[0].method == "GET" and page.captured[0].status == 200
    only_page2 = browser.get(site.url + "/spa", wait_for=".row", capture="*page=2*", wait_until="networkidle")
    assert [c.url.endswith("page=2") for c in only_page2.captured] == [True]


SIGN_IN = ["fill #user => ada", "click #signin", "wait #member"]  # the site's own form


def test_export_cookies_to_http_session(site, browser) -> None:
    page = browser.get(site.url + "/members/login", actions=SIGN_IN)
    assert page.css("#member::text").get() == "Members page 1 for ada"
    cookies = browser.export_cookies(site.url)
    assert any(c["name"] == "member" for c in cookies)
    with wg.Fetcher() as http:
        assert http.get(site.url + "/members/2").status == 401
        http.add_cookies(cookies)  # signed in once, in the browser; on at HTTP speed
        assert http.get(site.url + "/members/2").css("#member::text").get() == "Members page 2 for ada"


def test_spider_hands_browser_cookies_to_http(fresh_site) -> None:
    class Members(wg.Spider):
        log_level = None
        obey_robots_txt = False
        concurrency = 1

        def configure_sessions(self, sessions):
            super().configure_sessions(sessions)
            sessions.add("browser", AsyncBrowserFetcher(retries=0))

        def start_requests(self):
            yield wg.Request(fresh_site.url + "/members/login", session="browser", options={"actions": SIGN_IN})

        def parse(self, response):
            yield {"page": response.css("#member::text").get(), "source": response.source}
            if response.source == "browser":
                yield from (response.follow(f"/members/{i}") for i in range(2, 5))

    result = Members().run()
    by_page = {i["page"]: i["source"] for i in result.items}
    assert by_page == {f"Members page {i} for ada": "browser" if i == 1 else "http" for i in range(1, 5)}
    assert result.stats["cookie_handoffs"] == 1
