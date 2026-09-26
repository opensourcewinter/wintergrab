# Command line

Installing wintergrab adds a `wintergrab` command (also runnable as
`python -m wintergrab`). Logs and status lines go to stderr and data goes to
stdout, so you can pipe the output anywhere.

```
wintergrab [-v | -q] [--version] {get,crawl,data,shell,doctor} ...
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
| `--extract SCHEMA` | a typed record per page with confidence, no selectors needed ([extraction](extraction.md)); `--all` for every record of a listing |

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

# Typed records from a data schema, with where each value came from
wintergrab get https://shop.example/p/1 --extract product.schema.json --explain

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
| `--extract SCHEMA`, `--all`, `--container SEL`, `--explain`, `--provenance` | Typed extraction with a [data schema](extraction.md). |
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
| `--profile FILE` | `profile`: save the [site profile](intelligence.md#site-profiles) and [topology](intelligence.md#site-topology) as JSON |
| `--retry-failed` | `retry_dead_letters = True`: fetch only what an earlier run gave up on |
| `--no-robots` | `obey_robots_txt = False` |
| `--proxy URL`, `--proxy-file FILE` | `proxies` |
| `-b/--browser` | `use_browser = True` |
| `--auto-browser`, `--fetch-stats FILE`, `--render-if-missing SEL` | `adaptive_fetch`: HTTP first, a browser for the pages that [need one](spiders.md#http-first-a-browser-when-needed) |
| `--public-only` | `network_policy = "public"` |
| `--normalize-urls` | `url_normalizer = True` |
| `--block-trackers` | `resource_filter = True` |
| `--extract SCHEMA` | (URL mode) extract typed records with [the extractor](extraction.md) (`--all`, `--container`, `--provenance`) |
| `--pipeline FILE` | appends a [data pipeline](data.md#pipelines-as-configuration) to `pipelines` (`--allow-imports` if it names Python functions) |
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

## `wintergrab data`: check and clean datasets

```bash
wintergrab data infer INPUT [-o SCHEMA] [--explain]         # guess a schema from records
wintergrab data validate SCHEMA INPUT [-o VALID] [--rejects FILE]
wintergrab data run PIPELINE INPUT [-o OUTPUT] [--allow-imports]
wintergrab data quality INPUT [--schema SCHEMA] [--baseline REPORT] [--save REPORT] [--json]
wintergrab data entities INPUT --field NAME [--kind KIND] [--attribute ATTR=FIELD] [-o ENTITIES]
                         [--review-output FILE] [--annotate FILE] [--merge P] [--review P]
wintergrab data commit DIR INPUT [--key FIELD] [-m MESSAGE] [--force]   # save the next version
wintergrab data log DIR                                                  # list the versions
wintergrab data diff OLD NEW [--key FIELD] [--ignore FIELD] [-o CHANGES] [--json] [--exit-code]
```

Inputs are JSON Lines, JSON or CSV files (`-` reads JSON Lines from stdin);
outputs are chosen by extension, as for `crawl -o`. `validate` normalizes
records with the schema first (`--no-normalize` to skip; `--country`,
`--currency`, `--dayfirst` help read local formats), prints the most common
issues, and exits with status 1 if any record is invalid. `quality` prints
completeness, validity, consistency and anomalies per field; with
`--baseline` it compares with an earlier report and exits with status 1 when
quality collapsed. `entities` groups the names in a field into companies,
brands, products, people or places, with evidence, and prints the pairs it
did not dare merge. `commit` saves a dataset as the next version in a
versions directory and prints what changed; `diff` compares two files or two
versions (`DIR@v2`, `DIR@previous`, `DIR@latest`), prints the counts and the
fields that changed, writes the details with `-o`, and with `--exit-code`
exits with status 1 when they differ. See [data.md](data.md) and
[entities.md](entities.md).

## `wintergrab inspect`: profile a website

```bash
wintergrab inspect URL [--pages N] [--browser] [--no-robots] [--no-sitemaps] [-o PROFILE.json] [--json]
                       [--depth N] [--show N]
```

Reads the site's robots.txt and sitemaps, visits `--pages` pages (30): the
start page and a sample spread across the sitemaps, obeying robots.txt. It
prints the site's technologies, languages, page types, templates, structured
data, links, API endpoints, sitemaps and crawlability, then its sections as
a tree (`--depth` levels, 2), its navigation, feeds, paginated listings,
dead ends and duplicate routes. `--show` sets how many entries each list
shows (8). `--browser` renders the pages and records their XHR/fetch calls.
See [intelligence.md](intelligence.md#site-profiles).

## `wintergrab goal`: say what data you want

```bash
wintergrab goal "REQUEST" [--site URL] [--sample N] [--plan-only] [--save-plan FILE] [--explain] [--json]
                          [-y] [--confirm-over N] [--max-pages N] [--browser] [-o FILE]
wintergrab goal --plan FILE [-y] [-o FILE]
```

Reads the request ("Find all laptops under $1000 on shop.example with name,
price and rating"), surveys each site (robots.txt, sitemaps, `--sample`
pages), prints how it understood the request and the plan with its
estimates, and collects the records: JSON Lines on stdout, or `-o FILE`.
Plans of more than `--confirm-over` requests (200) ask first, or need `--yes`
without a terminal. `--plan-only` shows the plan and stops; `--save-plan`
keeps it as JSON to edit and run later with `--plan`; `--explain` says what
each estimate rests on. See [goals.md](goals.md).

## `wintergrab history`: what changed between crawls

```bash
wintergrab crawl URL --history FILE [--history-html] [--skip-fresh]   # record, and report changes
wintergrab history FILE [--name SPIDER]          # the runs, and what the last one changed
wintergrab history FILE --compare OLD NEW        # two runs, by id
wintergrab history FILE --url URL                # one page over time, and how often it changes
wintergrab history FILE --due                    # pages that have probably changed by now
```

`--json` prints JSON; `--show N` sets how many pages are listed. See
[history.md](history.md).

## `wintergrab shell`: explore interactively

```bash
wintergrab shell https://quotes.toscrape.com
```

Opens Python (IPython if installed) with `page` already fetched, plus `wg`,
`get`, `render`, `parse` and `Field`, so you can try selectors quickly.
Add `--browser` to render the page first.
