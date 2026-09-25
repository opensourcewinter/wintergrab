"""URL normalization, crawl rules and URL templates."""

from __future__ import annotations

import itertools
import pickle
import random

import pytest

from wintergrab import Spider, URLNormalizer, URLRules, normalize_url, url_template
from wintergrab.errors import ConfigurationError
from wintergrab.urls import remove_dot_segments


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # tracking parameters, fragment, default port, case, dot segments, query order
        (
            "HTTP://Example.COM:80/a/./b/../c?utm_source=x&b=2&a=1&gclid=zz#frag",
            "http://example.com/a/c?a=1&b=2",
        ),
        ("https://example.com", "https://example.com/"),
        ("https://example.com:443/x", "https://example.com/x"),
        ("https://example.com:8443/x", "https://example.com:8443/x"),
        # session ids in the path and the query
        ("https://example.com/p;jsessionid=ABC123?PHPSESSID=9&x=1", "https://example.com/p?x=1"),
        # escapes: upper-cased, unreserved ones decoded, unsafe characters escaped
        ("https://example.com/caf%c3%a9/%7euser/a%2fb?q=%7e", "https://example.com/caf%C3%A9/~user/a%2Fb?q=~"),
        ("https://example.com/a b/Ünï", "https://example.com/a%20b/%C3%9Cn%C3%AF"),
        # "+" and bare keys are kept as they are: servers may tell them apart
        ("https://example.com/s?q=a+b&flag", "https://example.com/s?flag&q=a+b"),
        # hashbang routes identify content on old AJAX sites
        ("https://example.com/#!/products/5", "https://example.com/#!/products/5"),
        # internationalized domain names become punycode
        ("https://bücher.example/", "https://xn--bcher-kva.example/"),
        # user info and IPv6 hosts survive
        ("https://user:pw@[::1]:8080/x", "https://user:pw@[::1]:8080/x"),
        # other schemes are left alone
        ("mailto:someone@example.com", "mailto:someone@example.com"),
    ],
)
def test_default_normalization(url: str, expected: str) -> None:
    assert normalize_url(url) == expected


def test_site_dependent_options() -> None:
    normalizer = URLNormalizer(
        strip_www=True, remove_index=True, remove_trailing_slash=True, lowercase_path=True, force_https=True
    )
    assert normalizer("http://www.Example.com/Shop/Index.html") == "https://example.com/shop"
    assert normalizer("http://www.example.com/") == "https://example.com/"
    assert URLNormalizer(strip_www=True)("https://www.co/x") == "https://www.co/x"  # "www" is the whole name here


def test_parameter_options() -> None:
    assert URLNormalizer(keep_params=["id"])("https://x.com/p?id=3&sort=asc&ref=a") == "https://x.com/p?id=3"
    assert URLNormalizer(drop_params=["Sort"])("https://x.com/p?id=3&sort=asc") == "https://x.com/p?id=3"
    assert URLNormalizer(remove_empty_params=True)("https://x.com/p?a=&b=1") == "https://x.com/p?b=1"
    assert URLNormalizer(strip_tracking=False)("https://x.com/p?utm_source=a") == "https://x.com/p?utm_source=a"
    unsorted = URLNormalizer(sort_query=False)
    assert unsorted("https://x.com/p?b=2&a=1&b=1") == "https://x.com/p?b=2&a=1&b=1"
    assert URLNormalizer(remove_fragment=False)("https://x.com/p#top") == "https://x.com/p#top"


def test_remove_dot_segments_matches_rfc3986_examples() -> None:
    assert remove_dot_segments("/a/b/c/./../../g") == "/a/g"
    assert remove_dot_segments("mid/content=5/../6") == "mid/6"
    assert remove_dot_segments("/..") == "/"
    assert remove_dot_segments("/a/..") == "/"
    assert remove_dot_segments("/a/.") == "/a/"


def test_normalization_is_idempotent_on_many_urls() -> None:
    rng = random.Random(7)
    hosts = ["Example.com", "www.shop.example", "bücher.example", "[::1]:8080", "a.b.c.example:443"]
    paths = ["", "/", "/a/./b/../c", "/x%2fy/%7e", "/café/ü", "/p;jsessionid=1", "/a b", "/index.html", "/a//b/"]
    queries = ["", "?b=2&a=1", "?q=a+b&flag", "?utm_source=x&id=%7e", "?a=&a=1&a=0", "?x=%zz"]
    fragments = ["", "#top", "#!/route"]
    normalizers = [URLNormalizer(), URLNormalizer(strip_www=True, remove_index=True, remove_trailing_slash=True)]
    combos = list(itertools.product(["http", "HTTPS"], hosts, paths, queries, fragments))
    for scheme, host, path, query, fragment in rng.sample(combos, 400):
        url = f"{scheme}://{host}{path}{query}{fragment}"
        for normalize in normalizers:
            once = normalize(url)
            assert normalize(once) == once, url


