"""The 60-second tour: fetch a page, select things, pull out data.

python examples/01_quickstart.py
"""

import wintergrab as wg

URL = "https://quotes.toscrape.com/"


def main(url: str = URL) -> list[dict]:
    # One call. It looks like Chrome at the TLS/HTTP2 level and retries hiccups.
    page = wg.get(url)
    print(page.status, page.title)

    # CSS selectors, with ::text and ::attr(name) to get strings out.
    print(page.css(".quote .text::text").get())
    print(page.css(".quote .author::text").getall()[:3])

    # XPath works too, and you can mix both.
    print(page.xpath("//small[@class='author']/text()").get())

    # Loop over elements and query inside each one.
    quotes = []
    for quote in page.css(".quote"):
        quotes.append(
            {
                "text": quote.css(".text::text").get(),
                "author": quote.css(".author::text").get(),
                "tags": quote.css(".tag::text").getall(),
            }
        )
    print(f"{len(quotes)} quotes, first: {quotes[0]}")

    # Links come back absolute; follow them with another get().
    next_page = page.css("li.next a::attr(href)").get()
    if next_page:
        print("next page:", page.urljoin(next_page))

    # Handy extras.
    print(page.find_by_text("Einstein").first)  # search by visible text
    print(page.markdown(main_content=True)[:200])  # page as Markdown
    return quotes


if __name__ == "__main__":
    main()
