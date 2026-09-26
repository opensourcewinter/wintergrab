"""Model providers: the requests they send and the answers they read (against a stand-in server
that speaks the OpenAI chat completions and Anthropic Messages APIs), and models in extraction."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from wintergrab import models
from wintergrab.errors import ConfigurationError, ModelError
from wintergrab.models import Anthropic, Image, Ollama, OpenAICompatible, load_model


class API(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        #: What the model says (the text of its answer); or (status, body, headers) answers, used first.
        self.answer = "{}"
        self.statuses: list[tuple[int, dict[str, Any], dict[str, str]]] = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    server: API

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        if self.server.statuses:
            status, payload, headers = self.server.statuses.pop(0)
        elif self.path.endswith("/chat/completions"):
            status, headers = 200, {}
            payload = {"id": "c1", "object": "chat.completion", "model": body["model"],
                       "choices": [{"index": 0, "message": {"role": "assistant", "content": self.server.answer},
                                    "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 120, "completion_tokens": 9, "total_tokens": 129}}  # fmt: skip
        else:
            status, headers = 200, {}
            payload = {"id": "m1", "type": "message", "role": "assistant", "model": body["model"],
                       "content": [{"type": "text", "text": self.server.answer}], "stop_reason": "end_turn",
                       "usage": {"input_tokens": 110, "output_tokens": 7}}  # fmt: skip
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(models, "_RETRY_AFTER", (0.0, 0.0))
    server = API()
    yield server
    server.shutdown()
    server.server_close()


def test_the_openai_compatible_api(api) -> None:
    api.answer = '{"brand": "Acme"}'
    model = OpenAICompatible("some-model", api_key="k-123", base_url=api.url + "/v1")
    assert model.complete("Which brand?", system="Be brief.", json_output=True) == '{"brand": "Acme"}'
    path, headers, body = api.requests[-1]
    assert path == "/v1/chat/completions" and headers["authorization"] == "Bearer k-123"
    assert body["model"] == "some-model" and body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Which brand?"}]
    assert model.usage == {"input": 120, "output": 9, "requests": 1}

    model.complete("What is shown?", images=[Image(b"\x89PNG...", "image/png")])
    parts = api.requests[-1][2]["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "What is shown?"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")
    plain = OpenAICompatible("m", base_url=api.url + "/v1", json_mode=False)  # a server of your own: no key needed
    plain.complete("x", json_output=True)
    assert "response_format" not in api.requests[-1][2] and "authorization" not in api.requests[-1][1]


def test_the_anthropic_api(api) -> None:
    api.answer = '{"brand": "Acme"}'
    model = Anthropic("some-model", api_key="k-456", base_url=api.url, max_tokens=300)
    assert model.complete("Which brand?", system="Be brief.") == '{"brand": "Acme"}'
    path, headers, body = api.requests[-1]
    assert path == "/v1/messages" and headers["x-api-key"] == "k-456" and headers["anthropic-version"] == "2023-06-01"
    assert body["system"] == "Be brief." and body["max_tokens"] == 300
    assert body["messages"] == [{"role": "user", "content": "Which brand?"}]
    assert model.usage == {"input": 110, "output": 7, "requests": 1}
    model.complete("What is shown?", images=[Image(b"GIF89a", "image/gif")])
    image, text = api.requests[-1][2]["messages"][0]["content"]
    assert (
        image["source"] == {"type": "base64", "media_type": "image/gif", "data": "R0lGODlh"} and text["type"] == "text"
    )


def test_failures_retries_and_keys(api, monkeypatch) -> None:
    model = OpenAICompatible("m", api_key="k", base_url=api.url + "/v1")
    api.statuses = [(429, {"error": {"message": "slow down"}}, {"Retry-After": "0"}), (503, {}, {})]
    api.answer = "ok"
    assert model.complete("x") == "ok" and len(api.requests) == 3  # asked again twice
    api.statuses = [(400, {"error": {"message": "model not found", "type": "invalid_request_error"}}, {})]
    with pytest.raises(ModelError, match="HTTP 400: model not found"):
        model.complete("x")  # not asked again
    api.statuses = [(500, {}, {})] * 3
    with pytest.raises(ModelError, match="HTTP 500"):
        model.complete("x")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="no API key \\(set OPENAI_API_KEY\\)"):
        load_model("openai:m")  # the API itself needs one
    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
        load_model("anthropic:m")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert load_model("anthropic:m").api_key == "from-env" and "from-env" not in repr(load_model("anthropic:m"))
    assert isinstance(load_model("ollama:m"), Ollama) and load_model("ollama:m").base_url == "http://localhost:11434/v1"
    with pytest.raises(ConfigurationError, match="no model provider 'nope'"):
        load_model("nope:m")
    with pytest.raises(ConfigurationError, match="name the model"):
        load_model("ollama")


PAGE = """<html><head><title>Trail shoe</title>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Product", "name": "Trail shoe",
"offers": {"@type": "Offer", "price": "89.00", "priceCurrency": "EUR"}}</script></head>
<body><h1>Trail shoe</h1><p>Made of recycled mesh, it weighs 280 g. Colour: moss green.</p></body></html>"""
SCHEMA = {"name": "shoe", "fields": {"name": "string", "price": "money",
                                     "material": {"type": "string", "description": "what it is made of"}}}  # fmt: skip


def test_a_model_fills_in_what_the_page_s_data_does_not_give(api) -> None:
    from wintergrab.extraction import Extractor
    from wintergrab.parser import Selector

    api.answer = '```json\n{"material": "recycled mesh"}\n```'
    model = OpenAICompatible("m", base_url=api.url + "/v1")
    record = Extractor(SCHEMA, model=model).extract(Selector(PAGE, url="https://s.example/trail"))
    assert record.fields["name"].method == "json-ld" and record.fields["price"].value["amount"] == 89.0  # not asked
    material = record.fields["material"]
    assert material.value == "recycled mesh" and material.method == "model" and material.source == "model:openai:m"
    prompt = api.requests[-1][2]["messages"][1]["content"]
    assert "- material (string): what it is made of" in prompt and "- price" not in prompt  # only what is missing

    api.answer = '{"material": "carbon fibre"}'  # not on the page: a model can invent, the page cannot
    invented = Extractor(SCHEMA, model=model).extract(Selector(PAGE, url="https://s.example/trail")).fields["material"]
    assert invented.value is None and "not-on-page" in invented.notes  # kept aside, not taken
    assert invented.alternatives[0]["value"] == "carbon fibre" and invented.alternatives[0]["confidence"] < 0.3


def test_the_command_line_takes_a_model(api, site, tmp_path, capsys) -> None:
    from wintergrab.cli import main

    schema = tmp_path / "book.json"
    schema.write_text(json.dumps({"name": "book", "fields": {"name": {"type": "string", "selectors": ["h1"]},
                                                            "blurb": "string"}}), encoding="utf-8")  # fmt: skip
    api.answer = '{"blurb": "Book number 3"}'
    url = site.url + "/books/catalogue/book-3/index.html"
    assert main(["get", url, "--extract", str(schema), "--model", "openai:m", "--model-url", api.url + "/v1"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["name"] == "Book number 3" and record["blurb"] == "Book number 3"


def test_a_model_reads_a_goal_and_is_checked(api) -> None:
    from wintergrab.goals import model_reader, parse_goal

    model = OpenAICompatible("m", base_url=api.url + "/v1")
    text = "espresso machines under 300 euros with a steam wand on coffee.example, the 20 best rated"
    api.answer = json.dumps({"entity": "product", "fields": ["name", "price", "rating", "steam_wand"],
                             "sites": ["coffee.example", "https://amazon.example"], "scope": ["espresso machines"],
                             "filters": ["price < 300 and (currency is None or currency == 'EUR')"], "limit": 20,
                             "monitor": None, "notes": ["'best rated' read as a limit"]})  # fmt: skip
    goal = parse_goal(text, parser=model_reader(model))
    assert goal.scope == ["espresso machines"] and goal.limit == 20 and goal.sites == ["https://coffee.example"]
    assert goal.filters[0].expression.startswith("price < 300")
    assert "read by openai:m" in goal.notes and any("amazon.example" in n for n in goal.notes)  # an invented site
    prompt = api.requests[-1][2]["messages"][0]["content"]
    assert "Request: " + text in prompt and "- property: name (string), price (money)" in prompt

    api.answer = json.dumps({"entity": "spaceship", "fields": ["name"]})  # not a kind there is
    goal = parse_goal(text, parser=model_reader(model))
    assert goal.entity == "product" and goal.sites == ["https://coffee.example"]  # the rules read it
    assert any("could not read it" in n and "read by the rules" in n for n in goal.notes)
    api.answer = json.dumps({"entity": "product", "filters": ["price <<< 3"]})  # not an expression
    assert "read by the rules" in parse_goal(text, parser=model_reader(model)).notes[-1]


def test_the_goal_command_takes_a_model(api, site, capsys) -> None:
    from wintergrab.cli import main

    api.answer = json.dumps({"entity": "product", "fields": ["name", "price"], "sites": [site.url + "/shop/"],
                             "filters": ["price < 50"]})  # fmt: skip
    assert main(["goal", f"cheap things on {site.url}/shop/", "--model", "openai:m", "--model-url", api.url + "/v1",
                 "--plan-only", "--sample", "5"]) == 0  # fmt: skip
    shown = capsys.readouterr()
    assert "where price < 50" in shown.err and "note: read by openai:m" in shown.err
