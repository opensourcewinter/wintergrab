"""Browser actions: steps written as data, done on the page before it is read, and what each did."""

from __future__ import annotations

import functools
import http.server
import json
import threading
from pathlib import Path

import pytest

from wintergrab.cli import main
from wintergrab.errors import BrowserFetchError, ConfigurationError
from wintergrab.fetchers.actions import Action, load_actions, parse_actions

PAGES = Path(__file__).parent / "data" / "actions"


def test_steps_as_text_and_as_mappings() -> None:
    steps = parse_actions([
        "click button.more until-gone", "click .next x3", "expand .faq summary", "dismiss #cookies button",
        "fill input[name=q] => winter parka", {"fill": {"#first": "Ada", "#last": "Lovelace"}},
        {"select": {"#sort": "price"}}, "press Enter", "press #q => Enter", "wait .results", "wait 1.5",
        "scroll 5", "tabs .tabs a", "snapshot", "screenshot shot.png", "pdf page.pdf", "download a.csv",
        {"click": ".maybe", "optional": True}, {"click": ".again", "repeat": 4},
    ])  # fmt: skip
    assert [str(s) for s in steps] == [
        "click button.more until-gone", "click .next x3", "expand .faq summary", "dismiss #cookies button",
        "fill input[name=q] => winter parka", "fill #first => Ada", "fill #last => Lovelace",
        "select #sort => price", "press Enter", "press #q => Enter", "wait .results", "wait 1.5", "scroll 5",
        "tabs .tabs a", "snapshot", "screenshot shot.png", "pdf page.pdf", "download a.csv",
        "click .maybe (optional)", "click .again x4",
    ]  # fmt: skip
    assert steps[0] == Action("click", "button.more", repeat=50, until_gone=True)
    assert steps[3].optional  # a dialog that may not be there
    assert parse_actions("snapshot") == [Action("snapshot")]


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ("teleport .x", "unknown browser action 'teleport'"),
        ("click", "click needs a selector"),
        ("fill #q", "fill SELECTOR => VALUE"),
        ("click .x x0", "x1 to x50"),
        ("scroll lots", "scroll takes a number"),
        ("pdf", "needs a file"),
        ({"click": ".a", "wait": ".b"}, "one verb"),
        ({"click": ".a", "repeat": 99}, "repeat: a number from 1 to 50"),
        (42, "a string or a mapping"),
    ],
)
def test_steps_that_are_not(step, message) -> None:
    with pytest.raises(ConfigurationError, match=message):
        parse_actions([step])


def test_steps_from_files(tmp_path) -> None:
    pytest.importorskip("yaml")
    listed = ["click .more until-gone", {"fill": {"#q": "parka"}}]
    (tmp_path / "steps.json").write_text(json.dumps(listed), encoding="utf-8")
    (tmp_path / "steps.yaml").write_text("- click .more until-gone\n- fill: {'#q': parka}\n", encoding="utf-8")
    assert load_actions(tmp_path / "steps.json") == load_actions(tmp_path / "steps.yaml") == parse_actions(listed)
    (tmp_path / "bad.json").write_text('{"click": ".more"}', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="a list of browser actions"):
        load_actions(tmp_path / "bad.json")


