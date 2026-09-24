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


def test_stealth_hides_automation_tells(site, browser) -> None:
    data = json.loads(browser.get(site.url + "/navigator").css("#out::text").get())
    assert "webdriver" not in data  # undefined, like a normal browser
    assert "HeadlessChrome" not in data["ua"]
    assert data["plugins"] > 0
    assert data["languages"][0] == "en-US"


def test_waits_out_challenge_pages(site, browser) -> None:
    page = browser.get(site.url + "/challenge")
    assert page.status == 200
    assert page.css("#real::text").get() == "Real content"


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
