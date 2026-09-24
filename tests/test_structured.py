from __future__ import annotations

import time
from typing import Any

import pytest

import wintergrab as wg
from wintergrab.parser.selector import parse_document
from wintergrab.parser.structured import (
    embedded_json,
    find_values,
    next_page_url,
    structured_data,
    table_to_records,
    tables,
)

URL = "https://shop.test/products/widget"


def root_of(html: str, url: str | None = None) -> Any:
    return wg.parse(html, url=url).root


# --------------------------------------------------------------------------- #
# structured_data
# --------------------------------------------------------------------------- #

PRODUCT_PAGE = r"""<!DOCTYPE html>
<html lang="en-GB">
<head>
  <meta charset="utf-8">
  <title>  Acme Widget |
     Acme Shop </title>
  <meta name="Description" content="The best widget money can buy.">
  <meta name="keywords" content="widget, gadget">
  <meta name="author" content="Acme Ltd">
  <meta name="robots" content="index, follow">
  <meta name="description" content="A later, ignored description">
  <link rel="canonical" href="/products/widget?ref=canonical">
  <link rel="shortcut icon" href="/static/favicon.ico">
  <link rel="apple-touch-icon" href="/static/touch.png">
  <link rel="alternate" type="application/rss+xml" href="/feed.rss" title="RSS">
  <link rel="alternate" type="application/atom+xml" href="https://shop.test/feed.atom">
  <link rel="alternate" hreflang="de" href="/de/products/widget">

  <meta property="og:type" content="product">
  <meta property="og:title" content="Acme Widget">
  <meta property="og:url" content="/products/widget">
  <meta property="og:image" content="https://cdn.shop.test/w1.jpg">
  <meta property="og:image:width" content="800">
  <meta property="og:image" content="/img/w2.jpg">
  <meta property="og:image:width" content="600">
  <meta property="product:price:amount" content="19.99">
  <meta property="product:price:currency" content="EUR">
  <meta property="article:tag" content="tools">
  <meta property="og:description" content="">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:site" content="@acme">
  <meta property="twitter:image" content="/img/card.png">

  <script type="application/ld+json">
  {"@context": "https://schema.org",
   "@graph": [
     {"@type": "Organization", "@id": "#org", "name": "Acme"},
     {"@type": "WebSite", "@context": "https://schema.org/", "url": "https://shop.test/"},
     "not an object"
   ]}
  </script>
  <script type="application/ld+json">
  [{"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": []},
   {"@context": "https://schema.org", "@type": "Person", "name": "Ann"}]
  </script>
  <script type="Application/LD+JSON; charset=utf-8">
  <!--
  {"@context": "https://schema.org", "@type": "Product", "name": "Widget",
   "note": "commas like ,} stay put",
   "offers": {"@type": "Offer", "price": "19.99",},
  }
  -->
  </script>
  <script type="application/ld+json">
  //<![CDATA[
  {"@type": "FAQPage", "text": "line one
line two"}
  //]]>
  </script>
  <script type="application/ld+json">&#xFEFF;{"broken": }</script>
  <script type="application/ld+json">
  {"@type": "Event", "name": "Launch"}
  {"@type": "Event", "name": "Party"}
  </script>
</head>
<body>
  <svg><title>icon title</title></svg>
  <div itemscope itemtype="https://schema.org/Product" itemid="/products/widget#product">
    <h1 itemprop="name alternateName">Acme   Widget</h1>
    <img itemprop="image" src="/img/widget.jpg" alt="">
    <p itemprop="description">A <b>really</b> good widget.</p>
    <a itemprop="url" href="widget?color=red">Red variant</a>
    <span itemprop="sku" content="W-1">W 1 (display)</span>
    <div itemprop="brand" itemscope itemtype="https://schema.org/Brand">
      <span itemprop="name">Acme</span>
    </div>
    <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
      <meta itemprop="priceCurrency" content="EUR">
      <span itemprop="price">19.99</span>
      <link itemprop="availability" href="https://schema.org/InStock">
      <time itemprop="priceValidUntil" datetime="2026-12-31">end of year</time>
      <data itemprop="gtin" value="0123456789012">barcode</data>
    </div>
    <div itemprop="aggregateRating" itemscope itemtype="https://schema.org/AggregateRating">
      Rated <span itemprop="ratingValue">4.6</span>/5 from
      <span itemprop="reviewCount">128</span> reviews
    </div>
    <div itemprop="review" itemscope itemtype="https://schema.org/Review">
      <span itemprop="author">Bob</span> <meter itemprop="rating" value="5">5 stars</meter>
    </div>
    <div itemprop="review" itemscope itemtype="https://schema.org/Review">
      <span itemprop="author">Eve</span>
    </div>
    <div itemscope itemtype="https://schema.org/Thing">
      <span itemprop="name">Independent thing</span>
    </div>
  </div>
</body>
</html>
"""


