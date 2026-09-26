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

## Site profiles

A `SiteProfile` sums up a whole site from its pages:

```bash
wintergrab inspect https://shop.example                 # robots.txt, sitemaps, and 30 pages
wintergrab inspect https://shop.example --pages 100 -o shop.profile.json
wintergrab inspect https://app.example --browser        # render pages, record their XHR/fetch calls
```

```
shop.example: 11 pages (10 ok, errors: 404 x1), 0.21 s per response
technologies: WordPress 6.4.2 (cms), MySQL (database), PHP (language), nginx (web-server)
languages: en (10); regions: GB, DE
page types: product 8, category 2
templates:
  /p/{id}                         8 pages  product
  /category/phones/page/{id}      2 pages  category
structured data: Offer 8, Product 8, OpenGraph 8
links: 15 internal, 1 external (facebook.com 1)
APIs:
  GET https://shop.example/wp-json/wp/v2/product/{id}  (link, 8 page(s))
  POST https://shop.example/api/cart  (script, 8 page(s))
  GET https://shop.example/api/prices  (script, 8 page(s))
  https://shop.example/api/reviews?page=&product=  (script, 8 page(s))
  https://shop.example/api/stock?sku=  (script, 8 page(s))
  https://shop.example/graphql  (script, 8 page(s))
  GET https://shop.example/wp-json/  (platform, WordPress REST API)
sitemaps: 3 (1 index), 1,000 pages listed, 90% with lastmod
crawlability: robots.txt allows crawling, crawl-delay 1; 8 page(s) with a canonical link
```

(The profile of eleven generated pages of a WordPress shop, with a
robots.txt and sitemaps. On a live site, `inspect` visits the start page and
a sample spread across the sitemaps, so that more templates show up.)

