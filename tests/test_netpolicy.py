"""Network policy (SSRF protection) and browser resource filtering."""

from __future__ import annotations

import asyncio
import logging
import sys

import pytest

import wintergrab as wg
from wintergrab import AsyncFetcher, Fetcher, NetworkPolicy, NetworkPolicyError, Spider
from wintergrab.errors import ConfigurationError, NetworkError
from wintergrab.fetchers.resources import BUILTIN_LISTS, ResourceFilter, registrable_domain
from wintergrab.netpolicy import address_category


@pytest.mark.parametrize(
    ("address", "category"),
    [
        ("8.8.8.8", "public"),
        ("1.1.1.1", "public"),
        ("127.0.0.1", "loopback"),
        ("127.255.0.9", "loopback"),
        ("10.1.2.3", "private"),
        ("172.20.0.1", "private"),
        ("172.32.0.1", "public"),
        ("192.168.1.1", "private"),
        ("100.100.100.200", "private"),  # Alibaba Cloud metadata (carrier-grade NAT range)
        ("169.254.169.254", "link_local"),  # AWS / GCP / Azure metadata
        ("192.0.0.192", "reserved"),  # Oracle Cloud metadata
        ("0.0.0.0", "unspecified"),
        ("224.0.0.1", "multicast"),
        ("255.255.255.255", "reserved"),
        ("198.18.0.1", "reserved"),
        ("::1", "loopback"),
        ("::", "unspecified"),
        ("::ffff:127.0.0.1", "loopback"),  # IPv4-mapped
        ("::ffff:8.8.8.8", "public"),
        ("64:ff9b::a00:1", "private"),  # NAT64 of 10.0.0.1
        ("2002:7f00:1::1", "loopback"),  # 6to4 of 127.0.0.1
        ("fd00:ec2::254", "private"),  # AWS IPv6 metadata
        ("fe80::1", "link_local"),
        ("fe80::1%eth0", "link_local"),
        ("2606:4700:4700::1111", "public"),
        ("2001:db8::1", "reserved"),
        ("ff02::1", "multicast"),
    ],
)
def test_address_categories(address: str, category: str) -> None:
    assert address_category(address) == category


def test_policy_switches() -> None:
    public = NetworkPolicy.public()
    assert public.address_reason("8.8.8.8") is None
    assert public.address_reason("10.0.0.1") == "10.0.0.1 is a private address"
    assert public.address_reason("169.254.169.254") == "169.254.169.254 is a link-local address"
    assert NetworkPolicy(allow_private=True).address_reason("10.0.0.1") is None
    assert NetworkPolicy(allow_private=True).address_reason("127.0.0.1") is not None
    assert NetworkPolicy(allow_loopback=True).address_reason("127.0.0.1") is None
    assert NetworkPolicy(allow_link_local=True).address_reason("169.254.169.254") is None
    assert NetworkPolicy(allowed_networks=["10.1.0.0/16"]).address_reason("10.1.2.3") is None
    assert NetworkPolicy(allowed_networks=["10.1.0.0/16"]).address_reason("10.2.0.1") is not None
    assert "denied network" in (NetworkPolicy(denied_networks=["8.8.8.0/24"]).address_reason("8.8.8.8") or "")
    assert NetworkPolicy.public().address_reason("not-an-ip") == "invalid address 'not-an-ip'"


def test_static_url_checks() -> None:
    policy = NetworkPolicy(allowed_ports=[80, 443], denied_hosts=["evil.example"], resolve=False)
    assert policy.reason("ftp://example.com/") == "scheme 'ftp' is not allowed"
    assert policy.reason("http://example.com:8080/") == "port 8080 is not allowed"
    assert policy.reason("http://sub.evil.example/") == "host sub.evil.example is denied"
    assert policy.reason("http://localhost/") == "localhost is a loopback name"
    assert policy.reason("http://api.localhost/") == "api.localhost is a loopback name"
    assert policy.reason("http://[::1]/") == "::1 is a loopback address"
    assert policy.reason("http://example.com/") is None
    assert NetworkPolicy(allowed_hosts=["intranet.corp"]).reason("http://intranet.corp/") is None


