"""Describe the data you want as a schema, then save it to CSV.

python examples/02_extract_to_csv.py
"""

import wintergrab as wg
from wintergrab import Field
from wintergrab.spider import write_items

URL = "https://books.toscrape.com/"


def price(text: str) -> float:
    return float(text.lstrip("£$€"))


BOOK = {
    # A plain string: first match. Elements give their text.
    "title": "h3 a::attr(title)",
    # Field: fallbacks, regexes, transforms and defaults.
    "price": Field(".price_color::text", ".price::text", transform=price),
    "rating": Field("p.star-rating", attr="class", regex=r"star-rating (\w+)"),
    "in_stock": Field(".availability", transform=lambda t: "In stock" in t, default=False),
    "url": "h3 a::attr(href)",
}


def main(url: str = URL, out: str = "books.csv") -> list[dict]:
    page = wg.get(url)
    books = page.extract_all("article.product_pod", BOOK)
    for book in books:
        book["url"] = page.urljoin(book["url"])  # make links absolute
    write_items(out, books)
    print(f"saved {len(books)} books to {out}; first: {books[0]}")
    return books


if __name__ == "__main__":
    main()
