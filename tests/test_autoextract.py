from __future__ import annotations

from types import SimpleNamespace

import pytest

import wintergrab as wg
from wintergrab.parser.autoextract import (
    LearnedSchema,
    RecordGroup,
    auto_extract,
    detect_records,
    is_stable_class,
    learn_schema,
)

BASE = "https://books.example/"

# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

PAGE_1_BOOKS = [
    ("A Light in the Attic", "a-light-in-the-attic_1000", "£51.77", "Three", "In stock"),
    ("Tipping the Velvet", "tipping-the-velvet_999", "£53.74", "One", "In stock"),
    ("Soumission", "soumission_998", "£50.10", "One", "In stock"),
    ("Sharp Objects", "sharp-objects_997", "£47.82", "Four", "Out of stock"),
    ("Sapiens: A Brief History of Humankind", "sapiens-a-brief-history-of-humankind_996", "£54.23", "Five", "In stock"),
    ("The Requiem Red", "the-requiem-red_995", "£22.65", "One", "In stock"),
]
PAGE_2_BOOKS = [
    ("In Her Wake", "in-her-wake_980", "£12.84", "One", "In stock"),
    ("How Music Works", "how-music-works_979", "£37.32", "Two", "In stock"),
    (
        "Foolproof Preserving: A Guide to Small Batch Jams",
        "foolproof-preserving_978",
        "£30.52",
        "Three",
        "Out of stock",
    ),
    ("Chase Me (Paris Nights #2)", "chase-me-paris-nights-2_977", "£25.27", "Five", "In stock"),
]
CATEGORIES = [
    "Travel",
    "Mystery",
    "Historical Fiction",
    "Sequential Art",
    "Classics",
    "Philosophy",
    "Romance",
    "Womens Fiction",
    "Fiction",
    "Childrens",
    "Religion",
    "Nonfiction",
    "Music",
    "Default",
    "Science Fiction",
    "Sports and Games",
    "Fantasy",
    "New Adult",
    "Young Adult",
    "Science",
    "Poetry",
    "Paranormal",
    "Art",
    "Psychology",
    "Autobiography",
]


def _short(title: str) -> str:
    """books.toscrape truncates long titles in the listing (the full title is in a[title])."""
    return title if len(title) <= 16 else title[:14].rstrip() + "..."


def catalogue(books: list[tuple[str, str, str, str, str]], page: int = 1) -> str:
    cards = "".join(
        f"""
        <li class="col-xs-6 col-sm-4 col-md-3 col-lg-3">
          <article class="product_pod">
            <div class="image_container">
              <a href="catalogue/{slug}/index.html"><img src="media/cache/{slug}.jpg" alt="{title}" class="thumbnail"></a>
            </div>
            <p class="star-rating {rating}">
              <i class="icon-star"></i><i class="icon-star"></i><i class="icon-star"></i><i class="icon-star"></i>
            </p>
            <h3><a href="catalogue/{slug}/index.html" title="{title}">{_short(title)}</a></h3>
            <div class="product_price">
              <p class="price_color">{price}</p>
              <p class="instock availability"><i class="icon-ok"></i>
                {stock}
              </p>
              <form><button type="submit" class="btn btn-primary btn-block">Add to basket</button></form>
            </div>
          </article>
        </li>"""
        for title, slug, price, rating, stock in books
    )
    menu = "".join(
        f'<li><a href="catalogue/category/books/{name.lower().replace(" ", "-")}_{i}/index.html">{name}</a></li>'
        for i, name in enumerate(CATEGORIES, 2)
    )
    tags = " ".join(f'<a href="/tag/{name.lower()}">{name.split()[0].lower()}</a>' for name in CATEGORIES[:22])
    return f"""<!DOCTYPE html>
<html lang="en-us"><head><title>All products | Books to Scrape - Sandbox</title>
<meta name="description" content="Books to Scrape"><style>.x {{ color: red }}</style></head>
<body id="default" class="default">
<header class="header container-fluid">
  <div class="row"><div class="col-sm-8 h1"><a href="index.html">Books to Scrape</a> We love being scraped!</div></div>
</header>
<nav class="navbar-top"><a href="index.html">Home</a> <a href="basket.html">Basket</a>
  <a href="login.html">Login</a> <a href="contact.html">Contact</a></nav>
<div class="container-fluid page">
  <ul class="breadcrumb"><li><a href="index.html">Home</a></li><li class="active">All products</li></ul>
  <div class="row">
    <aside class="sidebar col-sm-4 col-md-3">
      <div class="side_categories">
        <ul class="nav nav-list">
          <li><a href="catalogue/category/books_1/index.html">Books</a><ul>{menu}</ul></li>
        </ul>
      </div>
    </aside>
    <div class="col-sm-8 col-md-9">
      <div class="page-header action"><h1>All products</h1></div>
      <form method="get" class="form-horizontal"><strong>1000</strong> results - showing <strong>1</strong> to
        <strong>20</strong>.</form>
      <section>
        <div class="alert alert-warning" role="alert"><strong>Warning!</strong> This is a demo website.</div>
        <ol class="row">{cards}</ol>
        <ul class="pager"><li class="current">Page {page} of 50</li>
          <li class="next"><a href="catalogue/page-{page + 1}.html">next</a></li></ul>
        <div class="tag-cloud"><h4>Popular tags</h4>{tags}</div>
      </section>
    </div>
  </div>
</div>
<footer class="footer"><ul class="footer-links">
  <li><a href="/about">About us</a></li><li><a href="/careers">Careers</a></li><li><a href="/press">Press</a></li>
  <li><a href="/privacy">Privacy policy</a></li><li><a href="/terms">Terms of use</a></li><li><a href="/help">Help</a></li>
</ul><p>&copy; Books to Scrape</p></footer>
<script>var products = ["A Light in the Attic"];</script>
</body></html>"""


