from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from testsite import ProxyServer, SiteServer


@pytest.fixture(autouse=True)
def _isolated_adaptive_db(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never write adaptive fingerprints into the real user cache during tests."""
    db = tmp_path_factory.mktemp("adaptive") / "adaptive.sqlite3"
    monkeypatch.setenv("WINTERGRAB_ADAPTIVE_DB", str(db))


@pytest.fixture(scope="session")
def site() -> SiteServer:
    server = SiteServer().start()
    yield server
    server.stop()


@pytest.fixture
def fresh_site() -> SiteServer:
    """A site with its own hit counters (for tests that count requests)."""
    server = SiteServer().start()
    yield server
    server.stop()


@pytest.fixture
def proxies(monkeypatch: pytest.MonkeyPatch):
    # libcurl honours no_proxy even for explicit proxies; our proxies are local.
    for var in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
    servers = [ProxyServer("p1").start(), ProxyServer("p2").start()]
    yield servers
    for s in servers:
        s.stop()


def _browser_available() -> bool:
    if os.environ.get("WINTERGRAB_SKIP_BROWSER"):
        return False
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    from wintergrab.fetchers.browser import _discover_chromium

    if os.environ.get("WINTERGRAB_BROWSER_PATH") or _discover_chromium():
        return True
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    return any(cache.glob("chromium*"))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if _browser_available():
        return
    skip = pytest.mark.skip(reason="Playwright/Chromium not available")
    for item in items:
        if "browser" in item.keywords:
            item.add_marker(skip)