@pytest.fixture(scope="module")
def product_data() -> dict[str, Any]:
    return structured_data(root_of(PRODUCT_PAGE), URL)


def test_structured_data_shape(product_data: dict[str, Any]) -> None:
    assert list(product_data) == ["json_ld", "microdata", "opengraph", "twitter", "meta"]


def test_json_ld_graph_list_and_lenient_parsing(product_data: dict[str, Any]) -> None:
    items = product_data["json_ld"]
    assert all(isinstance(item, dict) for item in items)
    assert [item.get("@type") for item in items] == [
        "Organization",
        "WebSite",
        "BreadcrumbList",
        "Person",
        "Product",
        "FAQPage",
        "Event",
        "Event",
    ]
    org, site = items[0], items[1]
    assert org == {"@context": "https://schema.org", "@type": "Organization", "@id": "#org", "name": "Acme"}
    assert site["@context"] == "https://schema.org/"  # an item's own @context wins
    product = items[4]
    assert product["offers"] == {"@type": "Offer", "price": "19.99"}  # trailing commas + comment wrapper
    assert product["note"] == "commas like ,} stay put"
    assert items[5]["text"] == "line one\nline two"  # CDATA wrapper, raw newline in a string
    assert [items[6]["name"], items[7]["name"]] == ["Launch", "Party"]  # back-to-back objects


def test_json_ld_skips_broken_and_handles_bom() -> None:
    html = (
        '<script type="application/ld+json">\ufeff {"@type": "Thing", "name": "bom"}</script>'
        '<script type="application/ld+json">{"@type": oops}</script>'
        '<script type="application/ld+json"></script>'
        '<script type="application/json">{"@type": "NotLd"}</script>'
    )
    assert structured_data(root_of(html))["json_ld"] == [{"@type": "Thing", "name": "bom"}]


def test_microdata_nested_items(product_data: dict[str, Any]) -> None:
    product, thing = product_data["microdata"]
    assert thing == {"@type": "https://schema.org/Thing", "name": "Independent thing"}
    assert product["@type"] == "https://schema.org/Product"
    assert product["@id"] == "https://shop.test/products/widget#product"
    assert product["name"] == product["alternateName"] == "Acme Widget"
    assert product["image"] == "https://shop.test/img/widget.jpg"
    assert product["description"] == "A really good widget."
    assert product["url"] == "https://shop.test/products/widget?color=red"
    assert product["sku"] == "W-1"
    assert product["brand"] == {"@type": "https://schema.org/Brand", "name": "Acme"}
    assert product["offers"] == {
        "@type": "https://schema.org/Offer",
        "priceCurrency": "EUR",
        "price": "19.99",
        "availability": "https://schema.org/InStock",
        "priceValidUntil": "2026-12-31",
        "gtin": "0123456789012",
    }
    assert product["aggregateRating"] == {
        "@type": "https://schema.org/AggregateRating",
        "ratingValue": "4.6",
        "reviewCount": "128",
    }
    assert product["review"] == [
        {"@type": "https://schema.org/Review", "author": "Bob", "rating": "5"},
        {"@type": "https://schema.org/Review", "author": "Eve"},
    ]
    # Properties of nested items (and of the separate Thing) do not leak into the product.
    assert "ratingValue" not in product and "author" not in product
    assert product["name"] != "Independent thing"


def test_microdata_value_fallbacks() -> None:
    html = """<div itemscope>
      <a itemprop="label">no href</a>
      <time itemprop="when">yesterday</time>
      <object itemprop="doc" data="/files/a.pdf"></object>
      <span itemprop="tag">a</span><span itemprop="tag">b</span><span itemprop="tag">c</span>
    </div>"""
    (item,) = structured_data(root_of(html), "https://x.test/dir/")["microdata"]
    assert item == {
        "label": "no href",
        "when": "yesterday",
        "doc": "https://x.test/files/a.pdf",
        "tag": ["a", "b", "c"],
    }