| Part | From |
|---|---|
| technologies | [detection](#technologies) on every page, with the number of pages each was seen on |
| languages, regions | `<html lang>`, `Content-Language`, `hreflang` alternates, `og:locale`, a country-code domain |
| page types | [classification](#page-types) of every page |
| templates | pages grouped by layout (a SimHash of their tag structure), each with its URL pattern (`/p/{id}`) and main page type |
| structured data | schema.org types, and OpenGraph, per page |
| links | distinct internal and external links, and the external domains linked most |
| APIs | calls in inline scripts (`fetch(...)`, axios, jQuery, `xhr.open(...)`), API-looking paths (`/api/`, `/graphql`, `/wp-json/`...), `data-endpoint`-style attributes, JSON alternate links, calls recorded by a browser (`--browser`), and the detected platform's conventional endpoints (marked `platform`: not requested). Ids in paths and query values are generalized (`/product/{id}`, `?page=`) |
| sitemaps | how many, how many indexes, pages listed, share with `lastmod` |
| crawlability | robots.txt (allowed, crawl-delay), `noindex` pages, canonical links, pages that need JavaScript (little text and an app shell), 403/429 answers, bot protection seen (reCAPTCHA, hCaptcha, Turnstile) |
| latency, errors | average response time, statuses, error rate |
| change frequency | with a [history](history.md): the median change rate of the pages |
| topology | the site's [sections, navigation and odd pages](#site-topology) |

In code:

```python
from wintergrab.intel import SiteProfiler

profiler = SiteProfiler()                 # SiteProfiler(detailed=500): full analysis for the first 500 pages
for response in responses:
    profiler.observe(response)
profiler.add_robots(robots_txt)           # optional: what robots.txt says
profile = profiler.profile()
profile.to_dict()                         # everything, as JSON-ready data
```

A spider builds one with `profile = True` (`result.profile`), or
`profile = "site.json"` to save it too (`wintergrab crawl ... --profile
site.json`). Every page counts for statuses, latency, links, endpoints and
the topology; the full analysis (page type, technologies, layout,
structured data) runs on the first `detailed` pages (500). On one core, an
11 KB product page costs 6.9 ms with the full analysis and 2.3 ms after
that, parsing included (`SiteProfiler` columns of
`benchmarks/bench_pages.py`). Sitemaps and feeds that a crawl fetches
(`sitemap_urls`) are read into the profile as well.

## Site topology

The profile's `topology` says how the site is organized: its sections as a
tree, its main navigation, and its odd pages.

```
shop.example  (361 URLs, 11 visited)
├── P  /p  (301, product)
│   └── {slug}  /p/{slug}  (301, product)
├── Journal  /blog  (49, article)
│   └── {id}  /blog/{id}  (48, article)
│       └── {id}  /blog/{id}/{id}  (48, article)
├── C  /c  (6, category)
│   ├── Phones  /c/phones  (5, category)
│   │   ├── Android  /c/phones/android  (1, category)
│   │   └── iPhone  /c/phones/iphone  (1, category)
│   └── Laptops  /c/laptops  (1, category)
└── Help  /help  (2)
    ├── Returns  /help/returns  (1)
    └── Shipping  /help/shipping  (1)
navigation: Phones (Android, iPhone), Laptops, Journal, Help (Shipping, Returns)
feeds: https://shop.example/blog/feed
HTML sitemaps: https://shop.example/sitemap.html
paginated listings: /c/phones (2)
dead ends: 1 page linking nowhere on the site (https://shop.example/spring-sale)
duplicate routes: 2 group(s), such as https://shop.example/p/phone-1 = https://shop.example/p/phone-1-black (same text)
orphans: 1 page listed in sitemaps and linked from no page visited (https://shop.example/spring-sale)
```

(Eleven generated pages of a shop and a sitemap of 350 URLs, with
`complete=True`.)

- **The tree** is made of the paths of every URL known: listed in a
  sitemap, visited, or linked from a visited page. Numbers and hex ids are
  generalized (`/blog/{id}`), and more than 50 unnamed paths side by side
  are items rather than sections: they become one cluster, `/p/{slug}`,
  whose sub-paths add up (`/p/{slug}/reviews`). A path much bigger than its
  siblings (ten URLs or more, five times the median) stays a section. Each
  section has the number of URLs under it and its page type, when a quarter
  of them or more have it (read from the pages that were classified, guessed
  from the URLs otherwise). Names come from the site's menus and
  breadcrumbs (links and schema.org `BreadcrumbList` data), or from the
  path (`/help` is "Help"). The site's other hosts (`blog.shop.example`)
  get their own trees.
- **navigation**: the entries of `<nav>`, `<header>` and
  `role="navigation"` menus found on at least half the pages, in page
  order, with their submenus (`Help` is a heading that is not a link).
- **feeds** (RSS and Atom links), **HTML sitemaps** (a page named like a
  site map that links to 30 or more pages), **paginated listings** (the
  sections whose pages have a "next page" link).
- **dead ends**: pages with no link to another page of the site (their
  links are built by JavaScript, or they have none).
- **duplicate routes**: pages with the same visible text (200 characters or
  more, whitespace aside) at several URLs, and pages whose canonical link
  names another URL.
- **orphans**: pages listed in a sitemap that no visited page links to.
  They are only reported for a crawl that ran to the end in one go (a
  spider's `profile` when the crawl finishes and was not resumed, `inspect`
  when the site has fewer pages than `--pages`), and only in terms of the
  pages the crawl visited: a crawl limited by `allow`/`deny` rules may not
  have seen the page that links to one.

`wintergrab inspect` prints the tree two levels deep (`--depth`), with
`--show` sections per level. In code:

```python
from wintergrab.intel import TopologyBuilder

builder = TopologyBuilder()                    # or profiler.topology
builder.add_urls(sitemap_urls)
for response in responses:
    builder.observe(response)                  # observe(response, page_type="product") when known
builder.start_urls(["https://shop.example/"])  # reached without a link: never orphans
topology = builder.topology(complete=True)
topology.render(depth=3, width=8)              # the tree as text
topology.find("phones")                        # sections by name or path
topology.to_dict()                             # everything, as JSON-ready data
```

It remembers 200,000 distinct URLs (`TopologyBuilder(max_urls=...)`,
about 200 bytes each); past that, new URLs are not counted and
`counts["truncated"]` says so. The tree follows the URLs: a site whose URLs
do not mirror its structure (every product at `/p/{slug}`) shows it in its
navigation instead.

## Speed

`benchmarks/bench_pages.py` (one core of a 4-vCPU cloud VM, Python 3.11,
median of 5 runs of 200 pages, parsing included):

| Page | Parse only | `classify_page` | `detect_technologies` |
|---|---:|---:|---:|
| product page with JSON-LD (11 KB) | 0.26 ms | 3.0 ms | 0.9 ms |
| category page, 60 cards (10 KB) | 0.29 ms | 2.6 ms | 0.7 ms |
| product page, 480 KB of text | 1.5 ms | 20 ms | 12 ms |

`classify_url` takes about 11 µs.
