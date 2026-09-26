"""Page classification and technology profiling."""

from __future__ import annotations

import json

import pytest

from wintergrab.fetchers.response import Headers, Response
from wintergrab.intel import (
    PAGE_TYPES,
    PageClassifier,
    PageType,
    TechDetector,
    TechRule,
    classify_page,
    classify_url,
    detect_technologies,
)
from wintergrab.intel.tech import _literals


def page(
    body: str, head: str = "", *, url: str = "https://site.example/x", status: int = 200, headers=None
) -> Response:
    html = f"<!doctype html><html><head>{head}</head><body>{body}</body></html>"
    return Response(url, status=status, headers=headers or {"content-type": "text/html"}, body=html.encode())


def ld(data: dict) -> str:
    return f'<script type="application/ld+json">{json.dumps(data)}</script>'


LOREM = (
    "The committee met on Tuesday to discuss the new budget, which has been debated for months. "
    "Several members raised concerns about the cost of the proposed changes and their effect on services. "
)

# ---------------------------------------------------------------------------------------------- pages


def product_page(structured: bool = True) -> Response:
    head = "<title>Phone X | Acme</title>"
    if structured:
        head += ld({"@context": "https://schema.org", "@type": "Product", "name": "Phone X",
                    "offers": {"@type": "Offer", "price": "299.99", "priceCurrency": "USD"}})  # fmt: skip
    body = '<h1>Phone X</h1><p class="price">$299.99</p><button>Add to cart</button><p>A great phone.</p>'
    return page(body, head, url="https://shop.example/p/phone-x-123")


def category_page() -> Response:
    cards = "".join(
        f'<li class="card"><a href="/p/item-{i}">Item {i}</a> <span class="price">${10 + i}.99</span></li>'
        for i in range(12)
    )
    body = (
        f'<h1>Phones</h1><ul class="grid">{cards}</ul><nav class="pagination"><a rel="next" href="?page=2">2</a></nav>'
    )
    return page(body, "<title>Phones | Acme</title>", url="https://shop.example/category/phones")


def news_page() -> Response:
    head = "<title>Budget approved</title>" + ld(
        {"@context": "https://schema.org", "@type": "NewsArticle", "headline": "Budget approved"}
    )
    body = (
        '<article><h1>Budget approved</h1><p class="byline">By <a rel="author" href="/a/jo">Jo Doe</a></p>'
        '<time datetime="2024-05-01">May 1</time>' + "".join(f"<p>{LOREM}</p>" for _ in range(8)) + "</article>"
    )
    return page(body, head, url="https://news.example/2024/05/01/budget-approved")


def blog_page() -> Response:
    body = (
        '<article><h1>How we cut our build times</h1><span class="author">Sam</span><time datetime="2024-02-03">'
        "Feb 3</time>" + "".join(f"<p>{LOREM}</p>" for _ in range(8)) + "</article>"
    )
    return page(body, "<title>How we cut our build times</title>", url="https://dev.example/blog/faster-builds")


# ---------------------------------------------------------------------------------------------- classification


@pytest.mark.parametrize(
    ("make", "expected", "evidence"),
    [
        (lambda: product_page(), "product", "schema.org Product"),
        (lambda: product_page(structured=False), "product", "add-to-cart button"),
        (category_page, "category", "many prices"),
        (news_page, "news", "schema.org NewsArticle"),
        (blog_page, "article", "long article text"),
    ],
)
def test_content_pages(make, expected: str, evidence: str) -> None:
    result = classify_page(make())
    assert result.type == expected, result.scores
    assert evidence in result.evidence
    assert 0.3 < result.confidence <= 1


def test_related_types_do_not_compete() -> None:
    # A news page is also an article: "article" scoring high must not make "news" unsure.
    result = classify_page(news_page())
    assert result.scores["article"] > 0
    assert result.confidence > 0.9


