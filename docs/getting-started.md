# Getting started

This guide takes you from install to a resumable crawl in about ten minutes.
The examples use [quotes.toscrape.com](https://quotes.toscrape.com) and
[books.toscrape.com](https://books.toscrape.com), sandboxes made for practising
scraping.

## 1. Install

```bash
pip install wintergrab
```

For JavaScript-heavy sites, also install the browser extra (once):

```bash
pip install "wintergrab[browser]"
playwright install chromium
```

## 2. Fetch a page

```python
import wintergrab as wg

page = wg.get("https://quotes.toscrape.com/")
print(page.status)   # 200
print(page.title)    # 'Quotes to Scrape'
```

`wg.get` sends a request that looks like Chrome, down to the TLS handshake.
Many sites that block `requests` or `urllib` let it through. It retries
network errors and temporary failures (429, 5xx) with exponential backoff and
honours `Retry-After`.

The result is a `Response`. It has the usual `status`, `headers`, `text`,
`body` and `json()`, and you can query it directly.

## 3. Select things

```python
page.css(".quote .text::text").get()          # first match, as a string
page.css(".quote .author::text").getall()     # every match
page.css("li.next a::attr(href)").get()       # an attribute
page.xpath("//small[@class='author']/text()").getall()   # XPath works too
```

- `::text` gives the text directly inside an element and `::attr(name)` gives
  an attribute. This is the same syntax as Scrapy and parsel.
- `.get()` returns the first result (or `None`) and `.getall()` returns a list.
- Without `::text`, you get **elements** you can keep querying:

```python
for quote in page.css(".quote"):
    print(quote.css(".text::text").get())
    print(quote.css(".tag").texts)                # text of each tag element
    print(quote.css("a").attr("href"))            # attribute of the first link
```

Elements have `.text` (all visible text, whitespace cleaned), `.attrib`,
`.html`, `.parent`, `.children`, `.next`, `.previous` and more; see
[parsing.md](parsing.md).

## 4. Extract records in one go

```python
from wintergrab import Field

quotes = page.extract_all(".quote", {
    "text": ".text::text",
    "author": ".author::text",
    "tags": [".tag::text"],                                   # list = all matches
    "author_url": Field("a[href*=author]", attr="href", transform=page.urljoin),
})
```

Save them with `wintergrab.spider.write_items("quotes.csv", quotes)`. It also
writes `.json` and `.jsonl`.

## 5. Your first spider

A spider starts from some URLs, follows links and yields items:

```python
from wintergrab import Spider

class QuotesSpider(Spider):
    start_urls = ["https://quotes.toscrape.com/"]

    def parse(self, response):
        for quote in response.css(".quote"):
            yield {
                "text": quote.css(".text::text").get(),
                "author": quote.css(".author::text").get(),
            }
        next_page = response.css("li.next a")
        if next_page:
            yield response.follow(next_page[0])

result = QuotesSpider(output="quotes.jsonl").run()
print(result)        # <CrawlResult finished: 10 pages, 100 items, 100 kept>
```

Everything a callback yields is either an **item** (a dict, dataclass,
pydantic model…) or a **`Request`** to crawl next. Callbacks can be plain
functions, generators or `async def`, whichever you prefer.

Out of the box the spider fetches up to 16 pages at a time (4 per domain),
filters duplicate URLs, obeys robots.txt, and slows down automatically if the
site starts returning 429/503 errors.

## 6. Make it resumable

Add a `crawl_dir`:

```python
QuotesSpider(output="quotes.jsonl", crawl_dir=".crawl/quotes").run()
```

Press **Ctrl+C** while it runs. The spider finishes the requests in flight,
saves its queue and exits. Run the same code again and it continues where it
stopped, appending to the same output file. Press Ctrl+C twice to quit
immediately.

## 7. Or skip the code entirely

```bash
wintergrab get https://quotes.toscrape.com --css ".quote .text::text"

wintergrab get https://quotes.toscrape.com --each .quote \
    --field text=.text::text --field author=.author::text -o quotes.csv

wintergrab crawl https://quotes.toscrape.com --follow "li.next a" \
    --each .quote --field text=.text::text -o quotes.jsonl
```

## Where next

- Pages built with JavaScript: [fetching.md#browser](fetching.md#browser-fetching)
- Sites that block you: [anti-blocking.md](anti-blocking.md)
- Selectors that survive redesigns: [adaptive-selectors.md](adaptive-selectors.md)
- Every spider setting: [spiders.md](spiders.md#settings-reference)