RESULTS = [
    ("https://www.python.org/", "Welcome to Python.org", "www.python.org",
     "The official home of the Python Programming Language."),
    ("https://docs.python.org/3/tutorial/", "The Python Tutorial", "docs.python.org › tutorial",
     "Python is an easy to learn, powerful programming language with efficient data structures."),
    ("https://en.wikipedia.org/wiki/Python_(programming_language)", "Python (programming language) - Wikipedia",
     "en.wikipedia.org › wiki", "Python is a high-level, general-purpose programming language."),
    ("https://realpython.com/", "Real Python: Python Tutorials", "realpython.com",
     "Learn Python online: tutorials for beginners and experienced developers alike."),
    ("https://pypi.org/", "PyPI · The Python Package Index", "pypi.org",
     "The Python Package Index is a repository of software for the Python programming language."),
]  # fmt: skip

SEARCH = f"""<html><head><title>python - Search</title></head><body>
<header id="top"><form action="/search"><input name="q" value="python"></form>
  <nav class="tabs"><a href="/search?q=python">All</a><a href="/images?q=python">Images</a>
  <a href="/news?q=python">News</a><a href="/videos?q=python">Videos</a><a href="/maps?q=python">Maps</a></nav>
</header>
<div id="search">
{
    "".join(
        f'<div class="result"><h3><a href="{href}">{title}</a></h3><span class="url">{shown}</span>'
        f'<p class="snippet">{snippet}</p></div>'
        for href, title, shown, snippet in RESULTS
    )
}
</div>
<div class="related"><h4>Related searches</h4><ul>
{
    "".join(
        f'<li><a href="/search?q=python+{w}">python {w}</a></li>'
        for w in ["download", "ide", "book", "course", "jobs", "docs"]
    )
}
</ul></div>
<div class="pagination">{"".join(f'<a href="?p={i}">{i}</a>' for i in range(1, 11))}</div>
<footer><a href="/help">Help</a> <a href="/privacy">Privacy</a> <a href="/terms">Terms</a></footer>
</body></html>"""

INVENTORY = [
    ("Widget", "/p/widget", "$9.99", "120", "2024-05-01"),
    ("Gadget", "/p/gadget", "$24.50", "8", "2024-05-03"),
    ("Doohickey", "/p/doohickey", "$3.25", "0", "2024-04-28"),
    ("Thingamajig", "/p/thingamajig", "$120.00", "15", "2024-05-02"),
    ("Whatchamacallit", "/p/whatchamacallit", "$7.80", "42", "2024-04-30"),
]
TABLE = f"""<html><body>
<nav><ul><li><a href="/">Home</a></li><li><a href="/stock">Stock</a></li><li><a href="/about">About</a></li></ul></nav>
<h1>Warehouse inventory</h1>
<table class="inventory">
  <tr><th>Product</th><th>Price</th><th>In stock</th><th>Updated</th></tr>
  {
    "".join(
        f'<tr><td><a href="{href}">{name}</a></td><td>{price}</td><td>{qty}</td><td>{day}</td></tr>'
        for name, href, price, qty, day in INVENTORY
    )
}
</table>
<p>Prices exclude VAT.</p>
</body></html>"""