def test_utility_pages() -> None:
    login = page(
        '<h1>Sign in</h1><form><input name="email"><input type="password" name="pw"><button>Log in</button></form>',
        "<title>Sign in</title>",
        url="https://app.example/login",
    )
    assert classify_page(login).type == "login"

    results = "".join(f'<div class="result"><a href="/r/{i}">Result {i}</a></div>' for i in range(10))
    search = page(
        f'<input type="search" name="q" value="phone"><h1>Results for "phone"</h1>{results}',
        url="https://shop.example/search?q=phone",
    )
    assert classify_page(search).type == "search"

    code = "<pre><code>pip install x</code></pre>" * 4
    docs = page(f"<h1>Installation</h1>{code}<p>Then import it.</p>", url="https://x.example/docs/install")
    assert classify_page(docs).type == "documentation"

    job = page(
        "<h1>Backend engineer</h1><p>Full-time. Responsibilities: build things.</p>",
        ld({"@context": "https://schema.org", "@type": "JobPosting", "title": "Backend engineer"}),
        url="https://corp.example/careers/backend",
    )
    assert classify_page(job).type == "job"

    links = "".join(f'<a href="/s/{i}">Section {i}</a> ' for i in range(40))
    home = page(f"<h1>Acme</h1><div>{links}</div>", url="https://acme.example/")
    result = classify_page(home)
    assert result.type == "homepage"
    assert "site root" in result.evidence


def test_error_pages() -> None:
    missing = page("<h1>Oops</h1><p>We moved things around.</p>", url="https://x.example/p/gone-123", status=404)
    result = classify_page(missing)
    assert result.type == "error"
    assert "not-found status" in result.evidence
    # the status can be given for HTML text too, and the wording alone is enough
    assert classify_page("<h1>Oops</h1>", url="https://x.example/a", status=404).type == "error"
    soft = page("<h1>Page not found</h1><p>Sorry.</p>", "<title>Page not found</title>", url="https://x.example/a")
    assert classify_page(soft).type == "error"


def test_unknown_when_nothing_is_convincing() -> None:
    result = classify_page(page("<p>hello</p>", url="https://x.example/misc/thing"))
    assert result.type == "unknown"
    assert result.confidence == 0
    assert result.evidence == []
    assert "unknown" not in PAGE_TYPES


def test_custom_rules_and_broken_rules() -> None:
    classifier = PageClassifier()
    classifier.add_rule(
        "recipe", "recipe card", lambda f: (5.0, "a recipe card") if f.page.root.css(".recipe") else None
    )
    classifier.add_rule("product", "broken", lambda f: 1 / 0)  # a failing rule is skipped
    classifier.add_rule(
        "*", "several", lambda f: [("recipe", 1.0, "ingredients list")] if f.page.root.css(".ingredients") else None
    )
    result = classifier.classify(page('<div class="recipe"><ul class="ingredients"><li>Flour</li></ul></div>'))
    assert result.type == "recipe"
    assert result.evidence == ["a recipe card", "ingredients list"]
    assert result.scores["recipe"] == 6.0


def test_classify_url() -> None:
    assert classify_url("https://shop.example/products/phone-x").type == "product"
    assert classify_url("https://shop.example/search?q=phone").type == "search"
    assert classify_url("https://corp.example/careers/").type == "job"
    assert classify_url("https://acme.example/").type == "homepage"
    assert classify_url("https://acme.example/en/").type == "homepage"
    assert classify_url("https://acme.example/login").type == "login"
    assert classify_url("https://x.example/misc/thing").type == "unknown"
    # the URL alone is weaker evidence than the page
    assert classify_url("https://shop.example/p/phone-x-123").confidence < classify_page(product_page()).confidence


def test_page_type_output() -> None:
    result = classify_page(product_page())
    data = result.to_dict()
    assert data["type"] == "product"
    assert data["evidence"] == result.evidence
    assert str(result).startswith("product (")
    assert (
        repr(PageType("login", 0.5, ["password field"]))
        == "PageType('login', confidence=0.5, evidence=['password field'])"
    )


