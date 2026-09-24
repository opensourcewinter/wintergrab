"""A classic spider: follow pagination and author pages, save JSON Lines.

python examples/05_quotes_spider.py
# or through the CLI:
wintergrab crawl examples/05_quotes_spider.py -o quotes.jsonl
"""

from wintergrab import Spider


class QuotesSpider(Spider):
    name = "quotes"
    start_urls = ["https://quotes.toscrape.com/"]
    allowed_domains = ["quotes.toscrape.com"]
    concurrency = 8

    async def parse(self, response):
        for quote in response.css(".quote"):
            author_link = quote.css("a[href*='/author/']")
            yield {
                "text": quote.css(".text::text").get(),
                "author": quote.css(".author::text").get(),
                "tags": quote.css(".tag::text").getall(),
            }
            # Author pages repeat a lot - duplicates are filtered automatically.
            if author_link:
                yield response.follow(author_link[0], callback=self.parse_author)

        next_page = response.css("li.next a")
        if next_page:
            yield response.follow(next_page[0])  # callback defaults to parse()

    def parse_author(self, response):
        yield {
            "author": response.css(".author-title::text").get("").strip(),
            "born": response.css(".author-born-date::text").get(),
        }


if __name__ == "__main__":
    result = QuotesSpider(output="quotes.jsonl").run()
    print(result)
    print(result.stats)
