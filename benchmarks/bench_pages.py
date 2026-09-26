"""Speed of page analysis: extraction, page classification and technology detection (no network).

    .venv/bin/python benchmarks/bench_pages.py [--repeat 5] [--rounds 200]

Every measurement builds a fresh Response per page, so HTML parsing is
included, as in a crawl; the "parse only" row is that baseline. Each row is
the median of ``--repeat`` runs of ``--rounds`` pages.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import time
from collections.abc import Callable
from typing import Any

from wintergrab import __version__
from wintergrab.data import Schema
from wintergrab.extraction import Extractor
from wintergrab.fetchers.response import Headers, Response
from wintergrab.intel import classify_page, classify_url, detect_technologies

SCHEMA = Schema.from_dict(
    {
        "name": "product",
        "fields": {
            "name": {"type": "string", "required": True},
            "brand": "string",
            "price": {"type": "money", "required": True},
            "list_price": "money",
            "currency": "currency",
            "availability": "availability",
            "rating": {"type": "rating", "best": 5},
            "review_count": "integer",
            "sku": "string",
            "weight": {"type": "quantity", "unit": "kg"},
            "image": "url",
            "description": "text",
            "url": "url",
        },
    }
)

_LD = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Phone X",
    "brand": {"@type": "Brand", "name": "Acme"},
    "sku": "PX-1",
    "offers": {
        "@type": "Offer",
        "price": "299.99",
        "priceCurrency": "USD",
        "availability": "https://schema.org/InStock",
    },
    "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.5", "reviewCount": "120"},
}
_CHROME = (
    '<header><nav><a href="/">Home</a> <a href="/phones">Phones</a> <a href="/cart">Cart</a></nav></header>'
    + "".join(f'<a href="/help/{i}">Help {i}</a> ' for i in range(30))
    + "<footer><p>© Acme Shop</p></footer>"
)
_BODY = (
    '<h1>Phone X</h1><div class="product-price"><span class="price">$299.99</span>'
    '<del class="price old-price">$349.99</del></div><div class="stock">In stock</div>'
    '<div class="rating" aria-label="4.5 out of 5 stars"></div><a class="reviews">120 reviews</a>'
    "<table><tr><th>Weight</th><td>450 g</td></tr><tr><th>Colour</th><td>Black</td></tr></table>"
    '<div class="description">A great phone with a big screen and a long battery life.</div><button>Add to cart</button>'
)
_HEAD = (
    '<title>Phone X | Acme Shop</title><meta property="og:title" content="Phone X">'
    '<meta name="description" content="A great phone"><link rel="canonical" href="https://shop.example/p/phone-x">'
    '<meta name="generator" content="WordPress 6.4.2">'
    '<link rel="stylesheet" href="/wp-content/plugins/woocommerce/assets/css/woocommerce.css?ver=8.4.0">'
    '<script src="/wp-includes/js/jquery/jquery.min.js?ver=3.7.1"></script>'
    '<script async src="https://www.googletagmanager.com/gtm.js?id=GTM-ABC123"></script>'
)
_HEADERS = Headers(
    [("content-type", "text/html; charset=utf-8"), ("server", "cloudflare"), ("cf-ray", "8a1b2c3d4e5f-AMS"),
     ("x-powered-by", "PHP/8.2.1"), ("set-cookie", "__cf_bm=abc; path=/")]
)  # fmt: skip


def _html(head: str, body: str) -> bytes:
    return f"<!doctype html><html><head>{head}</head><body>{body}{_CHROME}</body></html>".encode()


def _card(i: int) -> str:
    return (
        f'<li class="product-card"><a href="/p/item-{i}"><img src="/img/{i}.jpg" alt="">'
        f'<span class="name">Item {i}</span></a><span class="price">${10 + i}.99</span></li>'
    )


PAGES: dict[str, tuple[str, bytes]] = {
    "product page, JSON-LD": (
        "https://shop.example/p/phone-x",
        _html(_HEAD + f'<script type="application/ld+json">{json.dumps(_LD)}</script>', _BODY * 20),
    ),
    "product page, no structured data": ("https://shop.example/p/phone-x", _html(_HEAD, _BODY * 20)),
    "category page, 60 cards": (
        "https://shop.example/category/phones",
        _html("<title>Phones</title>", '<ul class="grid">' + "".join(_card(i) for i in range(60)) + "</ul>"),
    ),
    "large product page": ("https://shop.example/p/phone-x", _html(_HEAD, _BODY + "<p>" + "lorem ipsum " * 40000)),
}


def per_page(fn: Callable[[Response], Any], url: str, body: bytes, rounds: int, repeat: int) -> float:
    """Median seconds per page."""
    times = []
    for _ in range(repeat):
        start = time.perf_counter()
        for _ in range(rounds):
            fn(Response(url, headers=_HEADERS, body=body))
        times.append((time.perf_counter() - start) / rounds)
    return statistics.median(times)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=200)
    args = parser.parse_args()
    extractor = Extractor(SCHEMA)
    operations: list[tuple[str, Callable[[Response], Any]]] = [
        ("parse only", lambda r: r.selector),
        ("Extractor.extract (13 fields)", extractor.extract),
        ("classify_page", classify_page),
        ("detect_technologies", detect_technologies),
    ]
    print(f"wintergrab {__version__}, Python {platform.python_version()}, {platform.machine()}, "
          f"median of {args.repeat} runs of {args.rounds} pages\n")  # fmt: skip
    print("| Page | " + " | ".join(name for name, _ in operations) + " |")
    print("|---|" + "---:|" * len(operations))
    for label, (url, body) in PAGES.items():
        rounds = max(1, args.rounds // 20) if len(body) > 100_000 else args.rounds
        cells = [f"{per_page(fn, url, body, rounds, args.repeat) * 1000:.2f} ms" for _, fn in operations]
        print(f"| {label} ({len(body) // 1000} KB) | " + " | ".join(cells) + " |")
    urls = [f"https://shop.example/{kind}/{i}" for i in range(1000) for kind in ("p", "blog", "search", "category")]
    start = time.perf_counter()
    for url in urls:
        classify_url(url)
    print(f"\nclassify_url: {(time.perf_counter() - start) / len(urls) * 1e6:.1f} µs per URL")


if __name__ == "__main__":
    main()