# ---------------------------------------------------------------------------------------------- technologies


def names(found) -> dict:
    return {t.name: t for t in found}


def test_wordpress_stack_from_a_response() -> None:
    head = (
        '<meta name="generator" content="WordPress 6.4.2">'
        '<link rel="stylesheet" href="/wp-content/plugins/woocommerce/assets/css/woocommerce.css?ver=8.4.0">'
        '<script src="/wp-includes/js/jquery/jquery.min.js?ver=3.7.1"></script>'
        '<script src="https://js.stripe.com/v3/"></script>'
    )
    headers = Headers(
        [
            ("Server", "cloudflare"),
            ("CF-RAY", "8a1b2c3d4e5f-AMS"),
            ("X-Powered-By", "PHP/8.2.1"),
            ("Set-Cookie", "__cf_bm=abc; path=/"),
            ("Set-Cookie", "PHPSESSID=x; path=/"),
        ]
    )
    found = names(detect_technologies(page('<div class="woocommerce-page"></div>', head, headers=headers)))
    wordpress = found["WordPress"]
    assert wordpress.version == "6.4.2"
    assert wordpress.category == "cms"
    assert "meta generator: WordPress 6.4.2" in wordpress.evidence
    assert found["jQuery"].version == "3.7.1"  # WordPress's ?ver= on its bundled jQuery
    assert found["PHP"].version == "8.2.1"
    assert found["Cloudflare"].confidence == 0.99
    assert "cookie __cf_bm" in found["Cloudflare"].evidence
    assert found["Stripe"].category == "payment"
    # WooCommerce implies WordPress (already seen), WordPress implies PHP and MySQL
    assert "implied by WooCommerce" in wordpress.evidence
    assert found["MySQL"].evidence == ["implied by WordPress"]
    assert found["MySQL"].confidence == round(wordpress.confidence * 0.5, 3)


def test_best_first_and_serialisable() -> None:
    found = detect_technologies(
        page("", '<script src="https://cdn.shopify.com/s/files/theme.js"></script>', headers={"x-shopid": "42"})
    )
    assert found[0].name == "Shopify"
    assert found[0].confidence == round(1 - 0.1 * 0.2, 3)  # a header and a script
    data = found[0].to_dict()
    assert data["name"] == "Shopify"
    assert data["evidence"] == ["header x-shopid", "asset https://cdn.shopify.com/s/files/theme.js"]
    assert "version" not in data
    confidences = [t.confidence for t in found]
    assert confidences == sorted(confidences, reverse=True)


def test_html_markers_are_case_insensitive() -> None:
    upper = page(
        '<DIV ID="app" NG-VERSION="17.0.4"></DIV><SCRIPT ID="__NEXT_DATA__" TYPE="application/json">{}</SCRIPT>'
    )
    found = names(detect_technologies(upper))
    assert found["Angular"].version == "17.0.4"
    assert "Next.js" in found
    assert found["React"].evidence == ["implied by Next.js"]


def test_markers_need_the_whole_pattern() -> None:
    # the literal is there, the pattern is not: no detection
    found = names(detect_technologies(page("<p>data-v- and svelte- are prefixes</p>")))
    assert "Vue.js" not in found
    assert "Svelte" not in found
    found = names(detect_technologies(page('<div data-v-1a2b3c4d class="x svelte-abc123"></div>')))
    assert {"Vue.js", "Svelte"} <= set(found)


def test_positions_survive_characters_that_lengthen_when_lower_cased() -> None:
    # "İ".lower() is two characters: the page and its lower-cased copy no longer line up
    found = names(detect_technologies(page("İ" * 50 + '<div data-v-0123abcd></div><p class="woocommerce-cart"></p>')))
    assert {"Vue.js", "WooCommerce"} <= set(found)