@pytest.fixture
def shop():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(PAGES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.mark.browser
def test_actions_on_a_page(shop, tmp_path) -> None:
    from wintergrab.fetchers.browser import BrowserFetcher

    with BrowserFetcher() as browser:
        page = browser.get(
            shop + "/shop.html",
            actions=["dismiss #accept", "click .load-more until-gone", "expand summary", {"fill": {"#q": "parka"}},
                     "press #q => Enter", {"select": {"#sort": "Price"}}, "wait #results", "tabs .tabs a",
                     "download a.export", f"pdf {tmp_path / 'shop.pdf'}", "dismiss .nothing-here"],
            downloads=tmp_path / "files",
        )  # fmt: skip
        assert [(a["step"], a["detail"]) for a in page.actions][:3] == [
            ("dismiss #accept", "clicked"),
            ("click .load-more until-gone", "clicked 3 time(s); it is gone"),
            ("expand summary", "clicked 2 element(s)"),
        ]
        assert page.actions[-1] == {"step": "dismiss .nothing-here", "ok": True, "detail": "not there"}
        assert not page.css("#cookies") and len(page.css("li.item")) == 8  # the dialog gone, every item loaded
        assert page.css("#results::text").get() == "Results for parka"
        assert [s["after"] for s in page.snapshots] == [".tabs a #1: Specifications", ".tabs a #2: Reviews"]
        assert "Weight: 1.2 kg" in page.snapshots[0]["html"] and "Four stars" in page.snapshots[1]["html"]
        [download] = page.downloads
        assert download["name"] == "report.csv" and Path(download["path"]).read_text().startswith("sku,name,price")
        assert (tmp_path / "shop.pdf").read_bytes().startswith(b"%PDF-")
        assert {"type": "warning", "text": "an old API"} in page.console
        # a step that cannot be done stops the page, says which, and is not tried again
        with pytest.raises(BrowserFetchError, match=r"browser action 'click \.no-such-button' failed") as failed:
            browser.get(shop + "/shop.html", actions=["click .no-such-button"], timeout=3)
        assert not failed.value.retryable and failed.value.context["action"] == "click .no-such-button"


@pytest.mark.browser
def test_steps_go_nowhere_the_network_policy_refuses(shop) -> None:
    from wintergrab.errors import NetworkPolicyError
    from wintergrab.fetchers.browser import BrowserFetcher
    from wintergrab.netpolicy import NetworkPolicy

    policy = NetworkPolicy(allow_loopback=True, denied_hosts=["localhost"])  # this shop, not the one next door
    with BrowserFetcher(network_policy=policy) as browser:
        with pytest.raises(NetworkPolicyError, match=r"'click #elsewhere' led to http://localhost:\d+/report\.csv"):
            browser.get(shop + "/shop.html", actions=["click #elsewhere"], timeout=5)
        # a link to where nothing answers: the browser's error page is no result
        with pytest.raises(BrowserFetchError, match="'click #closed' failed: the page it led to could not be loaded"):
            browser.get(shop + "/shop.html", actions=["click #closed"], timeout=5)
        # optional or not, a step that loses the page stops it
        with pytest.raises(BrowserFetchError, match="could not be loaded"):
            browser.get(shop + "/shop.html", actions=[{"click": "#closed", "optional": True}], timeout=5)


@pytest.mark.browser
def test_the_do_option(shop, capsys, tmp_path) -> None:
    code = main(["get", shop + "/shop.html", "--do", "click .load-more until-gone", "--do", "tabs .tabs a"])
    assert code == 0
    captured = capsys.readouterr()
    assert "Parka 8" in captured.out  # the page after its steps
    assert "Weight: 1.2 kg" in captured.out and "Four stars from 12 reviews" in captured.out  # each tab
    assert "ok click .load-more until-gone: clicked 3 time(s); it is gone" in captured.err
    steps = tmp_path / "steps.json"
    steps.write_text(json.dumps(["expand summary"]), encoding="utf-8")
    assert main(["-q", "get", shop + "/shop.html", "--actions", str(steps)]) == 0
    assert "Free over 50 euros" in capsys.readouterr().out
    assert main(["get", shop + "/shop.html", "--do", "fill #q"]) == 2
    assert "fill SELECTOR => VALUE" in capsys.readouterr().err


@pytest.mark.browser
def test_a_spider_request_with_actions(shop) -> None:
    from wintergrab import AsyncBrowserFetcher, Request, Spider

    class Shop(Spider):
        name = "actions"
        obey_robots_txt = False
        log_level = None

        def configure_sessions(self, sessions):
            sessions.add("browser", AsyncBrowserFetcher(headless=True, max_pages=1))

        def start_requests(self):
            yield Request(shop + "/shop.html", session="browser", options={"actions": ["click .load-more until-gone"]})

        def parse(self, response):
            yield {"items": len(response.css("li.item")), "steps": len(response.actions)}

    result = Shop().run()
    assert result.items == [{"items": 8, "steps": 1}]
