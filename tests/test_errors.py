"""The error taxonomy: classes, categories, context, pickling, and how fetchers classify failures."""

from __future__ import annotations

import pickle

import pytest

import wintergrab as wg
from wintergrab import errors
from wintergrab.fetchers.response import Response


def test_hierarchy_keeps_old_catch_clauses_working() -> None:
    # Everything a fetch can raise is still a FetchError.
    for cls in (
        errors.NetworkError,
        errors.ProxyError,
        errors.FetchTimeout,
        errors.PolicyError,
        errors.RobotsPolicyError,
        errors.NetworkPolicyError,
        errors.BrowserFetchError,
        errors.CacheMiss,
    ):
        assert issubclass(cls, errors.FetchError), cls
        assert issubclass(cls, errors.WintergrabError), cls
    assert issubclass(errors.SelectorSyntaxError, errors.ParserError)
    assert issubclass(errors.SelectorSyntaxError, ValueError)
    assert issubclass(errors.CheckpointError, errors.StorageError)
    assert issubclass(errors.BrowserNotAvailable, errors.BrowserError)
    assert not issubclass(errors.BrowserNotAvailable, errors.FetchError)  # an install problem, not a fetch failure
    assert issubclass(errors.BrowserFetchError, errors.BrowserError)
    assert issubclass(errors.ConfigurationError, ValueError)
    assert issubclass(errors.ValidationError, ValueError)
    assert wg.HTTPError is wg.HTTPStatusError
    assert wg.CacheMiss is errors.CacheMiss


def test_categories_and_kinds() -> None:
    assert errors.FetchTimeout("http://x/").category == "timeout"
    assert errors.FetchTimeout("http://x/").kind == "timeout"
    assert errors.FetchTimeout("http://x/").is_timeout
    assert errors.NetworkError("http://x/", "boom").kind == "connect"
    assert errors.NetworkError("http://x/", "boom", kind="dns").kind == "dns"
    proxy = errors.ProxyError("http://x/", "bad proxy")
    assert proxy.is_proxy_error and proxy.kind == "proxy"
    policy = errors.NetworkPolicyError("http://10.0.0.1/", "blocked", reason="private", address="10.0.0.1")
    assert policy.category == "policy" and policy.policy == "network" and not policy.retryable
    assert policy.context == {"url": "http://10.0.0.1/", "policy": "network", "address": "10.0.0.1"}
    robots = errors.RobotsPolicyError("http://x/private")
    assert robots.policy == "robots" and not robots.retryable
    assert errors.CacheMiss("http://x/").category == "cache"
    assert errors.category_of(ValueError()) == "internal"
    assert errors.category_of(TimeoutError()) == "timeout"
    assert errors.category_of(errors.BudgetExceeded("max_bytes")) == "budget"


def test_context_and_with_context() -> None:
    err = errors.ExtractionError("price missing").with_context(field="price", url="http://x/", selector=None)
    assert err.context == {"field": "price", "url": "http://x/"}
    cfg = errors.ConfigurationError("must be positive", key="crawl.concurrency")
    assert str(cfg) == "crawl.concurrency: must be positive"
    assert cfg.key == "crawl.concurrency" and cfg.context["key"] == "crawl.concurrency"
    status = errors.HTTPStatusError(Response("http://x/", status=503))
    assert status.context == {"url": "http://x/", "status": 503}
    lost = errors.ExportError("items: 64 item(s) not written: the database hung up", items=64)
    assert lost.items == 64 and lost.context == {"items": 64}  # (a crawl counts them: items_not_written)
    assert errors.ExportError("could not write items.parquet").items is None  # how many is not always known


@pytest.mark.parametrize(
    "exc",
    [
        errors.WintergrabError("plain", context={"a": 1}),
        errors.FetchError("http://x/", "boom", retryable=False),
        errors.NetworkError("http://x/", "dns", kind="dns"),
        errors.FetchTimeout("http://x/", "slow"),
        errors.NetworkPolicyError("http://x/", "blocked", reason="loopback", address="127.0.0.1"),
        errors.RobotsPolicyError("http://x/"),
        errors.BrowserFetchError("http://x/", "crashed"),
        errors.CacheMiss("http://x/"),
        errors.ConfigurationError("bad", key="k"),
        errors.ExportError("shop: 2 item(s) not written", items=2),
        errors.ValidationError("invalid", issues=["a"]),
        errors.BudgetExceeded("max_requests"),
        errors.HTTPStatusError(Response("http://x/", status=500), "server error"),
    ],
)
def test_errors_survive_pickling(exc: errors.WintergrabError) -> None:
    copy = pickle.loads(pickle.dumps(exc))
    assert type(copy) is type(exc)
    assert str(copy) == str(exc)
    assert copy.context == exc.context
    assert copy.category == exc.category
    if isinstance(exc, errors.FetchError):
        assert (copy.url, copy.kind, copy.retryable) == (exc.url, exc.kind, exc.retryable)


def test_http_fetcher_classifies_connection_refused() -> None:
    with pytest.raises(errors.NetworkError) as info:
        wg.get("http://127.0.0.1:9/", retries=0, timeout=5)
    assert info.value.kind == "connect" and info.value.retryable


def test_http_fetcher_classifies_timeouts(site) -> None:
    with pytest.raises(errors.FetchTimeout) as info:
        wg.get(site.url + "/slow?delay=2", retries=0, timeout=0.5)
    assert info.value.is_timeout and info.value.category == "timeout"


def test_http_fetcher_classifies_dns_failures() -> None:
    with pytest.raises(errors.NetworkError) as info:
        wg.get("http://no-such-host.invalid/", retries=0, timeout=5)
    assert info.value.kind in ("dns", "proxy")  # "proxy" when an environment proxy answers for the resolver


def test_too_many_redirects_is_not_retried(fresh_site) -> None:
    loop = fresh_site.url + "/redirect?to=/redirect?to=/redirect"
    with pytest.raises(errors.NetworkError) as info:
        wg.get(loop, retries=3, backoff=0.01, max_redirects=1)
    assert info.value.kind == "redirects" and not info.value.retryable
