# Examples

Each script runs on its own (`python examples/<name>.py`). They target
[quotes.toscrape.com](https://quotes.toscrape.com) and
[books.toscrape.com](https://books.toscrape.com), public sandboxes built for
practising web scraping.

| Script | Shows |
|---|---|
| `01_quickstart.py` | `wg.get`, CSS/XPath, `::text`/`::attr()`, looping, links, Markdown |
| `02_extract_to_csv.py` | extraction schemas with `Field`, `extract_all`, saving CSV |
| `03_adaptive_selectors.py` | selectors that survive a redesign, `find_similar` (offline) |
| `04_async_many_pages.py` | `AsyncFetcher.get_many` / `iter_many` |
| `05_quotes_spider.py` | a spider with pagination, multiple callbacks, JSON Lines output |
| `06_books_resumable.py` | pause/resume, AutoThrottle, proxies from env, item pipeline |
| `07_browser_rendering.py` | JavaScript pages with `BrowserFetcher`, `wait_for`, `page_action` |
| `08_sessions_and_fallback.py` | several sessions in one spider, escalating blocked pages to a browser |
| `09_zero_selector.py` | `auto_extract`, learning a schema from examples, `next_page`, structured data |
| `10_big_crawl.py` | sitemaps, disk frontier, revalidating cache + offline replay, SQLite upserts |
| `11_scrape_in_one_call.py` | `wg.scrape(..., deep=True)`: every page, each item's own page merged, typed values, Excel-ready CSV |

The spiders also run from the command line:

```bash
wintergrab crawl examples/05_quotes_spider.py -o quotes.jsonl
wintergrab crawl examples/06_books_resumable.py -s max_pages=20
```