def test_opengraph_and_twitter(product_data: dict[str, Any]) -> None:
    assert product_data["opengraph"] == {
        "type": "product",
        "title": "Acme Widget",
        "url": "https://shop.test/products/widget",
        "image": ["https://cdn.shop.test/w1.jpg", "https://shop.test/img/w2.jpg"],
        "image:width": ["800", "600"],
        "product:price:amount": "19.99",
        "product:price:currency": "EUR",
        "article:tag": "tools",
    }
    assert product_data["twitter"] == {
        "card": "summary_large_image",
        "site": "@acme",
        "image": "https://shop.test/img/card.png",
    }


def test_meta(product_data: dict[str, Any]) -> None:
    assert product_data["meta"] == {
        "title": "Acme Widget | Acme Shop",
        "description": "The best widget money can buy.",
        "keywords": "widget, gadget",
        "author": "Acme Ltd",
        "robots": "index, follow",
        "canonical": "https://shop.test/products/widget?ref=canonical",
        "language": "en-GB",
        "favicon": "https://shop.test/static/favicon.ico",
        "feeds": ["https://shop.test/feed.rss", "https://shop.test/feed.atom"],
    }


def test_structured_data_on_a_bare_page() -> None:
    data = structured_data(root_of("<p>nothing to see</p>"))
    assert data == {"json_ld": [], "microdata": [], "opengraph": {}, "twitter": {}, "meta": {}}
    only_touch = structured_data(root_of('<link rel="apple-touch-icon" href="/t.png"><title></title>'))
    assert only_touch["meta"] == {"favicon": "/t.png"}  # no base URL: left relative


# --------------------------------------------------------------------------- #
# embedded_json
# --------------------------------------------------------------------------- #

SPA_PAGE = r"""<html><head>
<script id="__NEXT_DATA__" type="application/json">
{"props": {"pageProps": {"product": {"id": 7, "name": "Widget", "price": {"amount": 19.99}}}}, "page": "/p/[id]"}
</script>
<script type="application/json" data-target="react-app.embeddedData">{"payload": {"repo": "wintergrab"}}</script>
<script type="application/json">[1, 2, 3]</script>
<script type="application/json">{}</script>
<script type="application/json">not json at all</script>
<script type="application/vnd.api+json" id="api">{"data": [{"id": "1"}]}</script>
<script type="application/ld+json">{"@type": "Thing"}</script>
<script type="text/template" id="tpl">{"not": "javascript"}</script>
<script>
  window.__INITIAL_STATE__ = {"user": {"name": "Ann", "id": 42}, "cart": [{"sku": "A1", "qty": 2}]};
  window["__APOLLO_STATE__"] = JSON.parse("{\"ROOT_QUERY\":{\"product\":{\"__ref\":\"Product:1\"}},\"Product:1\":{\"name\":\"Caf\u00e9 \\\"Deluxe\\\"\"}}");
  window.__DATA__ = JSON.parse('{"msg":"it\'s","hex":"\x41\u00e9","nl":"a\\nb","emoji":"\ud83d\ude00","q":"say \\"hi\\""}');
  self.__FLAGS = ["new-checkout", "dark-mode"];
  var pageConfig = {"locale": "en", "debug": false};
  let emptyThing = {};
  const notJson = {unquoted: 1};
  window.__CALLBACK__ = init({"a": 1});
  if (x == {"compare": 1}) {}
  var count = 5, tags = ["x", "y"];
</script>
<script type="module">__NUXT__ = {"state": {"count": 3}, "serverRendered": true}</script>
<script src="/app.js"></script>
</head><body></body></html>
"""