def test_one_asset_counts_once() -> None:
    # both htmx script patterns match this URL: it is still one observation
    found = names(
        detect_technologies(page("", '<script src="https://unpkg.com/htmx.org@1.9.10/dist/htmx.min.js"></script>'))
    )
    assert found["htmx"].confidence == 0.8
    assert len(found["htmx"].evidence) == 1


def test_html_text_headers_cookies_and_url() -> None:
    found = names(
        detect_technologies(
            "<p>hi</p>", url="https://acme.myshopify.com/", headers={"X-Powered-By": "Express"}, cookies=["csrftoken"]
        )
    )
    assert "url https://acme.myshopify.com/" in found["Shopify"].evidence
    assert found["Express"].evidence == ["header x-powered-by: Express"]
    assert found["Node.js"].evidence == ["implied by Express"]
    assert found["Django"].evidence == ["cookie csrftoken"]
    assert found["Python"].evidence == ["implied by Django"]
    assert detect_technologies("<p>plain</p>") == []


def test_custom_fingerprints() -> None:
    acme = TechRule(
        "Acme CMS",
        "cms",
        headers={"x-acme": r"(?P<version>\d+(?:\.\d+)+)"},
        html=(r"\bacme-block\b",),
        implies=("Acme Runtime",),
    )
    detector = TechDetector(extra=[acme])
    found = names(detector.detect(page('<div class="acme-block"></div>', headers={"X-Acme": "2.1"})))
    assert found["Acme CMS"].version == "2.1"
    assert found["Acme CMS"].confidence == round(1 - 0.1 * 0.4, 3)
    assert found["Acme Runtime"].category == "other"  # implied, and unknown to the rules
    only = TechDetector(rules=[acme])
    assert [t.name for t in only.detect(page("", headers={"server": "cloudflare", "x-acme": "1.0"}))] == [
        "Acme CMS",
        "Acme Runtime",
    ]


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        (r"\bShopify\.theme\b", ("shopify.theme", ["shopify.theme"])),
        (r"<link[^>]+rel=.https://api\.w\.org/", ("<link", ["https://api.w.org/", "<link", "rel="])),
        (r"\bclass=\"[^\"]*\bsvelte-[a-z0-9]{6,}\b", ('class="', ['class="', "svelte-"])),
        (r"abc|def", (None, [])),  # alternatives: nothing is required
        (r"(?:abc|def)ghij", (None, ["ghij"])),
        (r"(?i)abcdef", (None, [])),  # inline flags change the meaning
        (r"\x41bcdef", (None, [])),
        (r"abc?defg", (None, ["defg"])),  # "c" is optional: "ab" is too short to count
        (r"abc+defg", ("abc", ["defg", "abc"])),
        (r"^WordPress", (None, ["wordpress"])),
        (r"x{2}yyy", (None, ["yyy"])),
        (r"prefix[]x]suffix", ("prefix", ["prefix", "suffix"])),
        (r"end\.", ("end.", ["end."])),
        (r"ab", (None, [])),
    ],
)
def test_pattern_literals(pattern: str, expected) -> None:
    assert _literals(pattern) == expected


def test_builtin_markers_have_literals() -> None:
    # every built-in HTML pattern can be skipped cheaply on pages without its literal
    for rule in TechDetector().rules:
        for pattern in rule.html:
            assert _literals(pattern)[1], (rule.name, pattern)


# ---------------------------------------------------------------------------------------------- site profiles