ITEMS = [("Desk Lamp", "$24.00"), ("Office Chair", "$149.00"), ("Monitor Arm", "$89.50"),
         ("Keyboard Tray", "$35.99"), ("Cable Box", "$12.00")]  # fmt: skip
HASHED = f"""<html><body>
<div class="css-k1l2m3"><a class="css-4d5e6f" href="/">Shop</a> <a class="css-4d5e6f" href="/cart">Cart</a></div>
<main>
  <div class="css-1a2b3c">
  {
    "".join(
        f'<div class="css-9x8y7z sc-AbCdEf"><img class="css-0p9o8i" src="/img/{i}.jpg" alt="">'
        f'<h2 class="css-abc123">{name}</h2><span class="css-q1w2e3">{price}</span>'
        f'<a class="sc-bdVaJa" href="/items/{i}">View item</a></div>'
        for i, (name, price) in enumerate(ITEMS)
    )
}
  </div>
</main>
</body></html>"""


def detail(title: str, slug: str, price: str, stock: str, upc: str, description: str) -> str:
    return f"""<html><head><title>{title} | Books to Scrape</title>
<meta property="og:title" content="{title}"></head><body>
<header class="header"><a href="/">Books to Scrape</a></header>
<ul class="breadcrumb"><li><a href="../../index.html">Home</a></li>
  <li><a href="../category/books_1/index.html">Books</a></li><li class="active">{title}</li></ul>
<article class="product_page">
  <div class="row">
    <div class="col-sm-6"><div id="product_gallery"><img src="../../media/cache/{slug}.jpg" alt="{title}"></div></div>
    <div class="col-sm-6 product_main">
      <h1>{title}</h1>
      <p class="price_color">{price}</p>
      <p class="instock availability"><i class="icon-ok"></i> {stock}</p>
    </div>
  </div>
  <div id="product_description" class="sub-header"><h2>Product Description</h2></div>
  <p>{description}</p>
  <table class="table table-striped">
    <tr><th>UPC</th><td>{upc}</td></tr><tr><th>Product Type</th><td>Books</td></tr>
  </table>
</article>
</body></html>"""


DETAIL_1 = detail(
    "A Light in the Attic", "a-light", "£51.77", "In stock (22 available)", "a897fe39b1053632",
    "It's hard to imagine a world without A Light in the Attic.",
)  # fmt: skip
DETAIL_2 = detail(
    "Tipping the Velvet", "tipping", "£53.74", "In stock (20 available)", "90fa61229261140a",
    "Erotic and absorbing... Written with starling power.",
)  # fmt: skip


def _selectors_are_valid(page: wg.Selector, container: str | None, fields: dict[str, str]) -> None:
    """Generated selectors must be plain strings that wintergrab's own Selector.css() accepts."""
    scopes = page.css(container) if container else [page]
    assert scopes
    for query in [container, *fields.values()]:
        if query is None:
            continue
        assert isinstance(query, str)
        page.css(query)  # raises SelectorSyntaxError if invalid
    for sel in fields.values():
        assert any(scope.css(sel).get() is not None for scope in scopes), sel


# --------------------------------------------------------------------------- #
# class stability
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "token", ["product_pod", "star-rating", "Three", "col-md-6", "price_color", "card__title", "iPhone", "top100"]
)
def test_stable_classes(token: str) -> None:
    assert is_stable_class(token)


@pytest.mark.parametrize(
    "token",
    ["css-1x2y3z", "sc-AbCd", "sc-bdVaJa", "jsx-2912345", "item-123456", "a1b2c3", "kQzXpL", "Card_title__1a2Bc"],
)
def test_generated_classes(token: str) -> None:
    assert not is_stable_class(token)


# --------------------------------------------------------------------------- #
# detection and auto extraction
# --------------------------------------------------------------------------- #


