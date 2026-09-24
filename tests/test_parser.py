from __future__ import annotations

import pytest

import wintergrab as wg
from wintergrab import Field, Selector, SelectorSyntaxError

HTML = """
<html><head><title>Shop</title><base href="https://shop.example/"></head><body>
<nav><a href="/">Home</a> <a href="/about">About</a> <a href="mailto:x@y.z">Mail</a></nav>
<div id="products" class="grid">
  <div class="product featured" data-id="1"><h2 class="name">Blue Mug</h2><span class="price">$12.50</span><a href="/p/1">View</a></div>
  <div class="product" data-id="2"><h2 class="name">Red Mug</h2><span class="price">$9.99</span><a href="/p/2">View</a></div>
  <div class="product" data-id="3"><h2 class="name">Green Mug</h2><span class="price">$15.00</span><a href="/p/3#reviews">View</a></div>
</div>
<p id="promo">Price: <b>$10</b> today</p>
<aside class="sidebar"><h2>Ads</h2></aside>
<script>var hidden = "do not show";</script>
</body></html>
"""


@pytest.fixture
def page() -> Selector:
    return Selector(HTML, url="https://shop.example/list")


def test_css_text_and_attributes(page: Selector) -> None:
    assert page.css("title::text").get() == "Shop"
    assert page.css(".product h2::text").getall() == ["Blue Mug", "Red Mug", "Green Mug"]
    assert page.css(".product a::attr(href)").getall() == ["/p/1", "/p/2", "/p/3#reviews"]
    assert page.css("#nope::text").get() is None
    assert page.css("#nope::text").get("n/a") == "n/a"


def test_xpath(page: Selector) -> None:
    assert page.xpath("//span[@class='price']/text()").getall() == ["$12.50", "$9.99", "$15.00"]
    assert page.xpath("count(//div[contains(@class, 'product')])").get() == "3"
    assert page.xpath("//div[@data-id=$id]/h2/text()", id="2").get() == "Red Mug"


def test_select_guesses_css_or_xpath(page: Selector) -> None:
    assert page.select("//h2[@class='name']/text()").get() == "Blue Mug"
    assert page.select(".name::text").get() == "Blue Mug"
    assert page.select("xpath://title/text()").get() == "Shop"


def test_invalid_selectors_raise(page: Selector) -> None:
    with pytest.raises(SelectorSyntaxError):
        page.css("div[")
    with pytest.raises(SelectorSyntaxError):
        page.xpath("//div[")
    with pytest.raises(SelectorSyntaxError):
        page.css("a::bogus")


def test_element_properties(page: Selector) -> None:
    card = page.css(".product").first
    assert card is not None
    assert card.tag == "div"
    assert card.attrib["data-id"] == "1"
    assert card["class"] == "product featured"
    assert card.attr("missing", "x") == "x"
    assert card.text == "Blue Mug $12.50View"
    assert card.own_text == ""
    assert card.html.startswith('<div class="product featured"')
    assert card.inner_html.startswith("<h2")
    assert page.css("#promo").text == "Price: $10 today"
    assert "do not show" not in page.css("body").text


def test_navigation(page: Selector) -> None:
    red = page.css(".product")[1]
    assert red.parent is not None and red.parent.attr("id") == "products"
    assert [c.tag for c in red.children] == ["h2", "span", "a"]
    assert red.next is not None and red.next.attr("data-id") == "3"
    assert red.previous is not None and red.previous.attr("data-id") == "1"
    assert len(red.siblings) == 2
    assert [a.tag for a in red.ancestors][:2] == ["div", "body"]
    assert page.css(".price")[0].closest(".product").attr("data-id") == "1"
    assert page.css(".price")[0].closest("table") is None


def test_selector_list_helpers(page: Selector) -> None:
    products = page.css(".product")
    assert len(products) == 3
    assert products.css(".name::text").getall() == ["Blue Mug", "Red Mug", "Green Mug"]
    assert products.texts[1] == "Red Mug $9.99View"
    assert products.text == "Blue Mug $12.50View"
    assert products.attr("data-id") == "1"
    assert products.attrs("data-id") == ["1", "2", "3"]
    assert products.last.attr("data-id") == "3"
    assert isinstance(products[:2], wg.SelectorList)
    assert products.filter(lambda p: "featured" in p.attr("class")).attrs("data-id") == ["1"]
    assert page.css(".nope").text is None and page.css(".nope").first is None


def test_regex(page: Selector) -> None:
    assert page.css(".price").re(r"[\d.]+") == ["12.50", "9.99", "15.00"]
    assert page.css(".price").re_first(r"\$(\d+)") == "12"
    assert page.css(".price::text").re(r"(?P<extract>\d+)\.") == ["12", "9", "15"]
    assert page.css(".nope").re_first(r"x", default="d") == "d"