def _wp_product(i: int) -> Response:
    head = (
        f'<meta name="generator" content="WordPress 6.4.2"><link rel="canonical" href="/p/{i}">'
        f'<link rel="alternate" hreflang="de-DE" href="https://shop.example/de/p/{i}">'
        f'<link rel="alternate" type="application/json" href="/wp-json/wp/v2/product/{i}">'
        '<meta property="og:locale" content="en_GB">'
    )
    head += ld({"@context": "https://schema.org", "@type": "Product", "name": f"P{i}",
                "offers": {"@type": "Offer", "price": "9.99", "priceCurrency": "GBP"}})  # fmt: skip
    body = (
        f"<h1>Product {i}</h1><p class='price'>£9.99</p><button>Add to cart</button>"
        "<a href='/category/phones'>Phones</a><a href='https://facebook.com/shop'>fb</a>"
        "<div data-endpoint='/api/stock?sku=1'></div>"
        "<script>fetch('/api/reviews?product=1&page=2').then(r => r.json());"
        "axios.post('/api/cart', {id: 1}); $.getJSON('https://shop.example/api/prices');"
        "var img = '/static/logo.png'; var q = '/graphql';</script>"
    )
    html = f"<!doctype html><html lang='en-GB'><head>{head}</head><body>{body}</body></html>"
    headers = {"content-type": "text/html", "server": "nginx", "content-language": "en"}
    response = Response(f"https://shop.example/p/{i}", body=html.encode(), headers=headers, elapsed=0.2)
    return response


def test_site_profile() -> None:
    from wintergrab.fetchers.browser import CapturedResponse
    from wintergrab.intel import SiteProfiler

    profiler = SiteProfiler()
    for i in (101, 102, 103):
        profiler.observe(_wp_product(i))
    shell = Response(
        "https://shop.example/app",
        body=b"<html><head><script src='/a.js'></script></head><body><div id='root'></div></body></html>",
        headers={"content-type": "text/html"},
    )
    shell.captured = [
        CapturedResponse(
            "https://shop.example/api/v2/items?page=1", "GET", 200, {"content-type": "application/json"}, b"[]"
        )
    ]
    profiler.observe(shell)
    profiler.observe(Response("https://shop.example/gone", status=404, body=b"<h1>Not found</h1>"))
    profiler.observe(Response("https://shop.example/slow", status=429, body=b"slow down"))
    profiler.add_robots("User-agent: *\nDisallow: /admin\nCrawl-delay: 2\nSitemap: https://shop.example/sitemap.xml\n")
    profile = profiler.profile()

    assert profile.domains == ["shop.example"] and profile.pages == 6
    assert profile.statuses == {200: 4, 404: 1, 429: 1} and profile.error_rate == round(2 / 6, 4)
    assert profile.average_latency == 0.2
    names = {tech["name"]: tech for tech in profile.technologies}
    assert names["WordPress"]["version"] == "6.4.2" and names["WordPress"]["pages"] == 3
    assert profile.languages == {"en": 3} and profile.regions[:2] == ["GB", "DE"]
    assert profile.page_types["product"] == 3
    [products, *_] = profile.templates
    assert (products.pattern, products.pages, products.page_type) == ("/p/{id}", 3, "product")
    assert profile.structured_data["Product"] == 3 and profile.structured_data["OpenGraph"] == 3
    assert profile.external_domains == {"facebook.com": 1} and profile.internal_links == 1
    endpoints = {(e.method, e.url): e for e in profile.endpoints}
    assert endpoints[("GET", "https://shop.example/api/v2/items?page=")].source == "captured"
    linked = endpoints[("GET", "https://shop.example/wp-json/wp/v2/product/{id}")]  # ids generalized
    assert (linked.source, linked.pages) == ("link", 3)
    assert endpoints[(None, "https://shop.example/api/reviews?page=&product=")].pages == 3  # fetch(): method unknown
    assert endpoints[("POST", "https://shop.example/api/cart")].source == "script"
    assert ("GET", "https://shop.example/api/prices") in endpoints
    assert (None, "https://shop.example/api/stock?sku=") in endpoints  # a data- attribute
    assert (None, "https://shop.example/graphql") in endpoints
    assert not any("logo.png" in url for _, url in endpoints)  # assets are not APIs
    platform = endpoints[("GET", "https://shop.example/wp-json/")]
    assert platform.source == "platform" and platform.note == "WordPress REST API"
    crawl = profile.crawlability
    assert crawl["canonical_pages"] == 3 and crawl["js_required_pages"] == 1 and crawl["blocked_pages"] == 1
    assert crawl["robots"] == {"found": True, "disallow_all": False, "crawl_delay": 2.0, "sitemaps": 1}
    text = profile.describe()
    assert "technologies: WordPress 6.4.2 (cms)" in text and "/p/{id}" in text and "crawl-delay 2" in text
    assert "POST https://shop.example/api/cart" in text
    assert profile.to_dict()["templates"][0]["pattern"] == "/p/{id}"