def test_normalizer_coercion_and_pickling() -> None:
    assert URLNormalizer.coerce(None) is None and URLNormalizer.coerce(False) is None
    assert isinstance(URLNormalizer.coerce(True), URLNormalizer)
    assert URLNormalizer.coerce({"strip_www": True})("https://www.a.com/") == "https://a.com/"  # type: ignore[misc]
    assert URLNormalizer.coerce(str.lower) is str.lower
    with pytest.raises(ConfigurationError):
        URLNormalizer.coerce({"no_such_option": 1})
    copy = pickle.loads(pickle.dumps(URLNormalizer(strip_www=True)))
    assert copy("https://www.a.com/x?utm_x=1") == "https://a.com/x"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://x.com/fine/page", None),
        ("https://x.com/img/a.JPG", "extension"),
        ("https://x.com/files/archive.tar.gz", "extension"),
        ("https://x.com/a/b/a/b/a/b/a/b", "repeating-path"),
        ("ftp://x.com/", "scheme"),
        ("https://x.com/" + "a" * 3000, "url-too-long"),
        ("https://x.com/" + "/".join(f"s{i}" for i in range(40)), "path-too-deep"),
        ("https://x.com/ok?" + "&".join(f"p{i}=1" for i in range(40)), "too-many-params"),
    ],
)
def test_default_rules(url: str, reason: str | None) -> None:
    assert URLRules().check(url) == reason


def test_rule_options() -> None:
    rules = URLRules(
        allow=[r"/products/"],
        deny=[r"\?sort="],
        allowed_domains=["shop.example"],
        denied_domains=["ads.shop.example"],
        deny_extensions=(),
    )
    assert rules.check("https://shop.example/products/1") is None
    assert rules.check("https://www.shop.example/products/photo.jpg") is None  # extensions allowed
    assert rules.check("https://shop.example/about") == "not-allowed"
    assert rules.check("https://shop.example/products/1?sort=asc") == "deny"
    assert rules.check("https://other.example/products/1") == "domain"
    assert rules.check("https://ads.shop.example/products/1") == "denied-domain"
    assert rules.allows("https://shop.example/products/2")
    with pytest.raises(ConfigurationError) as info:
        URLRules(deny=["(unclosed"])
    assert info.value.key == "url_rules.deny"
    assert URLRules.coerce({"deny": ["x"]}).deny == ["x"]  # type: ignore[union-attr]
    assert URLRules.coerce(None) is None


@pytest.mark.parametrize(
    ("url", "template"),
    [
        ("https://shop.example/product/123?page=2&utm_source=x", "shop.example/product/{int}?page"),
        ("https://shop.example/blog/2024-05-01/my-first-post", "shop.example/blog/{date}/{slug}"),
        ("https://shop.example/p/apple-iphone-15-pro-128gb", "shop.example/p/{slug}"),
        ("https://shop.example/about", "shop.example/about"),
        ("https://shop.example/category/new-arrivals/", "shop.example/category/new-arrivals/"),
        ("https://shop.example/u/550e8400-e29b-41d4-a716-446655440000", "shop.example/u/{uuid}"),
        ("https://shop.example/img/deadbeef1234.png", "shop.example/img/{hex}.png"),
        ("https://shop.example/item/42.html", "shop.example/item/{int}.html"),
        ("https://shop.example/", "shop.example/"),
    ],
)
def test_url_templates(url: str, template: str) -> None:
    assert url_template(url) == template


def test_url_template_variants() -> None:
    url = "https://shop.example/product/9?color=red"
    assert url_template(url, include_host=False) == "/product/{int}?color"
    assert url_template(url, include_query=False) == "shop.example/product/{int}"


def test_spider_normalizes_and_filters_discovered_links(fresh_site) -> None:
    class Links(Spider):
        log_level = None
        obey_robots_txt = False
        start_urls = [fresh_site.url + "/tracking-links?utm_campaign=start"]
        url_normalizer = True
        url_rules = True

        def parse(self, response):
            yield {"url": response.url}
            if "tracking-links" in response.url:
                yield from response.follow_all(allow=[r"/item/", r"/img/", r"/a/b/"])

    result = Links().run()
    urls = sorted(item["url"] for item in result.items)
    # The start URL lost its tracking parameter; each item was fetched once despite 4 spellings.
    assert urls[-1] == fresh_site.url + "/tracking-links"
    assert [u.rsplit("/", 2)[-2:] for u in urls[:3]] == [["item", "0"], ["item", "1"], ["item", "2"]]
    hits = fresh_site.site.hits
    assert [hits[f"/item/{i}"] for i in range(3)] == [1, 1, 1]
    assert hits["/img/photo.jpg"] == 0 and hits["/a/b/a/b/a/b/a/b"] == 0
    assert result.stats["rules_filtered/extension"] >= 1
    assert result.stats["rules_filtered/repeating-path"] >= 1