def test_embedded_json_spa_state() -> None:
    data = embedded_json(root_of(SPA_PAGE))
    assert list(data) == [
        "__NEXT_DATA__",
        "react-app.embeddedData",
        "json_script_1",
        "api",
        "__INITIAL_STATE__",
        "__APOLLO_STATE__",
        "__DATA__",
        "__FLAGS",
        "pageConfig",
        "tags",
        "__NUXT__",
    ]
    assert data["__NEXT_DATA__"]["props"]["pageProps"]["product"]["name"] == "Widget"
    assert data["react-app.embeddedData"] == {"payload": {"repo": "wintergrab"}}
    assert data["json_script_1"] == [1, 2, 3]
    assert data["api"] == {"data": [{"id": "1"}]}
    assert data["__INITIAL_STATE__"]["cart"] == [{"sku": "A1", "qty": 2}]
    assert data["__APOLLO_STATE__"] == {
        "ROOT_QUERY": {"product": {"__ref": "Product:1"}},
        "Product:1": {"name": 'Café "Deluxe"'},
    }
    assert data["__FLAGS"] == ["new-checkout", "dark-mode"]
    assert data["pageConfig"] == {"locale": "en", "debug": False}
    assert data["__NUXT__"] == {"state": {"count": 3}, "serverRendered": True}


def test_embedded_json_js_string_escapes() -> None:
    data = embedded_json(root_of(SPA_PAGE))
    assert data["__DATA__"] == {"msg": "it's", "hex": "Aé", "nl": "a\nb", "emoji": "\U0001f600", "q": 'say "hi"'}


def test_embedded_json_duplicate_names_and_wrappers() -> None:
    html = r"""
    <script id="state" type="application/json"><!-- {"a": 1} --></script>
    <script>//<![CDATA[
      window.state = {"b": 2};
    //]]></script>
    <script>window.state = JSON.parse("{\"c\": 3}")</script>
    """
    assert embedded_json(root_of(html)) == {"state": {"a": 1}, "state_2": {"b": 2}, "state_3": {"c": 3}}


def test_embedded_json_is_fast_on_big_noisy_scripts() -> None:
    noise = "var x = 1;\n" * 20_000 + "a={" * 20_000 + 'e=["div",x];' * 20_000 + "x=" * 20_000
    html = f'<script>{noise}window.__STATE__ = {{"ok": [1, 2]}};{noise}</script>'
    started = time.perf_counter()
    assert embedded_json(root_of(html)) == {"__STATE__": {"ok": [1, 2]}}
    assert time.perf_counter() - started < 5  # ~0.1s; quadratic behaviour would take minutes


# --------------------------------------------------------------------------- #
# find_values
# --------------------------------------------------------------------------- #


def test_find_values_document_order_and_limit() -> None:
    data = {"a": {"id": 1, "items": [{"id": 2}, {"x": {"id": 3}}]}, "id": 4, "list": [[{"id": 5}]]}
    assert find_values(data, "id") == [1, 2, 3, 4, 5]
    assert find_values(data, "id", limit=2) == [1, 2]
    assert find_values(data, "id", limit=0) == []
    assert find_values(data, "missing") == []
    assert find_values("scalar", "id") == []
    assert find_values({"node": {"node": 1}}, "node") == [{"node": 1}, 1]


def test_find_values_predicate_and_next_data() -> None:
    data = embedded_json(root_of(SPA_PAGE))
    assert find_values(data["__NEXT_DATA__"], "amount") == [19.99]
    assert find_values(data, lambda k: isinstance(k, str) and k.startswith("__ref")) == ["Product:1"]


def test_find_values_deep_and_cyclic_data() -> None:
    deep: Any = {"id": "bottom"}
    for _ in range(50_000):
        deep = [{"child": deep}]
    assert find_values(deep, "id") == ["bottom"]
    cyclic: dict[str, Any] = {"id": 1}
    cyclic["self"] = cyclic
    assert find_values(cyclic, "id") == [1]


# --------------------------------------------------------------------------- #
# tables
# --------------------------------------------------------------------------- #

