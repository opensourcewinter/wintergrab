"""Scrape without writing selectors: auto-extraction, learning by example, structured data.

python examples/09_zero_selector.py
"""

import json

import wintergrab as wg

URL = "https://books.toscrape.com/"


def main(url: str = URL, example_title: str = "A Light in the Attic", example_price: str = "£51.77") -> dict:
    page = wg.get(url)

    # 1. Let wintergrab find the main list of records and name the fields.
    records = page.auto_extract()
    print(f"auto_extract: {len(records)} records, first: {records[0] if records else None}")

    # 2. Or point at values you can see and let it write the selectors.
    schema = page.learn({"title": example_title, "price": example_price})
    print("learned:", schema)
    rows = schema.extract(page)

    # The schema is reusable on every page with the same template...
    next_url = page.next_page()
    if next_url:
        rows += schema.extract(wg.get(next_url))
    # ...and can be saved for later (or for `wintergrab crawl --schema`).
    saved = json.dumps(schema.to_dict())

    # 3. Pages often publish machine-readable data too.
    meta = page.structured_data()["meta"]
    print(f"{len(rows)} rows via the learned schema; page title: {meta.get('title')}")
    return {"records": records, "rows": rows, "schema": saved, "next": next_url}


if __name__ == "__main__":
    main()