def test_spiders_and_inspect_build_profiles(site, tmp_path, capsys) -> None:
    from wintergrab.cli import QuickSpider, main

    saved = tmp_path / "profile.json"
    spider = QuickSpider(start_urls=[site.url + "/"], max_pages=8, profile=str(saved), log_level=None, progress=False,
                         autothrottle=False, output=None, keep_items=False)  # fmt: skip
    result = spider.run(resume=False)
    profile = result.profile
    assert profile.pages >= 8 and profile.crawlability["robots"]["found"] is True
    assert any(t.pattern == "/products/page/{id}" for t in profile.templates)
    assert json.loads(saved.read_text(encoding="utf-8"))["pages"] == profile.pages

    assert main(["inspect", site.url + "/", "--pages", "12", "-o", str(tmp_path / "inspect.json")]) == 0
    out = capsys.readouterr().out
    assert "templates:" in out and "sitemaps:" in out and "robots.txt allows crawling" in out
    data = json.loads((tmp_path / "inspect.json").read_text(encoding="utf-8"))
    assert data["sitemaps"]["pages"] > 0 and data["pages"] <= 12
    assert main(["inspect", site.url + "/", "--pages", "3", "--json", "--no-sitemaps"]) == 0
    assert json.loads(capsys.readouterr().out)["sitemaps"] is None


# ------------------------------------------------------------------------------------------- topology
MENU = """<header><nav><ul>
<li><a href="/products">Products</a><ul>
  <li><a href="/products/phones">Phones</a></li><li><a href="/products/laptops">Laptops</a></li></ul></li>
<li><a href="/blog">Blog</a></li>
<li><button>Company</button><ul><li><a href="/about">About us</a></li><li><a href="/contact">Contact</a></li></ul></li>
</ul></nav></header>"""


def shop_page(url: str, body: str, head: str = "") -> Response:
    return page(MENU + body, "<title>Shop</title>" + head, url=url)