def test_detect_records_prefers_products_over_menus() -> None:
    page = wg.parse(catalogue(PAGE_1_BOOKS), url=BASE)
    groups = detect_records(page.root)
    assert groups and all(isinstance(g, RecordGroup) for g in groups)
    best = groups[0]
    assert len(best.elements) == len(PAGE_1_BOOKS)
    assert all(el.get("class") == "product_pod" for el in best.elements)
    assert best.container_selector == "article.product_pod"
    assert len(page.css(best.container_selector)) == len(best.elements)
    assert [g.score for g in groups] == sorted((g.score for g in groups), reverse=True)
    assert {"title", "url", "image", "price", "rating", "availability"} <= set(best.fields)
    _selectors_are_valid(page, best.container_selector, best.fields)
    assert "RecordGroup('article.product_pod', 6 records" in repr(best)


def test_auto_extract_catalogue() -> None:
    records = auto_extract(wg.parse(catalogue(PAGE_1_BOOKS), url=BASE).root)
    assert len(records) == len(PAGE_1_BOOKS)
    first = records[0]
    assert first["title"] == "A Light in the Attic"  # full title from a[title], not the truncated text
    assert first["url"] == BASE + "catalogue/a-light-in-the-attic_1000/index.html"
    assert first["image"] == BASE + "media/cache/a-light-in-the-attic_1000.jpg"
    assert first["price"] == "£51.77"
    assert "Three" in first["rating"]
    assert first["availability"] == "In stock"
    assert [r["availability"] for r in records].count("Out of stock") == 1
    assert [r["title"] for r in records] == [b[0] for b in PAGE_1_BOOKS]
    assert all("Add to basket" not in r.values() for r in records)  # constant boilerplate is skipped


def test_auto_extract_accepts_selector_response_and_markup() -> None:
    html = catalogue(PAGE_1_BOOKS)
    from_selector = auto_extract(wg.parse(html, url=BASE))
    response = SimpleNamespace(url=BASE, selector=wg.parse(html, url=BASE))
    assert from_selector == auto_extract(response) == auto_extract(html, BASE)
    assert from_selector[0]["url"].startswith(BASE)


def test_auto_extract_search_results() -> None:
    page = wg.parse(SEARCH, url="https://search.example/search?q=python")
    best = detect_records(page)[0]
    assert best.container_selector in ("div.result", "#search > div.result")
    _selectors_are_valid(page, best.container_selector, best.fields)
    records = auto_extract(page)
    assert [r["title"] for r in records] == [r[1] for r in RESULTS]
    assert [r["url"] for r in records] == [r[0] for r in RESULTS]
    assert [r["snippet"] for r in records] == [r[3] for r in RESULTS]
    assert records[1]["url_2"] == "docs.python.org › tutorial"


def test_auto_extract_table_rows_named_by_headers() -> None:
    page = wg.parse(TABLE, url="https://shop.example/inventory")
    best = detect_records(page)[0]
    assert len(best.elements) == len(INVENTORY)  # the header row is not a record
    assert len(page.css(best.container_selector)) == len(INVENTORY)
    _selectors_are_valid(page, best.container_selector, best.fields)
    records = auto_extract(page)
    assert records[0] == {
        "title": "Widget",
        "url": "https://shop.example/p/widget",
        "price": "$9.99",
        "in_stock": "120",
        "updated": "2024-05-01",
    }
    assert [r["title"] for r in records] == [row[0] for row in INVENTORY]


def test_auto_extract_with_generated_class_names() -> None:
    page = wg.parse(HASHED, url="https://shop.example/")
    best = detect_records(page)[0]
    assert len(best.elements) == len(ITEMS)
    selectors = [best.container_selector, *best.fields.values()]
    assert not any("css-" in s or "sc-" in s for s in selectors), selectors
    assert len(page.css(best.container_selector)) == len(ITEMS)
    _selectors_are_valid(page, best.container_selector, best.fields)
    records = auto_extract(page)
    assert [(r["title"], r["price"]) for r in records] == ITEMS
    assert records[2]["url"] == "https://shop.example/items/2"
    assert records[2]["image"] == "https://shop.example/img/2.jpg"


