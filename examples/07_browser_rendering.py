"""JavaScript-rendered pages with a headless browser.

Needs the browser extra:  pip install "wintergrab[browser]" && playwright install chromium

    python examples/07_browser_rendering.py
"""

import wintergrab as wg

URL = "https://quotes.toscrape.com/js/"  # quotes are inserted by JavaScript


async def click_nothing_but_scroll(page) -> None:
    """page_action gets Playwright's async Page: click, type, scroll... anything."""
    await page.mouse.wheel(0, 2000)


def main(url: str = URL, selector: str = ".quote") -> list[str]:
    # Plain HTTP only sees the empty template:
    print("plain HTTP:", len(wg.get(url).css(selector)), "matches")

    # A real (headless) Chromium runs the JavaScript first.
    with wg.BrowserFetcher(headless=True, block_resources=("image", "font", "media")) as browser:
        page = browser.get(url, wait_for=selector, page_action=click_nothing_but_scroll)
        texts = page.css(f"{selector} .text::text").getall() or page.css(selector).texts
        print("browser:", len(page.css(selector)), "matches;", texts[:1])
    return texts


if __name__ == "__main__":
    main()