def test_topology_tree_navigation_and_odd_pages() -> None:
    from wintergrab.intel import TopologyBuilder

    builder = TopologyBuilder()
    builder.add_urls([f"https://shop.example/p/{i}" for i in range(1, 40)] + ["https://shop.example/hidden-offer"])
    builder.start_urls(["https://shop.example/"])
    cards = "".join(f'<a href="/p/{i}">Item {i}</a>' for i in range(1, 30))
    crumbs = '<ol class="breadcrumbs"><a href="/">Home</a> <a href="/products">All products</a></ol>'
    pages = [
        shop_page(
            "https://www.shop.example/", cards[:200], '<link rel="alternate" type="application/rss+xml" href="/feed">'
        ),
        shop_page("https://shop.example/products/phones", crumbs + cards + '<a rel="next" href="?page=2">Next</a>'),
        shop_page("https://shop.example/p/1", f"<p>{LOREM * 2}</p>"),
        shop_page("https://shop.example/p/1-copy", f"<p>{LOREM * 2}</p>"),
        shop_page("https://shop.example/p/2", "<p>two</p>", '<link rel="canonical" href="https://shop.example/p/1">'),
    ]
    for response in pages:
        builder.observe(response)
    builder.observe(page("<h1>Landing</h1><a href='/landing'>again</a>", url="https://shop.example/landing"))
    builder.observe(page("<h1>Offer</h1><a href='/hidden-offer'>again</a>", url="https://shop.example/hidden-offer"))

    topology = builder.topology()
    assert topology.site == "shop.example" and topology.orphans is None  # the crawl was not complete
    sections = {node.path: node for node in topology.root.walk()}
    assert sections["/products"].label == "Products"  # the menu's name beats the breadcrumb's
    assert sections["/products/phones"].label == "Phones" and sections["/about"].label == "About us"
    assert sections["/p/{id}"].urls == 39 and sections["/p"].page_type == "product"
    assert [n.path for n in topology.find("phone")] == ["/products/phones"]
    assert [(e["label"], e["url"], [c["label"] for c in e["children"]]) for e in topology.navigation] == [
        ("Products", "https://shop.example/products", ["Phones", "Laptops"]),
        ("Blog", "https://shop.example/blog", []),
        ("Company", None, ["About us", "Contact"]),  # a heading that is not a link
    ]
    assert topology.feeds == ["https://www.shop.example/feed"]
    assert topology.paginated == {"/products/phones": 1}
    # pages whose only link is to themselves lead nowhere
    assert topology.dead_ends == ["https://shop.example/landing", "https://shop.example/hidden-offer"]
    assert topology.duplicates == [
        {
            "reason": "same text",
            "urls": ["https://shop.example/p/1", "https://shop.example/p/1-copy"],
            "canonical": None,
        },
        {"reason": "canonical link", "urls": ["https://shop.example/p/2"], "canonical": "https://shop.example/p/1"},
    ]
    assert topology.counts["visited"] == 7 and topology.counts["listed"] == 40

    complete = builder.topology(complete=True)
    # listed, and linked from no page but itself; the start page is reached without a link
    assert complete.orphans == ["https://shop.example/hidden-offer"] + [
        f"https://shop.example/p/{i}" for i in range(30, 40)
    ]
    assert complete.counts["orphans"] == 11
    text = complete.render()
    assert text.splitlines()[0] == "shop.example  (50 URLs, 7 visited)"  # listed, visited or linked
    assert "├── Products  /products  (4, product)" in text and "│   ├── Phones  /products/phones  (2" in text
    summary = "\n".join(complete.summary())
    assert "navigation: Products (Phones, Laptops), Blog, Company (About us, Contact)" in summary
    assert "orphans: 11 pages listed in sitemaps and linked from no page visited" in summary
    assert json.loads(json.dumps(complete.to_dict()))["root"]["urls"] == 50


