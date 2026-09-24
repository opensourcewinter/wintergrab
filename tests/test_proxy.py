from __future__ import annotations

import time

import pytest

import wintergrab as wg
from wintergrab import ProxyRotator
from wintergrab.proxy import normalize_proxy, proxy_for_playwright, proxy_label


def test_normalize_proxy_formats() -> None:
    assert normalize_proxy("1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert normalize_proxy("1.2.3.4:8080:user:pw") == "http://user:pw@1.2.3.4:8080"
    assert normalize_proxy("socks5://h:1080") == "socks5://h:1080"
    assert proxy_label("http://u:secret@h:1") == "http://u:***@h:1"
    assert proxy_for_playwright("http://u:p%40ss@h:3128") == {
        "server": "http://h:3128",
        "username": "u",
        "password": "p@ss",
    }


def test_round_robin_and_strategies() -> None:
    rotator = ProxyRotator(["a:1", "b:1", "c:1"])
    assert [rotator.next() for _ in range(4)] == ["http://a:1", "http://b:1", "http://c:1", "http://a:1"]
    least = ProxyRotator(["a:1", "b:1"], strategy="least_used")
    picks = [least.next() for _ in range(4)]
    assert picks.count("http://a:1") == picks.count("http://b:1") == 2
    assert ProxyRotator(["a:1"], strategy="random").next() == "http://a:1"
    with pytest.raises(ValueError):
        ProxyRotator([])
    with pytest.raises(ValueError):
        ProxyRotator(["a:1"], strategy="nope")


def test_failing_proxies_are_benched() -> None:
    rotator = ProxyRotator(["a:1", "b:1"], max_failures=2, cooldown=0.2)
    rotator.report_failure("http://a:1")
    rotator.report_failure("http://a:1")
    assert {rotator.next() for _ in range(4)} == {"http://b:1"}
    time.sleep(0.25)
    assert "http://a:1" in {rotator.next() for _ in range(4)}
    rotator.report_success("http://a:1")
    stats = {s["proxy"]: s for s in rotator.stats()}
    assert stats["http://a:1"]["failures"] == 2 and stats["http://a:1"]["successes"] == 1


def test_all_benched_still_returns_a_proxy() -> None:
    rotator = ProxyRotator(["a:1"], max_failures=1, cooldown=60)
    rotator.report_failure("http://a:1")
    assert rotator.next() == "http://a:1"


def test_from_file(tmp_path) -> None:
    path = tmp_path / "proxies.txt"
    path.write_text("# comment\n1.1.1.1:80\n\nsocks5://2.2.2.2:1080\n")
    assert ProxyRotator.from_file(path).proxies == ["http://1.1.1.1:80", "socks5://2.2.2.2:1080"]


def test_requests_go_through_rotating_proxies(site, proxies) -> None:
    rotator = ProxyRotator([p.url for p in proxies])
    with wg.Fetcher(proxies=rotator) as fetcher:
        seen = [fetcher.get(site.url + "/product/1").headers.get("x-via-proxy") for _ in range(4)]
    assert seen == ["p1", "p2", "p1", "p2"]


def test_broken_proxy_is_retried_through_another(site, proxies) -> None:
    proxies[0].broken = True
    rotator = ProxyRotator([p.url for p in proxies], max_failures=1, cooldown=60)
    with wg.Fetcher(proxies=rotator, retries=2, backoff=0.01) as fetcher:
        first = fetcher.get(site.url + "/product/1")
        assert first.headers.get("x-via-proxy") == "p2"
        assert proxies[0].used == 1
        # p1 answered 502 (a gateway error) once and is now benched
        assert [fetcher.get(site.url + "/product/2").headers.get("x-via-proxy") for _ in range(3)] == ["p2"] * 3
        assert proxies[0].used == 1


def test_single_proxy_argument(site, proxies) -> None:
    page = wg.get(site.url + "/product/1", proxy=proxies[1].url)
    assert page.headers["X-Via-Proxy"] == "p2"
