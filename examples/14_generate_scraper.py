"""Generate a scraper for a goal, and keep it only when it passes: selectors learned from the
site's record pages, then linted, tested on those pages, run on a sample crawl, checked with
the quality system and benchmarked against wintergrab's own extraction.

    python examples/14_generate_scraper.py
    wintergrab generate "books with title, price and rating" --site books.toscrape.com -o scrapers/books
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from wintergrab.goals import GenerationResult, GoalPlan, generate_scraper


def main(site: str = "https://books.toscrape.com/", directory: str | None = None) -> GenerationResult:
    directory = directory or tempfile.mkdtemp(prefix="scraper-")
    result = generate_scraper(
        "books with title, price, availability and rating",
        directory,
        sites=[site],
        sample=15,  # pages to survey the site with
        train=4,  # record pages to learn the selectors from
        test=6,  # record pages beyond those to test the scraper on
    )
    print(result.describe())
    for name, learned in result.generated.fields.items() if result.generated else ():
        print(f"  {name}: {learned.selector or learned.status}")
    if result.accepted:  # a plan with a schema of its own: run it like any goal's plan
        records = GoalPlan.load(Path(directory) / "plan.json").run(max_pages=5, log_level="WARNING").records
        print(f"{len(records)} record(s); the first: {records[0] if records else None}")
    return result


if __name__ == "__main__":
    main()
