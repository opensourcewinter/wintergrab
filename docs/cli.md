# Command line

Installing wintergrab adds a `wintergrab` command (also runnable as
`python -m wintergrab`). Logs and status lines go to stderr and data goes to
stdout, so you can pipe the output anywhere.

```
wintergrab [-v | -q] [--version] {get,crawl,shell} ...
```

`-v` shows debug logs; `-q` keeps only warnings.

## `wintergrab get`: fetch and extract

```bash
wintergrab get URL [URL ...] [options]
```

What gets printed depends on the options:

| You pass | Output (default format) |
|---|---|
| nothing | the page as Markdown |
| `--css` / `--xpath` | each match on its own line (text for elements, the value for `::text`/`::attr()`) |
| `--each SEL` and/or `--field NAME=SEL` | one JSON record per matched element (JSON Lines) |

Change the format with `-f md|text|html|json|jsonl|csv`, or let `-o FILE`
pick it from the extension (`.md`, `.txt`, `.html`, `.json`, `.jsonl`,
`.csv`).

```bash
# Read a page as Markdown (main content only)
wintergrab get https://quotes.toscrape.com --main-content

# Pull values
wintergrab get https://quotes.toscrape.com --css ".quote .author::text"
wintergrab get https://quotes.toscrape.com --xpath "//a[@class='tag']/@href"

# Structured records -> CSV
wintergrab get https://books.toscrape.com --each article.product_pod \
    --field title="h3 a::attr(title)" --field price=.price_color::text -o books.csv

# One record per page, several pages at once (fetched concurrently)
wintergrab get https://books.toscrape.com/catalogue/page-{1,2,3}.html \
    --field first_title="h3 a::attr(title)"

# Save the raw HTML
wintergrab get https://example.com -o page.html

# JavaScript page, in a headless browser
wintergrab get https://quotes.toscrape.com/js/ --browser --wait-for .quote --css ".quote .text::text"
wintergrab get https://example.com --browser --scroll --screenshot page.png
```

Options:

| Option | |
|---|---|
| `--css SEL`, `--xpath XPATH` | Print what matches (repeatable). |
| `--each SEL`, `--field NAME=SEL` | Build records (repeatable). |
| `-f/--format`, `-o/--output FILE` | Output format and destination. |
| `--main-content` | Markdown/text of the main content only. |
| `--adaptive` | Use [adaptive selectors](adaptive-selectors.md). |
| `--impersonate BROWSER` | `chrome` (default), `firefox`, `safari`, `edge`, `none`. |
| `-H/--header 'Name: value'`, `--cookie name=value` | Extra headers/cookies (repeatable). |
| `--timeout SEC`, `--retries N`, `--insecure` | Network behaviour. |
| `--proxy URL` (repeatable), `--proxy-file FILE` | Proxies (several = rotation). |
| `-b/--browser` | Use a headless browser. |
| `--wait-for SEL`, `--wait SEC`, `--scroll`, `--headful`, `--screenshot FILE` | Browser options. |
| `--block-trackers` | (browser) Also block ads, analytics and tracker requests. |
| `--public-only` | Refuse private, loopback and cloud-metadata addresses ([SSRF protection](fetching.md#network-policy-ssrf-protection)). |

The exit code is 1 if any URL failed or returned a 4xx/5xx status.

## `wintergrab crawl`: run a spider

```bash
wintergrab crawl SPIDER.py[:ClassName] [options]   # a Spider subclass from a file
wintergrab crawl URL [options]                     # a quick crawl without code
```

Items are streamed as JSON Lines to stdout unless you pass `-o FILE`
(`.jsonl`, `.json` or `.csv`). A summary goes to stderr at the end, with a
short diagnosis of the most common failures (the full
[failure report](observability.md#failure-reports) with `-v`).

```bash
wintergrab crawl examples/05_quotes_spider.py -o quotes.jsonl
wintergrab crawl spiders.py:BooksSpider -s max_pages=50 -s 'start_urls=["https://books.toscrape.com/"]'

# Resumable: Ctrl+C pauses, the same command resumes
wintergrab crawl my_spider.py -o items.jsonl --crawl-dir .crawl/mine
wintergrab crawl my_spider.py -o items.jsonl --crawl-dir .crawl/mine --fresh   # start over
```

Options for any crawl:

| Option | Spider setting |
|---|---|
| `-o/--output FILE` | `output` |
| `--crawl-dir DIR`, `--fresh` | `crawl_dir`, `run(resume=False)` |
| `--concurrency N`, `--per-domain N`, `--delay SEC` | `concurrency`, `concurrency_per_domain`, `download_delay` |
| `--no-autothrottle` | `autothrottle = False` |
| `--max-pages N`, `--max-items N`, `--max-depth N` | limits |
| `--max-requests N`, `--max-bytes N`, `--max-runtime SEC` | budgets (see [spiders.md](spiders.md#budgets)) |
| `--order bfs\|dfs` | `crawl_order` |
| `--events FILE` | `event_log` (structured events as JSON lines) |
| `--retry-failed` | `retry_dead_letters = True`: fetch only what an earlier run gave up on |
| `--no-robots` | `obey_robots_txt = False` |
| `--proxy URL`, `--proxy-file FILE` | `proxies` |
| `-b/--browser` | `use_browser = True` |
| `--public-only` | `network_policy = "public"` |
| `--normalize-urls` | `url_normalizer = True` |
| `--block-trackers` | `resource_filter = True` |
| `-s/--set NAME=VALUE` | any attribute; values are parsed as JSON when possible (`-s retries=5`, `-s 'allowed_statuses=[404]'`) |

### Quick crawls from a URL

Without a spider file, `crawl URL` follows links on the same domain and
emits one record per page (`url`, `status`, `title`) unless told otherwise.
Links to images, media, archives and crawler traps are skipped
(`-s url_rules=null` follows them anyway):

```bash
# Follow pagination and product links, extract fields from product pages
wintergrab crawl https://books.toscrape.com \
    --follow "li.next a" --follow "article.product_pod h3 a" \
    --each ".product_main" --field title=h1::text --field price=.price_color::text \
    --max-pages 100 -o books.jsonl

# Only follow URLs matching a pattern
wintergrab crawl https://example.com --allow "/blog/" --deny "\?replytocom=" --max-depth 3
```

| Option | |
|---|---|
| `--follow SEL` | Links (or containers of links) to follow. Default: every link on the same domain. |
| `--allow REGEX`, `--deny REGEX` | Filter followed URLs. |
| `--any-domain` | Allow leaving the start domain. |
| `--each SEL`, `--field NAME=SEL` | What to extract from each page. |

## `wintergrab shell`: explore interactively

```bash
wintergrab shell https://quotes.toscrape.com
```

Opens Python (IPython if installed) with `page` already fetched, plus `wg`,
`get`, `render`, `parse` and `Field`, so you can try selectors quickly.
Add `--browser` to render the page first.