TABLES_PAGE = """<html><body>
<table id="prices" class="data  striped">
  <caption> Price   list </caption>
  <thead><tr><th>Item</th><th>Price</th></tr></thead>
  <tfoot><tr><td>Total</td><td>3.50</td></tr></tfoot>
  <tbody>
    <tr><td>Tea</td><td>1.50</td></tr>
    <tr><td> </td><td></td></tr>
    <tr><th>Coffee</th><td>2.00</td></tr>
  </tbody>
</table>

<table id="spans">
  <thead>
    <tr><th rowspan="2">Region</th><th colspan="2">Sales</th></tr>
    <tr><th>2023</th><th>2024</th></tr>
  </thead>
  <tbody>
    <tr><td rowspan="2">North</td><td>10</td><td>12</td></tr>
    <tr><td>11</td><td>13</td></tr>
    <tr><td>South</td><td colspan="2">n/a</td></tr>
  </tbody>
</table>

<table id="outer">
  <tr><th>Name</th><th>Details</th></tr>
  <tr><td>Alpha</td><td><table id="inner"><tr><td>k</td><td>v</td></tr></table></td></tr>
</table>

<table id="plain"><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td><td>e</td></tr></table>

<table id="dupes">
  <tr><th>Name</th><th>Name</th><th></th><th>Name</th></tr>
  <tr><td>1</td><td>2</td><td>3</td><td>4</td></tr>
</table>

<table id="multilevel">
  <tr><th rowspan="2">City</th><th colspan="2">Temp</th></tr>
  <tr><th>Min</th><th>Max</th></tr>
  <tr><td>Oslo</td><td>-3</td><td>4</td></tr>
</table>

<table id="empty"></table>
<table id="rowless"><tr></tr></table>
</body></html>
"""


@pytest.fixture(scope="module")
def page_tables() -> dict[str, dict[str, Any]]:
    return {t["id"]: t for t in tables(root_of(TABLES_PAGE), "https://x.test/")}


def test_tables_found_in_document_order(page_tables: dict[str, dict[str, Any]]) -> None:
    assert list(page_tables) == ["prices", "spans", "outer", "inner", "plain", "dupes", "multilevel"]


def test_table_with_thead_caption_and_tfoot(page_tables: dict[str, dict[str, Any]]) -> None:
    prices = page_tables["prices"]
    assert prices["caption"] == "Price list"
    assert prices["class"] == "data striped"
    assert prices["headers"] == ["Item", "Price"]
    assert prices["rows"] == [
        {"Item": "Tea", "Price": "1.50"},
        {"Item": "Coffee", "Price": "2.00"},  # row-header <th> is a normal cell
        {"Item": "Total", "Price": "3.50"},  # <tfoot> goes last, like browsers render it
    ]


def test_table_colspan_rowspan(page_tables: dict[str, dict[str, Any]]) -> None:
    spans = page_tables["spans"]
    assert spans["headers"] == ["Region", "Sales / 2023", "Sales / 2024"]
    assert spans["rows"] == [
        {"Region": "North", "Sales / 2023": "10", "Sales / 2024": "12"},
        {"Region": "North", "Sales / 2023": "11", "Sales / 2024": "13"},
        {"Region": "South", "Sales / 2023": "n/a", "Sales / 2024": "n/a"},
    ]


def test_nested_tables_keep_their_own_rows(page_tables: dict[str, dict[str, Any]]) -> None:
    outer = page_tables["outer"]
    assert outer["headers"] == ["Name", "Details"]
    assert outer["rows"] == [{"Name": "Alpha", "Details": "k v"}]
    assert page_tables["inner"]["rows"] == [{"column_1": "k", "column_2": "v"}]


def test_tables_without_headers_and_duplicate_headers(page_tables: dict[str, dict[str, Any]]) -> None:
    plain = page_tables["plain"]
    assert plain["caption"] is None and plain["class"] is None
    assert plain["headers"] == ["column_1", "column_2", "column_3"]
    assert plain["rows"] == [
        {"column_1": "a", "column_2": "b", "column_3": ""},
        {"column_1": "c", "column_2": "d", "column_3": "e"},
    ]
    dupes = page_tables["dupes"]
    assert dupes["headers"] == ["Name", "Name_2", "column_3", "Name_3"]
    assert dupes["rows"] == [{"Name": "1", "Name_2": "2", "column_3": "3", "Name_3": "4"}]


def test_multilevel_header_without_thead(page_tables: dict[str, dict[str, Any]]) -> None:
    table = page_tables["multilevel"]
    assert table["headers"] == ["City", "Temp / Min", "Temp / Max"]
    assert table["rows"] == [{"City": "Oslo", "Temp / Min": "-3", "Temp / Max": "4"}]


def test_table_to_records_single_element_and_span_cap() -> None:
    root = root_of('<table id="t"><tr><th>A</th></tr><tr><td colspan="99999" rowspan="0">x</td></tr></table>')
    record = table_to_records(root.find(".//table"))
    assert len(record["headers"]) == 1000
    assert record["headers"][:2] == ["A", "column_2"]
    assert record["rows"][0]["A"] == record["rows"][0]["column_1000"] == "x"
    empty = table_to_records(root_of("<table id='e'><caption>Nothing</caption></table>").find(".//table"))
    assert empty == {"caption": "Nothing", "headers": [], "rows": [], "id": "e", "class": None}


