"""Say what data you want: wintergrab reads the request, surveys the site, plans the
crawl with an estimate of its cost, and collects typed records.

    python examples/12_goal.py
    wintergrab goal "books rated 4 stars or more on books.toscrape.com, at most 20" --plan-only
"""

from __future__ import annotations

from typing import Any

from wintergrab.goals import parse_goal, plan_goal


def main(site: str = "https://books.toscrape.com/", limit: int = 20) -> list[dict[str, Any]]:
    goal = parse_goal(f"books with name, price and rating on {site}, at most {limit}")
    print("Understood:", goal.describe(), sep="\n")
    plan = plan_goal(goal, sample=10)  # robots.txt, sitemaps and a sample of pages
    print(plan.describe(goal=False))
    result = plan.run(keep_items=True, log_level="WARNING")
    print(f"{len(result.records)} record(s) from {result.counts['pages']} page(s)")
    return result.records


if __name__ == "__main__":
    main()