def test_find_by_text_and_regex(page: Selector) -> None:
    found = page.find_by_text("red mug")
    assert [e.tag for e in found] == ["h2"]
    assert page.find_by_text("Red Mug", partial=False, case_sensitive=True).first.tag == "h2"
    assert not page.find_by_text("red mug", case_sensitive=True)
    # Text split across elements: the deepest element holding all of it wins.
    assert page.find_by_text("Price: $10").first.attr("id") == "promo"
    assert [e.text for e in page.find_by_regex(r"^\$\d+\.\d{2}$")] == ["$12.50", "$9.99", "$15.00"]


def test_find_similar(page: Selector) -> None:
    blue = page.find_by_text("Blue Mug").first.parent
    similar = blue.find_similar()
    assert [e.attr("data-id") for e in similar] == ["2", "3"]
    assert not page.css(".sidebar").first.find_similar()


def test_generated_selectors_point_back(page: Selector) -> None:
    red = page.css(".product")[1]
    assert red.css_path == "#products > div:nth-of-type(2)"
    assert page.css(red.css_path).first == red
    assert page.xpath(red.xpath_path).first == red
    h2 = page.css("aside h2").first
    assert page.css(h2.css_path).first == h2


def test_links(page: Selector) -> None:
    links = page.links()
    assert links[:2] == ["https://shop.example/", "https://shop.example/about"]
    assert "https://shop.example/p/3" in links  # fragment dropped, <base> respected
    assert not any(link.startswith("mailto:") for link in links)
    assert page.links(".product", allow=r"/p/[12]$") == ["https://shop.example/p/1", "https://shop.example/p/2"]
    assert page.links(deny="/p/") == ["https://shop.example/", "https://shop.example/about"]
    assert page.links(".product a::attr(href)")[0] == "https://shop.example/p/1"
    assert page.links(domains=["other.example"]) == []
    assert page.urljoin("x/y") == "https://shop.example/x/y"


def test_extract_schema(page: Selector) -> None:
    data = page.extract(
        {
            "title": "title",
            "names": [".name"],
            "first_price": Field(".cost", ".price", regex=r"[\d.]+", transform=float),
            "prices": Field(".price::text", many=True, transform=lambda v: float(v.strip("$"))),
            "missing": Field(".nope", default="n/a"),
            "promo": {"text": "#promo", "bold": "#promo b::text"},
            "count": lambda sel: len(sel.css(".product")),
            "first_link": Field(".product a", attr="href"),
        }
    )
    assert data == {
        "title": "Shop",
        "names": ["Blue Mug", "Red Mug", "Green Mug"],
        "first_price": 12.5,
        "prices": [12.5, 9.99, 15.0],
        "missing": "n/a",
        "promo": {"text": "Price: $10 today", "bold": "$10"},
        "count": 3,
        "first_link": "/p/1",
    }


def test_extract_all(page: Selector) -> None:
    rows = page.extract_all(".product", {"name": "h2", "price": ".price::text", "url": "a::attr(href)"})
    assert rows[0] == {"name": "Blue Mug", "price": "$12.50", "url": "/p/1"}
    assert len(rows) == 3


def test_text_selector_behaviour(page: Selector) -> None:
    text = page.css("title::text").first
    assert not text.is_element and text.tag is None
    assert text.text == text.html == text.get() == "Shop"
    assert text.css("x") == [] and text.parent is None


def test_parse_helpers_and_bad_markup() -> None:
    assert wg.parse("<p>hi").css("p::text").get() == "hi"
    assert wg.parse("").css("*")  # never raises, yields an empty document
    assert wg.parse(b"<p>bytes</p>").css("p").text == "bytes"


def test_xml_documents() -> None:
    xml = "<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'><entry><title>A</title></entry></feed>"
    doc = wg.parse(xml, type="xml")
    assert doc.xpath("//a:title/text()", namespaces={"a": "http://www.w3.org/2005/Atom"}).get() == "A"
    doc.remove_namespaces()
    assert doc.xpath("//title/text()").get() == "A"
    assert doc.css("entry > title::text").get() == "A"


def test_descendant_text_matches_parsel_semantics() -> None:
    doc = wg.parse("<div><h1 id='a'>Hello <b id='b'>big</b> world</h1></div>")
    assert doc.css("h1::text").getall() == ["Hello ", " world"]
    assert doc.css("h1 ::text").getall() == ["Hello ", "big", " world"]
    assert doc.css("h1 ::attr(id)").getall() == ["a", "b"]
    assert doc.css("h1").first.css("*::text").getall() == ["Hello ", "big", " world"]
