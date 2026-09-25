"""From a listing URL to complete, typed records in one call - no selectors.

python examples/11_scrape_in_one_call.py
"""

import wintergrab as wg

URL = "https://books.toscrape.com/"


def main(url: str = URL, pages: int | None = 2, out: str = "books.csv") -> list[dict]:
    # Listing pages up to `pages` (None: all), each book's own page merged in.
    books = wg.scrape(url, pages=pages, deep=True, output=out)
    print(f"{len(books)} books saved to {out}")

    for key, value in books[0].items():
        print(f"  {key:18} {value!r}")

    # Values are typed, so analysis needs no string cleaning.
    in_stock = [b for b in books if b.get("in_stock")]
    cheapest = min(books, key=lambda b: b["price"])
    print(f"{len(in_stock)} in stock; cheapest: {cheapest['title']} ({cheapest['price']} {cheapest['currency']})")
    return books


if __name__ == "__main__":
    main()
