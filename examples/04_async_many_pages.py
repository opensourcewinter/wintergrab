"""Fetch many pages at once with the async fetcher.

python examples/04_async_many_pages.py
"""

import asyncio

import wintergrab as wg

BASE = "https://books.toscrape.com/catalogue/page-{}.html"


async def main(urls: list[str] | None = None) -> list[str]:
    urls = urls or [BASE.format(n) for n in range(1, 11)]
    titles: list[str] = []
    async with wg.AsyncFetcher(retries=3) as fetcher:
        # get_many keeps input order; failures come back as FetchError objects.
        pages = await fetcher.get_many(urls, concurrency=5)
        for url, page in zip(urls, pages, strict=True):
            if isinstance(page, wg.FetchError):
                print("failed:", url, page)
                continue
            titles.extend(page.css("article.product_pod h3 a::attr(title)").getall())

        # Or handle each page the moment it arrives:
        async for page in fetcher.iter_many(urls[:3], concurrency=3):
            print("got", page.url if isinstance(page, wg.Response) else page)
    print(f"{len(titles)} titles, e.g. {titles[:2]}")
    return titles


if __name__ == "__main__":
    asyncio.run(main())