def test_cards_split_over_grid_rows_are_one_list() -> None:
    names = ["Alpha Speaker", "Bravo Headphones", "Charlie Turntable", "Delta Soundbar", "Echo Amplifier",
             "Foxtrot Radio", "Golf Earbuds", "Hotel Subwoofer"]  # fmt: skip
    cards = [
        f'<div class="card"><img src="data:image/gif;base64,R0lGOD" data-src="/img/{i}.jpg" alt="">'
        f'<h4 class="card-title"><a href="/p/{i}">{name}</a></h4>'
        f'<p class="card-text">{name} with a {i + 2} year warranty.</p><span class="price">€{40 + 9 * i},00</span></div>'
        for i, name in enumerate(names)
    ]
    rows = "".join(f'<div class="row">{"".join(cards[i : i + 3])}</div>' for i in range(0, len(cards), 3))
    html = f'<html><body><main><div class="container">{rows}</div></main></body></html>'
    best = detect_records(html)[0]
    assert best.container_selector == "div.card"  # the cards, not the rows holding them
    assert len(best.elements) == len(names)
    records = auto_extract(html, "https://audio.example/")
    assert [r["title"] for r in records] == names
    assert records[1]["image"] == "https://audio.example/img/1.jpg"  # lazy-loaded data-src, not the placeholder
    assert records[1]["price"] == "€49,00"
    assert records[1]["card_text"] == "Bravo Headphones with a 3 year warranty."


def test_auto_extract_returns_nothing_without_records() -> None:
    article = """<html><body><nav><ul class="menu">
      <li><a href="/">Home</a></li><li><a href="/blog">Blog</a></li><li><a href="/about">About</a></li>
      <li><a href="/contact">Contact</a></li></ul></nav>
      <main><h1>On scraping</h1>
      <p>Scraping is the art of reading pages written for humans with a program instead of a person.</p>
      <p>Most pages have a <a href="/list">list</a> of records, but <em>this one</em> does not have any.</p>
      <p>It is just an essay with a few paragraphs and a menu, so nothing should be extracted here.</p>
      <p>Returning an empty list is better than returning paragraphs or menu links as records.</p>
      </main></body></html>"""
    assert auto_extract(article) == []
    assert auto_extract("<html><body></body></html>") == []


# --------------------------------------------------------------------------- #
# scraping by example
# --------------------------------------------------------------------------- #


def test_learn_schema_from_one_record_generalizes_to_other_pages() -> None:
    page_1 = wg.parse(catalogue(PAGE_1_BOOKS), url=BASE)
    schema = learn_schema(page_1, {"title": "A Light in the Attic", "price": "£51.77"})
    assert isinstance(schema, LearnedSchema)
    assert schema.container == "article.product_pod"
    assert schema.fields == {"title": "h3 a::attr(title)", "price": "p.price_color::text"}
    _selectors_are_valid(page_1, schema.container, schema.fields)

    assert schema.extract(page_1) == [{"title": t, "price": p} for t, _, p, _, _ in PAGE_1_BOOKS]
    page_2 = wg.parse(catalogue(PAGE_2_BOOKS, page=2), url=BASE + "catalogue/page-2.html")
    assert schema.extract(page_2) == [{"title": t, "price": p} for t, _, p, _, _ in PAGE_2_BOOKS]
    assert schema.extract_one(page_2) == {"title": "In Her Wake", "price": "£12.84"}


def test_learn_schema_matches_urls_images_and_attributes() -> None:
    page = wg.parse(catalogue(PAGE_1_BOOKS), url=BASE)
    schema = learn_schema(
        page.root,
        {
            "title": "Sharp Objects",
            "url": BASE + "catalogue/sharp-objects_997/index.html",  # absolute; the href is relative
            "image": "media/cache/sharp-objects_997.jpg",
            "stock": "Out of stock",
        },
        base_url=BASE,
    )
    # "Sharp Objects" is shown in full, but other books are truncated: the full title lives in a[title].
    assert schema.fields == {
        "title": "h3 a::attr(title)",
        "url": "h3 a::attr(href)",
        "image": "img.thumbnail::attr(src)",
        "stock": "p.availability::text",
    }
    page_2 = wg.parse(catalogue(PAGE_2_BOOKS, page=2), url=BASE)
    rows = schema.extract(page_2)
    assert rows[2]["title"] == "Foolproof Preserving: A Guide to Small Batch Jams"
    assert rows[1] == {
        "title": "How Music Works",
        "url": BASE + "catalogue/how-music-works_979/index.html",
        "image": BASE + "media/cache/how-music-works_979.jpg",
        "stock": "In stock",
    }


