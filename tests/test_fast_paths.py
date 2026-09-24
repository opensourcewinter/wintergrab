"""The hot-path shortcuts must give exactly the results of the code they bypass."""

from __future__ import annotations

import random
from urllib.parse import urljoin

import pytest

from wintergrab import Request, Selector
from wintergrab.errors import SelectorSyntaxError
from wintergrab.fetchers.blocking import has_challenge_markers
from wintergrab.parser.css import css_to_xpath
from wintergrab.utils import _canonicalize, canonicalize_url, fast_urljoin

BASES = [
    "http://example.com/a/b/c.html",
    "https://Example.com:8443/dir/",
    "http://user:pw@h.com/x?y=1#f",
    "",
    "ftp://f.com/x",
    "https://h.com",
    "http://[::1]:80/p",
    "about:blank",
    "HTTP://Upper.example/Path",
]
PIECES = [
    "/", "//", ".", "..", "/./", "/../", "?", "#", "?#", ";", ";?", "a", "B", "%2e", "%", "\\", "\t", "\n",
    " ", ":", "@", "=", "&", "x=1", "http://", "https://", "HTTP://", "h", "~", "é", "[", "]", ":80",
    ":443", ":8080", ":0", ":99999", "example.com", "EXAMPLE.com", "user@", "-", "!", "'", "*", ",",
]  # fmt: skip
STARTS = ["", "/", "http://", "https://", "https://h.com", "http://127.0.0.1:81", "/a"]


def _random_hrefs(n: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    return [rng.choice(STARTS) + "".join(rng.choice(PIECES) for _ in range(rng.randint(0, 8))) for _ in range(n)]


def _outcome(func, *args):
    try:
        return func(*args)
    except ValueError:
        return ValueError


TRICKY_HREFS = [
    "/x;", "/x;y", "/x?#f", "/x?", "/x#", "/./a", "/../a", "/a/../b", "//cdn.example/x", "http://",
    "https://", "http://\thost/", "/a\nb", "http://e.com/x;", "https://E.com/", "http://e.com/b?",
    "/catalogue/page-2.html", "https://other.example/path?q=1#top", "page-2.html", "?page=2", "#top", "",
]  # fmt: skip


@pytest.mark.parametrize("base", BASES)
def test_fast_urljoin_matches_urljoin(base: str) -> None:
    for href in TRICKY_HREFS + _random_hrefs(3000, seed=len(base)):
        expected = _outcome(urljoin, base, href)
        if expected is not ValueError:  # the shortcut may accept what urljoin rejects
            assert fast_urljoin(base, href) == expected, (base, href)


def test_canonicalize_fast_path_matches_full_normalisation() -> None:
    urls = [
        "http://a.com", "http://a.com/", "http://a.com:80/x", "https://a.com:443", "https://a.com:080/",
        "http://a.com:8080", "http://a.com:65535/p", "http://a.com:65536/", "http://a.com:0/", "HTTP://A.com/",
        "http://a.com/x?b=2&a=1", "http://a.com/x#frag", "http://a.com/a b", "http://user@a.com/",
    ]  # fmt: skip
    for href in _random_hrefs(20000, seed=3):
        urls.append(href if href.startswith("http") else "http://h.example" + href)
    for url in urls:
        assert _outcome(canonicalize_url, url) == _outcome(_canonicalize.__wrapped__, url), url


def test_class_prefilter_keeps_css_semantics() -> None:
    assert "contains(@class, 'item')" in css_to_xpath(".item")
    html = """<ul>
        <li class="item">1</li><li class="items">no</li><li class="an-item">no</li>
        <li class="a	item
        b">2</li><li class="x item">3</li><li>no</li><li class="ITEM">no</li>
        <li class='it"em'>q</li>
    </ul>"""
    sel = Selector(html)
    assert sel.css(".item::text").getall() == ["1", "2", "3"]
    assert sel.css("li.item.x::text").getall() == ["3"]
    assert sel.css(r".it\"em::text").getall() == ["q"]


def test_byte_level_challenge_check_matches_text_check() -> None:
    pages = [
        "<title>Just a moment...</title>",
        "<script src='/_Incapsula_Resource?SWJIYLWA=1'></script>",
        "<div id='PX-Captcha'></div>",
        "<p>Please VERIFY you are human</p>",
        "<html><body>" + "ordinary content " * 3000 + "</body></html>",
        "<html>" + "x" * 70_000 + "checking your browser</html>",  # beyond the scanned prefix
    ]
    for page in pages:
        assert has_challenge_markers(page.encode()) == has_challenge_markers(page), page[:40]
    assert has_challenge_markers(b"<script src='/_Incapsula_Resource?x=1'></script>")


def test_compiled_xpath_cache_respects_namespaces_variables_and_errors() -> None:
    xml = '<r xmlns:a="urn:a" xmlns:b="urn:b"><a:x>A</a:x><b:x>B</b:x><n v="1">one</n><n v="2">two</n></r>'
    sel = Selector(xml, type="xml")
    assert sel.xpath("//p:x/text()", namespaces={"p": "urn:a"}).getall() == ["A"]
    assert sel.xpath("//p:x/text()", namespaces={"p": "urn:b"}).getall() == ["B"]
    assert sel.xpath("//n[@v=$v]/text()", v="1").getall() == ["one"]
    assert sel.xpath("//n[@v=$v]/text()", v="2").getall() == ["two"]
    for _ in range(2):  # failures are not cached as successes
        with pytest.raises(SelectorSyntaxError):
            sel.xpath("//n[")


def test_request_caches_follow_changes() -> None:
    request = Request("https://A.example/x")
    assert request.host == "a.example"
    first = request.fingerprint()
    assert request.fingerprint() is first
    request.url = "https://b.example/x"
    assert request.host == "b.example"
    assert request.fingerprint() != first

    form = Request("https://a.example/", method="POST", data={"q": "1"})
    before = form.fingerprint()
    form.data["q"] = "2"  # dict bodies can change in place
    assert form.fingerprint() != before
    assert Request("https://a.example/", method="POST", data={"q": "2"}).fingerprint() == form.fingerprint()
