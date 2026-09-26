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