def test_topology_clusters_items_but_keeps_sections() -> None:
    from wintergrab.intel import TopologyBuilder

    builder = TopologyBuilder()
    urls = [f"https://shop.example/p/item-{i}" for i in range(3000)]
    urls += [f"https://shop.example/p/item-{i}/reviews" for i in range(2000)]
    urls += [
        f"https://shop.example/blog/{year}/{month:02d}/post-{i}"
        for year in (2024, 2025)
        for month in range(1, 13)
        for i in range(3)
    ]
    urls += [f"https://shop.example/t/thread-{i}/{n}" for i in range(80) for n in range(1, 20)]
    urls += [f"https://shop.example/post-{i}" for i in range(300)] + ["https://shop.example/about"]
    urls += ["https://blog.shop.example/a", "https://blog.shop.example/b/c", "https://blog.shop.example/b/d"]
    builder.add_urls(urls)
    topology = builder.topology()
    tops = {node.path: node for node in topology.root.children}
    # many unnamed paths side by side are items: one cluster, whose sub-paths add up
    assert tops["/p"].children[0].path == "/p/{slug}" and tops["/p"].children[0].urls == 5000
    assert [(n.path, n.urls) for n in tops["/p"].children[0].children] == [("/p/{slug}/reviews", 2000)]
    assert tops["/t"].children[0].children[0].path == "/t/{slug}/{id}"  # numbers are ids
    assert tops["/blog"].urls == 72 and tops["/blog"].children[0].path == "/blog/{id}"  # a section all the same
    assert tops["/{slug}"].urls == 301 and tops["/{slug}"].page_type is None  # one /about is not a "company" section
    assert tops["/p"].page_type == "product" and tops["/p"].types["product"] == 5000
    assert [(h.label, h.urls) for h in topology.hosts] == [("blog.shop.example", 3)]
    assert len(json.dumps(topology.to_dict())) < 10_000

    wide = TopologyBuilder()
    wide.add_urls(f"https://docs.example/{section}/{page}" for section in range(70) for page in ("a", "b"))
    wide.add_urls(["https://docs.example/start"])
    root = wide.topology().root
    assert len(root.children) == 1 and root.children[0].path == "/{id}"  # 70 numbered sections are one pattern

    # sections the site names are kept, however many; 50 per section are listed, the rest counted
    menu = "".join(f'<nav><a href="/topic-{i}/intro">Topic {i}</a></nav>' for i in range(60))
    named = TopologyBuilder()
    named.observe(page(menu, url="https://docs.example/"))
    root = named.topology().root
    assert len(root.children) == 50 and (root.other_sections, root.other_urls) == (10, 10)
    assert root.children[0].children[0].label == "Topic 0"
    assert "└── ... 10 more section(s) (10 URLs)" in named.topology().render(width=50)

    empty = TopologyBuilder().topology(complete=True)  # a crawl that saw no page (only a sitemap index)
    assert (empty.site, empty.root.urls, empty.orphans) == ("", 0, None)
    assert empty.render() == "site  (0 URLs, 0 visited)" and empty.summary() == []

    small = TopologyBuilder(max_urls=3)
    small.observe(page(menu, url="https://docs.example/"))
    assert small.topology().counts == {
        "urls": 3, "listed": 0, "visited": 1, "linked": 2, "dead_ends": 0, "duplicates": 0, "truncated": 1
    }  # fmt: skip


def test_crawl_profile_and_inspect_show_the_topology(site, tmp_path, capsys) -> None:
    from wintergrab.cli import main

    saved = tmp_path / "site.json"
    args = ["-q", "crawl", site.url + "/", "--sitemap", site.url + "/sitemap_index.xml", "--follow", "a"]
    assert main([*args, "--profile", str(saved), "-o", str(tmp_path / "pages.jsonl"), "--no-autothrottle"]) == 0
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["sitemaps"]["sitemaps"] == 3 and data["sitemaps"]["pages"] == 8  # the .xml.gz one too
    topology = data["topology"]
    # a crawl that ran to the end: pages only a sitemap lists (and that only link to themselves) are orphans
    assert topology["orphans"] == [site.url + f"/item/{i}" for i in range(3)]
    assert topology["paginated"]["/products"] == 4 and topology["navigation"][0]["label"] == "Quotes"
    tops = {node["path"]: node for node in topology["root"]["children"]}
    assert tops["/product"]["urls"] == 20 and tops["/product"]["page_type"] == "product"
    capsys.readouterr()

    # a crawl resumed after a limit has only the last run's pages in its profile: no orphans then
    first = [*args, "--profile", str(saved), "--crawl-dir", str(tmp_path / "crawl"), "-o", str(tmp_path / "p.jsonl")]
    assert main([*first, "--max-pages", "1"]) == 0  # the sitemap index only: its sitemaps come after the resume
    assert json.loads(saved.read_text(encoding="utf-8"))["topology"]["orphans"] is None  # stopped early
    assert main(first) == 0
    resumed = json.loads(saved.read_text(encoding="utf-8"))
    assert resumed["sitemaps"]["pages"] == 8 and resumed["topology"]["orphans"] is None
    capsys.readouterr()

    assert main(["inspect", site.url + "/", "--pages", "10", "--depth", "1"]) == 0
    out = capsys.readouterr().out
    assert "sections:" in out and "├── Product  /product  (" in out and "{id}  /product/{id}" not in out  # depth 1
    assert "paginated listings: /products" in out
