# Page and site intelligence

What a page is, and what a site is built with, from the page you already
fetched: no extra requests, nothing executed.

```python
from wintergrab import get
from wintergrab.intel import classify_page, detect_technologies

page = get("https://shop.example/p/phone-x-123")
classify_page(page)
# PageType('product', confidence=0.956, evidence=['schema.org Product', 'URL /p/phone-x-123',
#                                                'add-to-cart button', 'one price near the title'])
detect_technologies(page)
# [Technology('Cloudflare', 'cdn', confidence=0.99, evidence=['header server: cloudflare', 'header cf-ray']),
#  Technology('WordPress', 'cms', version='6.4.2', confidence=0.99, evidence=['meta generator: WordPress 6.4.2', ...]),
#  Technology('PHP', 'language', version='8.2.1', confidence=0.95, ...),
#  Technology('WooCommerce', 'ecommerce', confidence=0.92, ...), ...]
```

Both take a `Response`, a `Selector` or HTML text (pass `url=` with HTML).

## Page types

`classify_page(page)` answers one of `PAGE_TYPES`: `product`, `category`,
`listing`, `article`, `news`, `job`, `event`, `company`, `profile`, `review`,
`directory`, `documentation`, `homepage`, `login`, `search`, `archive`,
`contact`, `error`; or `unknown` when nothing is convincing.

It adds up evidence. Each rule that applies gives its type a weight:

| Evidence | Examples | Weight |
|---|---|---:|
| schema.org types in JSON-LD or microdata | `Product`, `NewsArticle`, `JobPosting`, `SearchResultsPage` (`Organization`: 1) | 1-6 |
| `og:type` | `product`, `article`, `profile` | 3 |
| a 404 status, "Page not found" in the title | | 8, 5 |
| a password field in a small form | | 6 |
| the URL | `/p/...`, `/category/`, `/blog/...`, `/careers/`, `?q=`, the site root | 2-4 |
| layout | an add-to-cart button, many repeated cards with prices, pagination, a long `<article>`, a byline and a date, code blocks | 1-3 |
| wording | "Results for", "Sign in", "Apply now", "Get tickets" | 1.5-3 |

The weights are evidence strengths chosen by hand, not trained
probabilities: a specific, reliable signal counts more than a URL word. The
winner needs at least 2. Its confidence grows with its score and with its
margin over the best competing type:
`(1 - e^(-score/4)) * (score - runner_up) / score`.

Some types refine others instead of competing with them: `news` refines
`article`; `category`, `search`, `archive` and `directory` refine
`listing`. A refined type with evidence of its own (at least 2) also counts
its parent's, so a page with `schema.org NewsArticle` and a long article
body is `news`, while a blog post with the same body is `article`.

```python
result = classify_page(page)
result.type, result.confidence      # 'news', 0.973
result.evidence                     # ['schema.org NewsArticle', 'URL /2024/05/01/budget-approved', 'long article text', ...]
result.scores                       # {'news': 14.5, 'article': 8.5}
result.to_dict()                    # {'type': 'news', 'confidence': 0.973, 'evidence': [...]}
```

### Your own rules

A rule gets a `PageFeatures` and returns `None` when it does not apply, a
weight, `(weight, evidence text)`, or a list of `(type, weight, evidence)`:

```python
from wintergrab.intel import PageClassifier

classifier = PageClassifier()                       # the built-in rules
classifier.add_rule("recipe", "recipe card", lambda f: (5.0, "recipe card") if f.page.root.css(".recipe") else None)
classifier.add_rule("product", "SKU in the URL", lambda f: 2.0 if "/sku/" in f.path else None)
classifier.classify(page)
```

`PageFeatures` computes each fact once, when a rule first asks: `url`,
`path`, `query`, `status`, `schema_types`, `og_type`, `title`, `h1`, `text`,
`prices` (distinct prices in the text), `password_inputs`, `form_inputs`,
`cart_button`, `article_text`, `paragraphs`, `code_blocks`, `time_elements`,
`byline`, `repeated` (the largest group of similar sibling elements holding
links, outside navigation), `links`, `pagination`, `search_box`, and `page`
(the [`PageContext`](extraction.md), for anything else). A rule that raises
is skipped.

### Before fetching