def test_learn_schema_with_several_examples() -> None:
    page = wg.parse(SEARCH, url="https://search.example/")
    schema = learn_schema(
        page,
        [
            {"title": "Welcome to Python.org", "snippet": RESULTS[0][3]},
            {"title": "The Python Tutorial", "snippet": RESULTS[1][3]},
        ],
    )
    assert schema.container is not None
    assert len(page.css(schema.container)) == len(RESULTS)
    _selectors_are_valid(page, schema.container, schema.fields)
    assert [r["title"] for r in schema.extract(page)] == [r[1] for r in RESULTS]
    assert schema.extract(page)[-1]["snippet"] == RESULTS[-1][3]


def test_optional_fields() -> None:
    rows = [("One", "1.00"), ("Two", None), ("Three", "3.00"), ("Four", "4.00")]
    prices = [f'<span class="price">${price}</span>' if price else "" for _, price in rows]
    items = "".join(
        f'<li class="item"><h2>{name}</h2>{price}<a href="/i/{name.lower()}">more</a></li>'
        for (name, _), price in zip(rows, prices, strict=True)
    )
    page = f"<html><body><ul class='items'>{items}</ul></body></html>"
    records = auto_extract(page, "https://shop.example/")
    assert records[1] == {"title": "Two", "url": "https://shop.example/i/two"}  # no price: key left out
    # The second example has no price: it still marks its <li> as a record.
    schema = learn_schema(page, [{"name": "One", "price": "$1.00"}, {"name": "Two"}])
    assert schema == LearnedSchema("li.item", {"name": "h2::text", "price": "span.price::text"})
    assert schema.extract(page)[1] == {"name": "Two", "price": None}


def test_learn_schema_on_a_detail_page() -> None:
    page = wg.parse(DETAIL_1, url=BASE + "catalogue/a-light/index.html")
    example = {
        "title": "A Light in the Attic",
        "price": "£51.77",
        "upc": "a897fe39b1053632",
        "description": "It's hard to imagine a world without A Light in the Attic.",
    }
    schema = learn_schema(page, example)
    assert schema.container is None  # a single record: selectors are relative to the document
    assert schema.fields["title"] == "h1::text"  # not the breadcrumb item with the same text
    assert not any(":scope" in s for s in schema.fields.values())  # no positional paths needed
    _selectors_are_valid(page, None, schema.fields)
    assert schema.extract_one(page) == example
    other = wg.parse(DETAIL_2, url=BASE + "catalogue/tipping/index.html")
    assert schema.extract(other) == [
        {
            "title": "Tipping the Velvet",
            "price": "£53.74",
            "upc": "90fa61229261140a",
            "description": "Erotic and absorbing... Written with starling power.",
        }
    ]
    # With a single field there is no record to anchor on: prefer content over navigation.
    assert learn_schema(page, {"title": "A Light in the Attic"}).fields == {"title": "h1::text"}


def test_learn_schema_on_hashed_classes_and_tables() -> None:
    page = wg.parse(HASHED, url="https://shop.example/")
    schema = learn_schema(page, {"name": "Office Chair", "price": "$149.00"})
    assert "css-" not in (schema.container or "") and not any("css-" in s for s in schema.fields.values())
    assert [(r["name"], r["price"]) for r in schema.extract(page)] == ITEMS

    table = wg.parse(TABLE, url="https://shop.example/")
    schema = learn_schema(table, {"product": "Gadget", "updated": "2024-05-03"})
    assert len(table.css(schema.container or "")) == len(INVENTORY)
    assert schema.extract(table)[-1] == {"product": "Whatchamacallit", "updated": "2024-04-30"}


def test_learned_schema_round_trip() -> None:
    page = wg.parse(catalogue(PAGE_1_BOOKS), url=BASE)
    schema = learn_schema(page, {"title": "Soumission", "price": "£50.10"})
    again = LearnedSchema.from_dict(schema.to_dict())
    assert again == schema
    assert again.extract(page) == schema.extract(page)
    assert repr(schema) == f"LearnedSchema(container={schema.container!r}, fields={schema.fields!r})"
    with pytest.raises(ValueError):
        LearnedSchema.from_dict({"container": 3, "fields": {}})


def test_learn_schema_unknown_example_raises() -> None:
    page = wg.parse(catalogue(PAGE_1_BOOKS), url=BASE)
    with pytest.raises(ValueError, match="'price' not found"):
        learn_schema(page, {"title": "A Light in the Attic", "price": "£999.99"})
    with pytest.raises(ValueError):
        learn_schema(page, {})
