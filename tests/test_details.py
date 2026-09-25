"""extract_details: one flat, typed record from any detail page."""

from __future__ import annotations

import json

import wintergrab as wg
from wintergrab.parser.details import extract_details


def test_books_toscrape_product_page(site) -> None:
    # The local copy has the structure of a real books.toscrape.com product page.
    details = wg.get(site.url + "/books/catalogue/book-3/index.html").extract_details()
    assert details == {
        "title": "Book number 3",
        "price": 14.5,
        "currency": "GBP",
        "availability": "In stock (6 available)",
        "in_stock": True,
        "stock": 6,
        "rating": 4.0,
        "upc": "upc0003",
        "product_type": "Books",
        "price_excl_tax": 14.5,
        "price_incl_tax": 14.5,
        "tax": 0.0,
        "number_of_reviews": 0,
        "category": "Poetry",
        "breadcrumbs": "Home > Books > Poetry",
        "description": "Book number 3 is a wonderful read. It was written long ago and still holds up today.",
        "image": site.url + "/books/media/cache/3.jpg",
        "url": site.url + "/books/catalogue/book-3/index.html",
    }
    raw = wg.get(site.url + "/books/catalogue/book-3/index.html").extract_details(clean=False)
    assert (raw["price"], raw["rating"]) == ("£14.50", "star-rating Four")


JSON_LD_PRODUCT = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Aurora Desk Lamp",
    "sku": "AUR-1",
    "gtin13": "0123456789012",
    "brand": {"@type": "Brand", "name": "Lumen"},
    "image": ["https://cdn.lumen.example/aurora.jpg"],
    "description": "A warm, dimmable desk lamp for late nights.",
    "offers": {
        "@type": "Offer",
        "price": "89.00",
        "priceCurrency": "EUR",
        "availability": "https://schema.org/InStock",
        "seller": {"@type": "Organization", "name": "Lumen Store"},
    },
    "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.6", "reviewCount": "128"},
}
BREADCRUMBS = {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    "itemListElement": [
        {"@type": "ListItem", "position": 2, "name": "Lighting"},
        {"@type": "ListItem", "position": 1, "name": "Home"},
        {"@type": "ListItem", "position": 3, "name": "Desk lamps"},
    ],
}


def test_schema_org_product_with_specs() -> None:
    html = f"""<html><head><title>Aurora Desk Lamp – Lumen</title>
    <script type="application/ld+json">{json.dumps(JSON_LD_PRODUCT)}</script>
    <script type="application/ld+json">{json.dumps(BREADCRUMBS)}</script>
    <link rel="canonical" href="https://lumen.example/p/aurora"></head>
    <body><header><h1>Lumen</h1></header><main><h1>Aurora Desk Lamp</h1>
    <p class="price"><del>€109,00</del> €89,00</p>
    <dl class="specs"><dt>Wattage</dt><dd>8 W</dd><dt>Colour temperature</dt><dd>2700 K</dd></dl>
    </main></body></html>"""
    assert extract_details(html, "https://lumen.example/p/aurora?ref=x") == {
        "title": "Aurora Desk Lamp",
        "price": 89.0,
        "currency": "EUR",
        "availability": "In stock",
        "in_stock": True,
        "seller": "Lumen Store",
        "rating": 4.6,
        "review_count": 128,
        "sku": "AUR-1",
        "gtin13": "0123456789012",
        "brand": "Lumen",
        "wattage": "8 W",
        "colour_temperature": "2700 K",
        "category": "Desk lamps",
        "breadcrumbs": "Home > Lighting > Desk lamps",
        "description": "A warm, dimmable desk lamp for late nights.",
        "image": "https://cdn.lumen.example/aurora.jpg",
        "url": "https://lumen.example/p/aurora",
    }


def test_visible_page_without_structured_data() -> None:
    html = """<html><body><nav><a href="/">Shop</a></nav><main>
    <div class="product"><img class="logo" src="/logo.png"><img src="/img/trail.jpg" alt="Trail Shoe">
      <h1>Trail Shoe</h1>
      <p class="price"><s class="price-old">$120.00</s> <span class="price-now">$89.99</span></p>
      <p class="stock">Only 3 left in stock</p>
      <div class="rating" aria-label="4.5 out of 5 stars"></div>
    </div>
    <ul class="details"><li>Brand: Stride</li><li>Weight: 280 g</li><li>Drop: 8 mm</li></ul>
    <h2>Description</h2><p>Grippy, light and made for long runs on rough ground.</p>
    </main></body></html>"""
    details = extract_details(html, "https://shoes.example/trail")
    assert details == {
        "title": "Trail Shoe",
        "price": 89.99,  # not the struck-out $120.00
        "currency": "$",
        "availability": "Only 3 left in stock",
        "in_stock": True,
        "stock": 3,
        "rating": 4.5,
        "brand": "Stride",
        "weight": "280 g",
        "drop": "8 mm",
        "description": "Grippy, light and made for long runs on rough ground.",
        "image": "https://shoes.example/img/trail.jpg",  # not the logo
        "url": "https://shoes.example/trail",
    }


def test_microdata_product_out_of_stock() -> None:
    html = """<html><body><div itemscope itemtype="https://schema.org/Product">
    <h1 itemprop="name">Pocket Radio</h1>
    <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
      <meta itemprop="priceCurrency" content="GBP"><span itemprop="price">£24.99</span>
      <link itemprop="availability" href="https://schema.org/OutOfStock"></div>
    <p itemprop="description">A tiny radio for big sound anywhere you go.</p></div></body></html>"""
    details = extract_details(html, "https://radio.example/pocket")
    assert details["title"] == "Pocket Radio"
    assert (details["price"], details["currency"]) == (24.99, "GBP")
    assert (details["availability"], details["in_stock"]) == ("Out of stock", False)
    assert details["description"] == "A tiny radio for big sound anywhere you go."


def test_news_article() -> None:
    article = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": "Scrapers get smarter",
        "author": [{"@type": "Person", "name": "Ada"}, {"@type": "Person", "name": "Linus"}],
        "datePublished": "2026-09-01T10:00:00Z",
        "publisher": {"@type": "Organization", "name": "Daily Byte"},
        "image": {"@type": "ImageObject", "url": "/img/lead.jpg"},
        "articleSection": "Tech",
        "keywords": ["scraping", "python"],
    }
    html = f"""<html><head><script type="application/ld+json">{json.dumps(article)}</script>
    <meta name="description" content="How extraction learned to read pages."></head>
    <body><article><h1>Scrapers get smarter</h1><p>Body text.</p></article></body></html>"""
    assert extract_details(html, "https://news.example/a/1") == {
        "title": "Scrapers get smarter",
        "author": "Ada, Linus",
        "date_published": "2026-09-01T10:00:00Z",
        "publisher": "Daily Byte",
        "article_section": "Tech",
        "keywords": "scraping, python",
        "description": "How extraction learned to read pages.",
        "image": "https://news.example/img/lead.jpg",
        "url": "https://news.example/a/1",
    }


def test_empty_page_gives_just_the_url() -> None:
    assert extract_details("<html><body><p>hi</p></body></html>", "https://x.example/") == {"url": "https://x.example/"}