def test_coercion() -> None:
    assert NetworkPolicy.coerce(None) is None and NetworkPolicy.coerce("any") is None
    assert NetworkPolicy.coerce(False) is None
    assert NetworkPolicy.coerce("public").allowed_categories == {"public"}  # type: ignore[union-attr]
    assert "loopback" in NetworkPolicy.coerce("private").allowed_categories  # type: ignore[union-attr]
    assert NetworkPolicy.coerce({"allow_private": True}).allowed_categories == {"public", "private"}  # type: ignore[union-attr]
    policy = NetworkPolicy()
    assert NetworkPolicy.coerce(policy) is policy
    with pytest.raises(ConfigurationError):
        NetworkPolicy.coerce("everything")
    with pytest.raises(ConfigurationError):
        NetworkPolicy.coerce({"allow_everything": True})
    with pytest.raises(ConfigurationError):
        NetworkPolicy(allowed_networks=["not-a-network"])


def test_names_are_resolved_and_every_address_checked() -> None:
    policy = NetworkPolicy.public()

    async def check(url: str) -> None:
        await policy.check(url)

    with pytest.raises(NetworkPolicyError) as info:
        asyncio.run(check("http://127.0.0.1:8080/"))
    assert info.value.address == "127.0.0.1" and info.value.reason == "127.0.0.1 is a loopback address"
    if sys.platform.startswith("linux"):
        # glibc turns these into 127.0.0.1; the policy judges what the resolver returns.
        for sneaky in ("http://2130706433/", "http://0x7f000001/", "http://0177.0.0.1/"):
            with pytest.raises(NetworkPolicyError):
                asyncio.run(check(sneaky))
    with pytest.raises(NetworkError) as dns:
        asyncio.run(check("http://no-such-host.invalid/"))
    assert dns.value.kind == "dns"
    # Through a proxy, a name that does not resolve here is the proxy's business.
    asyncio.run(policy.check("http://no-such-host.invalid/", proxied=True))
    with pytest.raises(NetworkPolicyError):
        policy.check_sync("http://localhost:1/")
    assert policy.stats["refused"] >= 2


def test_connected_address_is_checked() -> None:
    policy = NetworkPolicy.public()
    policy.check_connected("http://example.com/", "93.184.215.14")
    with pytest.raises(NetworkPolicyError) as info:
        policy.check_connected("http://example.com/", "10.0.0.7")
    assert info.value.address == "10.0.0.7"
    policy.check_connected("http://example.com/", "10.0.0.7", proxied=True)  # the proxy's address says nothing
    NetworkPolicy(allowed_hosts=["example.com"]).check_connected("http://example.com/", "10.0.0.7")


def test_fetchers_refuse_before_connecting(fresh_site) -> None:
    with pytest.raises(NetworkPolicyError):
        wg.get(fresh_site.url + "/products/page/1", network_policy="public")
    with Fetcher(network_policy="public", retries=3) as fetcher, pytest.raises(NetworkPolicyError):
        fetcher.get(fresh_site.url + "/products/page/2")

    async def run() -> None:
        async with AsyncFetcher(network_policy="public", retries=3) as fetcher:
            with pytest.raises(NetworkPolicyError):
                await fetcher.get(fresh_site.url + "/products/page/3")

    asyncio.run(run())
    assert sum(fresh_site.site.hits.values()) == 0  # nothing reached the server


def test_every_redirect_hop_is_checked(fresh_site) -> None:
    # 127.0.0.1 is allowed by name; the redirect target "localhost" is not.
    port = fresh_site.server_address[1]
    policy = NetworkPolicy(allowed_hosts=["127.0.0.1"])
    target = f"http://localhost:{port}/products/page/1"
    ok = wg.get(fresh_site.url + "/redirect?to=/product/1", network_policy=policy)
    assert ok.status == 200 and ok.url == fresh_site.url + "/product/1"
    assert ok.history == [fresh_site.url + "/redirect?to=/product/1"]
    assert ok.ip == "127.0.0.1"
    with pytest.raises(NetworkPolicyError) as info:
        wg.get(fresh_site.url + "/redirect?to=" + target, network_policy=policy)
    assert info.value.url == target
    assert fresh_site.site.hits["/products/page/1"] == 0

    async def run() -> None:
        async with AsyncFetcher(network_policy=policy) as fetcher:
            with pytest.raises(NetworkPolicyError):
                await fetcher.get(fresh_site.url + "/redirect?to=" + target)
            page = await fetcher.get(fresh_site.url + "/redirect?to=/product/2")
            assert page.url.endswith("/product/2") and len(page.history) == 1

    asyncio.run(run())
    assert fresh_site.site.hits["/products/page/1"] == 0