`classify_url(url)` uses the URL rules alone. It is cheap (about 11 µs) and
less sure than `classify_page`, which makes it a good crawl priority:

```python
from wintergrab import Spider
from wintergrab.intel import classify_url

def products_first(request):
    guess = classify_url(request.url).type
    return 10 if guess == "product" else 5 if guess in ("category", "listing") else 0

class Shop(Spider):
    priority_fn = products_first
```

## Technologies

`detect_technologies(page)` matches the page against 124 built-in
fingerprints: content management systems (WordPress, Drupal, Shopify,
Magento, Wix...), JavaScript frameworks (Next.js, Nuxt, React, Vue.js,
Angular, Svelte...), analytics and tag managers, payment providers, consent
tools, CDNs and hosts (Cloudflare, Fastly, CloudFront, Vercel, Netlify...),
web servers and back-end languages.

It reads what the server sent: response headers, cookie names, `<meta>`
tags such as `generator`, the URLs of scripts and stylesheets, distinctive
HTML markers (the first 500 kB) and the page URL. Each signal found is
evidence, and they combine as `1 - (1 - p1)(1 - p2)...` (at most 0.99):

| Signal | Weight | Example |
|---|---:|---|
| header, `<meta>`, page URL | 0.9 | `server: cloudflare`, `generator: WordPress 6.4.2`, `*.myshopify.com` |
| script or stylesheet URL | 0.8 | `cdn.shopify.com/...`, `/_next/static/...` (one per asset) |
| cookie name | 0.75 | `_shopify_y`, `PHPSESSID`, `csrftoken` |
| HTML marker | 0.6 | `<script id="__NEXT_DATA__"`, `data-v-1a2b3c4d`, `ng-version="17.0.4"` |

Versions come from the signal that shows one (`generator`, `x-powered-by:
PHP/8.2.1`, `ng-version`, `jquery-3.7.1.min.js`, WordPress's `?ver=`).
Technologies imply others (WooCommerce implies WordPress, Next.js implies
React, WordPress implies PHP and MySQL); an implied one gets half its
parent's confidence and the evidence `implied by ...`, or raises the
confidence of one seen directly.

```python
tech = detect_technologies(page)[1]
tech.name, tech.category, tech.version    # 'WordPress', 'cms', '6.4.2'
tech.to_dict()                            # {'name': 'WordPress', 'category': 'cms', 'confidence': 0.99,
                                          #  'version': '6.4.2', 'evidence': [...]}
detect_technologies("<html>...</html>", url="https://example.com/", headers={"server": "nginx"})
```

### Your own fingerprints

```python
from wintergrab.intel import TechDetector, TechRule

acme = TechRule(
    "Acme CMS", "cms",
    headers={"x-acme-version": r"(?P<version>\d+(?:\.\d+)+)"},   # "" = the header is present
    cookies=(r"^acme_session$",),
    meta={"generator": r"^Acme"},
    scripts=(r"/acme-static/",),
    html=(r"\bacme-block\b",),
    implies=("PHP",),
)
detector = TechDetector(extra=[acme])        # built-in rules plus yours; rules=[...] replaces them
detector.detect(page)
```

Patterns are case-insensitive regular expressions; a named group `version`
captures a version. An HTML pattern only scans pages that contain its
literal parts (`acme-block` above), so give each a distinctive literal.

### What it cannot tell

Detection sees what a page shows. A site can hide its stack (remove the
`generator` tag, rename cookies, serve assets from its own domain) or fake
it (any header can be set), and technologies that leave no trace in the
page (databases, most back ends) are only inferred. Treat results as
evidence with a confidence, which is how they are reported.

## Speed

`benchmarks/bench_pages.py` (one core of a 4-vCPU cloud VM, Python 3.11,
median of 5 runs of 200 pages, parsing included):

| Page | Parse only | `classify_page` | `detect_technologies` |
|---|---:|---:|---:|
| product page with JSON-LD (11 KB) | 0.26 ms | 3.0 ms | 0.9 ms |
| category page, 60 cards (10 KB) | 0.29 ms | 2.6 ms | 0.7 ms |
| product page, 480 KB of text | 1.5 ms | 20 ms | 12 ms |

`classify_url` takes about 11 µs.