# --------------------------------------------------------------------------- #
# next_page_url
# --------------------------------------------------------------------------- #

BASE = "https://blog.test/page/3"


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        pytest.param(
            '<head><link rel="next" href="/page/4"></head><body><a href="/page/9">Next</a></body>',
            "https://blog.test/page/4",
            id="link-rel-next",
        ),
        pytest.param(
            '<a href="/page/2">Previous</a><a rel="nofollow NEXT" href="?p=4">go</a><a href="/x">Next</a>',
            "https://blog.test/page/3?p=4",
            id="a-rel-next",
        ),
        pytest.param(
            '<a href="/page/2" aria-label="Previous page">‹</a><a href="/page/4" aria-label="Next page"><svg/></a>',
            "https://blog.test/page/4",
            id="aria-label",
        ),
        pytest.param(
            '<ul class="pager"><li class="previous"><a href="/page/2/">← Previous</a></li>'
            '<li><a href="/page/4/">Next <span aria-hidden="true">→</span></a></li></ul>',
            "https://blog.test/page/4/",
            id="text-next-arrow",
        ),
        pytest.param(
            '<a href="?page=1">«</a><a href="?page=2">‹</a><a href="?page=4">›</a><a href="?page=9">»</a>',
            "https://blog.test/page/3?page=4",
            id="single-arrow-beats-double",
        ),
        pytest.param(
            '<div class="nav-links"><a href="/older">« Older Entries</a></div>',
            "https://blog.test/older",
            id="older-entries",
        ),
        pytest.param(
            '<a class="btn prev" href="/page/2"><i></i></a><a class="btn pager__next" href="/page/4"><i></i></a>',
            "https://blog.test/page/4",
            id="class-next",
        ),
        pytest.param(
            '<nav class="pagination"><a href="/page/1">1</a><a href="/page/2">2</a>'
            '<span class="page-numbers current">3</span><a href="/page/4">4</a><a href="/page/5">5</a></nav>',
            "https://blog.test/page/4",
            id="numeric-current-span",
        ),
        pytest.param(
            '<ul class="pages"><li><a href="?page=2">2</a></li><li><a aria-current="page" href="/page/3">3</a></li>'
            '<li><a href="?page=4"><span class="sr-only">Page</span> 4</a></li></ul>',
            "https://blog.test/page/3?page=4",
            id="numeric-aria-current",
        ),
        pytest.param(
            '<div role="navigation"><ul><li><a href="/p/2">2</a></li><li class="active"><a href="#">3</a></li>'
            '<li><a href="/p/4">4</a></li></ul></div>',
            "https://blog.test/p/4",
            id="numeric-active-class",
        ),
    ],
)
def test_next_page_url(html: str, expected: str) -> None:
    assert next_page_url(root_of(html), BASE) == expected


@pytest.mark.parametrize(
    "html",
    [
        pytest.param('<a class="prev" href="/page/2">« Previous</a>', id="only-previous"),
        pytest.param(
            '<div class="pagination"><a href="/page/2">« Previous</a> <span class="current">3</span></div>',
            id="last-numeric-page",
        ),
        pytest.param(
            '<a href="javascript:void(0)">Next</a><a href="#">Next</a><a href="mailto:a@b.test">next</a>'
            '<a href="/page/3#top">Next</a><a class="next disabled" href="/page/4">Next</a>',
            id="unusable-links",
        ),
        pytest.param("<p>No links here</p>", id="no-links"),
    ],
)
def test_next_page_url_none(html: str) -> None:
    assert next_page_url(root_of(html), BASE) is None


def test_next_page_url_without_base_and_priority() -> None:
    html = '<a href="?page=2">Load more</a><a href="?page=4">›</a><a href="?page=5">Next page</a>'
    assert next_page_url(root_of(html)) == "?page=5"  # word "next" outranks "load more" and arrows


def test_next_page_url_via_parse_document() -> None:
    root = parse_document('<li class="next"><a href="/catalogue/page-2.html">next</a></li>')
    assert next_page_url(root, "https://books.test/index.html") == "https://books.test/catalogue/page-2.html"