@pytest.mark.parametrize(("code", "method"), [(301, "GET"), (302, "GET"), (303, "GET"), (307, "POST"), (308, "POST")])
def test_manual_redirects_follow_http_semantics(fresh_site, code: int, method: str) -> None:
    # With a policy, wintergrab follows redirects itself; it must do exactly what curl does.
    policy = NetworkPolicy(allowed_hosts=["127.0.0.1"])
    url = fresh_site.url + f"/redirect?to=/echo&code={code}"
    by_hand = wg.post(url, data={"a": "1"}, network_policy=policy).json()
    by_curl = wg.post(url, data={"a": "1"}).json()
    assert by_hand["method"] == by_curl["method"] == method
    if method == "POST":
        assert by_hand["body"] == by_curl["body"] == "a=1"


def test_manual_redirect_limit(fresh_site) -> None:
    policy = NetworkPolicy(allowed_hosts=["127.0.0.1"])
    with pytest.raises(NetworkError) as info:
        wg.get(
            fresh_site.url + "/redirect?to=/redirect?to=/redirect",
            network_policy=policy,
            max_redirects=1,
            retries=2,
        )
    assert info.value.kind == "redirects" and not info.value.retryable


def test_dns_rebinding_is_caught_after_connecting(fresh_site, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a name that resolved to a public address for the check, then connected to a private one.
    policy = NetworkPolicy.public()

    async def passes(url: str, *, proxied: bool = False) -> None:
        return None

    monkeypatch.setattr(policy, "check", passes)

    async def run() -> None:
        async with AsyncFetcher(network_policy=policy) as fetcher:
            with pytest.raises(NetworkPolicyError) as info:
                await fetcher.get(fresh_site.url + "/product/1")
            assert info.value.address == "127.0.0.1" and "connected to" in info.value.reason

    asyncio.run(run())


def test_spider_with_public_policy_refuses_local_site(fresh_site, caplog: pytest.LogCaptureFixture) -> None:
    class Local(Spider):
        log_level = None
        start_urls = [fresh_site.url + "/products/page/1", fresh_site.url + "/products/page/2"]
        network_policy = "public"
        refused: list[str] = []

        def parse(self, response):
            yield {"url": response.url}

        def on_error(self, request, error):
            self.refused.append(request.url)

    with caplog.at_level(logging.WARNING, logger="wintergrab"):
        result = Local().run()
    assert result.items == [] and result.stats["policy_blocked"] == 2
    assert Local.refused == []  # refusals are counted, not reported as errors
    assert sum(fresh_site.site.hits.values()) == 0  # not even robots.txt was fetched
    warnings = [r for r in caplog.records if "network policy refused" in r.getMessage()]
    assert len(warnings) == 1  # once per host


def test_spider_policy_allowing_the_site_crawls_normally(site) -> None:
    class Allowed(Spider):
        log_level = None
        start_urls = [site.url + "/products/page/1"]
        network_policy = {"allow_loopback": True}

        def parse(self, response):
            yield {"title": response.title}

    result = Allowed().run()
    assert result.items == [{"title": "Products 1"}] and "policy_blocked" not in result.stats


# --------------------------------------------------------------------------- #
# resource filter
# --------------------------------------------------------------------------- #
def test_registrable_domain() -> None:
    assert registrable_domain("shop.example.co.uk") == "example.co.uk"
    assert registrable_domain("a.b.example.com") == "example.com"
    assert registrable_domain("example.com") == "example.com"
    assert registrable_domain("user.github.io") == "user.github.io"
    assert registrable_domain("127.0.0.1") == "127.0.0.1"
    assert registrable_domain("WWW.Example.COM.") == "example.com"


def test_resource_filter_decisions() -> None:
    f = ResourceFilter(
        block_types=["image"],
        lists=["analytics", "ads"],
        block_domains=["cdn.bad.example"],
        allow_domains=["good.example"],
        block_patterns=[r"/pixel\.gif"],
    )
    page = "https://shop.example/p/1"
    assert f.reason("https://shop.example/logo.png", "image", page) == "type:image"
    assert f.reason("https://www.google-analytics.com/analytics.js", "script", page) == "list"
    assert f.reason("https://securepubads.g.doubleclick.net/x.js", "script", page) == "list"
    assert f.reason("https://x.cdn.bad.example/a.js", "script", page) == "domain"
    assert f.reason("https://shop.example/track/pixel.gif?x=1", "other", page) == "pattern"
    assert f.reason("https://good.example/a.png", "image", page) is None  # allow-listed first
    assert f.reason("https://shop.example/app.js", "script", page) is None
    assert f.reason("https://shop.example/p/1", "document", page, main_document=True) is None
    # Lists only apply to third parties: crawling an analytics vendor's own site still works.
    assert f.reason("https://www.google-analytics.com/app.js", "script", "https://www.google-analytics.com/") is None
    third = ResourceFilter(block_types=(), block_third_party=True)
    assert third.reason("https://cdn.other.example/a.js", "script", page) == "third-party"
    assert third.reason("https://static.shop.example/a.js", "script", page) is None
    assert f.blocks("https://shop.example/a.png", "image", page) and f.stats["type:image"] == 1


def test_resource_filter_lists_and_coercion(tmp_path) -> None:
    blocklist = tmp_path / "list.txt"
    blocklist.write_text(
        "# hosts file\n0.0.0.0 ads.one.example\n127.0.0.1 localhost\n"
        "! adblock\n||tracker.two.example^\n||x.example/path$script\nplain.three.example\n"
        "not a domain\n",
        encoding="utf-8",
    )
    f = ResourceFilter(block_types=())
    assert f.load_list(blocklist) == 3
    assert f.block_domains == {"ads.one.example", "tracker.two.example", "plain.three.example"}
    assert ResourceFilter.coerce(None, block_types=()) is None
    assert ResourceFilter.coerce(None).block_types == {"image", "media", "font"}  # type: ignore[union-attr]
    everything = ResourceFilter.coerce(True)
    assert everything is not None and set(everything.lists) == set(BUILTIN_LISTS)
    assert ResourceFilter.coerce({"lists": ["ads"]}, block_types=["font"]).block_types == {"font"}  # type: ignore[union-attr]
    with pytest.raises(ConfigurationError):
        ResourceFilter(block_types=["pictures"])
    with pytest.raises(ConfigurationError):
        ResourceFilter(lists=["malware"])


@pytest.mark.browser
def test_browser_blocks_resources_and_reports_them(site) -> None:
    with wg.BrowserFetcher(resource_filter={"lists": ["analytics"]}) as browser:
        page = browser.get(site.url + "/thirdparty")
    assert page.css("#t::text").get() == "scripted"  # the page's own script still ran
    assert page.blocked_resources == {"type:image": 1, "list": 1}


@pytest.mark.browser
def test_browser_network_policy(fresh_site) -> None:
    port = fresh_site.server_address[1]
    with wg.BrowserFetcher(network_policy="public") as browser, pytest.raises(NetworkPolicyError):
        browser.get(fresh_site.url + "/product/1")
    assert fresh_site.site.hits["/product/1"] == 0

    policy = NetworkPolicy(allowed_hosts=["127.0.0.1"])
    with wg.BrowserFetcher(network_policy=policy) as browser:
        page = browser.get(fresh_site.url + "/fetch-localhost", wait_for="#out:not(:empty)", wait=0.5)
        assert page.css("#out::text").get() == "refused"
        assert page.blocked_resources.get("policy") == 1
        assert page.ip == "127.0.0.1"
        # Playwright cannot intercept redirect hops; the page is checked afterwards and discarded.
        with pytest.raises(NetworkPolicyError):
            browser.get(fresh_site.url + f"/redirect?to=http://localhost:{port}/products/page/1")
    assert fresh_site.site.hits["/api/products"] == 0
