"""Language models, through adapters: none is needed, and none is called unless you name one.

::

    --model openai:NAME        an OpenAI-compatible chat completions API: OpenAI, or a self-hosted
                               server that speaks it (vLLM, llama.cpp, LM Studio...; --model-url)
    --model anthropic:NAME     the Anthropic Messages API
    --model ollama:NAME        a local Ollama server (http://localhost:11434)

    model = load_model("ollama:NAME")
    Extractor(schema, model=model)     # asked only for fields the page's own data does not give

A model's answers are never taken on trust. The extractor normalizes and validates them like any
value, and checks them against the page. A value found nowhere on the page keeps a low confidence
(see :mod:`wintergrab.extraction.model`).

API keys come from the environment (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``) or ``api_key=``. They
are never written into run records or logs. A provider sends the page's text, and its images when
given, to the API it names: pick one you may send them to.

A provider is a :class:`ModelProvider` subclass (``complete(prompt, system=, images=,
json_output=) -> str``). Plugins add theirs with ``registry.model_provider(name, cls)``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from . import __version__
from .errors import ConfigurationError, ModelError
from .extraction.model import ModelRequest, parse_answer

__all__ = [
    "PROVIDERS",
    "Anthropic",
    "Image",
    "ModelProvider",
    "Ollama",
    "OpenAICompatible",
    "load_model",
    "register_provider",
]

log = logging.getLogger("wintergrab.models")

_RETRY_AFTER = (2.0, 8.0)  # seconds before the second and third attempts (429, 5xx, network)
_SYSTEM = (
    "You extract data from web pages. You answer with JSON only, and use only what the page states: "
    "null for anything it does not."
)


@dataclass(frozen=True)
class Image:
    """An image for a model that reads images (a screenshot, a chart): its bytes and media type."""

    data: bytes
    media_type: str = "image/png"

    @property
    def base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


class ModelProvider:
    """Asks one model through one API (see the module docs).

    Args:
        model: The model's name, as the API knows it.
        api_key: The key (by default from the provider's environment variable).
        base_url: Where the API is (for a self-hosted or compatible server).
        timeout: Seconds for each attempt.
        max_tokens: The longest answer.
        temperature: 0 for the most repeatable answers.
    """

    #: The provider's name in ``--model NAME:MODEL``.
    provider: ClassVar[str] = ""
    #: The environment variable holding the key.
    key_variable: ClassVar[str | None] = None
    default_url: ClassVar[str] = ""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> None:
        if not model:
            raise ConfigurationError(f"{self.provider}: name the model ({self.provider}:MODEL)", key="model")
        self.model = model
        self.api_key = api_key or (os.environ.get(self.key_variable) if self.key_variable else None) or None
        if not self.api_key and self.key_required(base_url):
            raise ConfigurationError(f"{self.provider}: no API key (set {self.key_variable})", key="model")
        self.base_url = (base_url or self.default_url).rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        #: Tokens used so far: ``{"input": n, "output": n}`` (when the API says).
        self.usage = {"input": 0, "output": 0, "requests": 0}

    def key_required(self, base_url: str | None) -> bool:
        """Whether the API at ``base_url`` needs a key."""
        return True

    @property
    def name(self) -> str:
        """``provider:model``, as provenance and logs show it."""
        return f"{self.provider}:{self.model}"

    def complete(
        self, prompt: str, *, system: str | None = None, images: Sequence[Image] = (), json_output: bool = False
    ) -> str:
        """The model's answer to ``prompt``."""
        raise NotImplementedError

    def __call__(self, request: ModelRequest) -> dict[str, Any]:
        """An extraction model (see :class:`~wintergrab.extraction.Extractor`): the fields asked for."""
        answer = self.complete(request.prompt(), system=_SYSTEM, json_output=True)
        values = parse_answer(answer)
        if not values and answer.strip():
            log.warning("%s gave no JSON object: %.120r", self.name, answer)
        return values

    # -- HTTP ------------------------------------------------------------------------------------ #
    def _post(self, url: str, body: Mapping[str, Any], headers: Mapping[str, str]) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        all_headers = {"Content-Type": "application/json", "User-Agent": f"wintergrab/{__version__}", **headers}
        for attempt in range(len(_RETRY_AFTER) + 1):
            request = urllib.request.Request(url, data=data, headers=all_headers, method="POST")
            retry_after: float | None = None
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as answer:
                    result = json.loads(answer.read().decode("utf-8"))
                self.usage["requests"] += 1
                return result if isinstance(result, dict) else {}
            except urllib.error.HTTPError as exc:
                detail = _error_detail(exc)
                if exc.code not in (408, 409, 429) and exc.code < 500:
                    raise ModelError(f"{self.name}: HTTP {exc.code}: {detail}") from None
                reason = f"HTTP {exc.code}: {detail}"
                header = exc.headers.get("retry-after") if exc.headers else None
                retry_after = float(header) if header and header.replace(".", "", 1).isdigit() else None
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                reason = str(getattr(exc, "reason", exc))
            except ValueError as exc:
                raise ModelError(f"{self.name}: the answer was not JSON ({exc})") from None
            if attempt == len(_RETRY_AFTER):
                raise ModelError(f"{self.name}: {reason}")
            wait = min(retry_after, 60.0) if retry_after is not None else _RETRY_AFTER[attempt]
            log.info("%s: %s; asking again in %.0f s", self.name, reason, wait)
            time.sleep(wait)
        raise AssertionError("unreachable")

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.model!r}, base_url={self.base_url!r})"


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
    except Exception:
        return exc.reason if isinstance(exc.reason, str) else "error"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or error)[:300]
    return str(error or body)[:300]


class OpenAICompatible(ModelProvider):
    """The chat completions API (``POST {base_url}/chat/completions``) of OpenAI and of servers
    that speak it. ``json_mode`` asks for a JSON object (``response_format``), which some servers
    do not know: turn it off for them."""

    provider = "openai"
    key_variable = "OPENAI_API_KEY"
    default_url = "https://api.openai.com/v1"

    def __init__(self, model: str, *, json_mode: bool = True, **options: Any) -> None:
        super().__init__(model, **options)
        self.json_mode = json_mode

    def key_required(self, base_url: str | None) -> bool:
        return not base_url or base_url.rstrip("/") == self.default_url  # a server of your own may need none

    def complete(
        self, prompt: str, *, system: str | None = None, images: Sequence[Image] = (), json_output: bool = False
    ) -> str:
        content: Any = prompt
        if images:
            content = [{"type": "text", "text": prompt}] + [
                {"type": "image_url", "image_url": {"url": f"data:{image.media_type};base64,{image.base64}"}}
                for image in images
            ]
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": content}]
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": self.temperature,
                                "max_tokens": self.max_tokens}  # fmt: skip
        if json_output and self.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        answer = self._post(f"{self.base_url}/chat/completions", body, headers)
        usage = answer.get("usage") or {}
        self.usage["input"] += int(usage.get("prompt_tokens") or 0)
        self.usage["output"] += int(usage.get("completion_tokens") or 0)
        try:
            return str(answer["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            raise ModelError(f"{self.name}: an answer without choices: {str(answer)[:200]}") from None


class Ollama(OpenAICompatible):
    """A local Ollama server, through its OpenAI-compatible API (no key)."""

    provider = "ollama"
    key_variable = "OLLAMA_API_KEY"
    default_url = "http://localhost:11434/v1"

    def key_required(self, base_url: str | None) -> bool:
        return False


class Anthropic(ModelProvider):
    """The Anthropic Messages API (``POST {base_url}/v1/messages``)."""

    provider = "anthropic"
    key_variable = "ANTHROPIC_API_KEY"
    default_url = "https://api.anthropic.com"
    version = "2023-06-01"

    def complete(
        self, prompt: str, *, system: str | None = None, images: Sequence[Image] = (), json_output: bool = False
    ) -> str:
        content: Any = prompt
        if images:
            content = [
                {"type": "image", "source": {"type": "base64", "media_type": image.media_type, "data": image.base64}}
                for image in images
            ] + [{"type": "text", "text": prompt}]
        body: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens, "temperature": self.temperature,
                                "messages": [{"role": "user", "content": content}]}  # fmt: skip
        if system:
            body["system"] = system
        headers = {"x-api-key": str(self.api_key), "anthropic-version": self.version}
        answer = self._post(f"{self.base_url}/v1/messages", body, headers)
        usage = answer.get("usage") or {}
        self.usage["input"] += int(usage.get("input_tokens") or 0)
        self.usage["output"] += int(usage.get("output_tokens") or 0)
        parts = answer.get("content")
        if not isinstance(parts, list):
            raise ModelError(f"{self.name}: an answer without content: {str(answer)[:200]}")
        return "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("type") == "text")


#: Providers by name (``--model NAME:MODEL``); plugins add theirs.
PROVIDERS: dict[str, type[ModelProvider]] = {
    "openai": OpenAICompatible,
    "anthropic": Anthropic,
    "ollama": Ollama,
}


def register_provider(name: str, provider: type[ModelProvider]) -> None:
    """Add a provider for ``--model NAME:MODEL``."""
    if not (isinstance(provider, type) and issubclass(provider, ModelProvider)):
        raise ConfigurationError(f"a model provider is a ModelProvider subclass, not {provider!r}")
    PROVIDERS[name.lower()] = provider


def load_model(spec: str, **options: Any) -> ModelProvider:
    """A provider from ``"provider:model"`` (``"anthropic:NAME"``, ``"ollama:NAME"``), with its options
    (``base_url``, ``api_key``, ``timeout``...)."""
    provider, _, model = str(spec).partition(":")
    name = provider.strip().lower()
    if name not in PROVIDERS:
        from .plugins import load_plugins

        load_plugins()  # a plugin may add it
    if name not in PROVIDERS:
        raise ConfigurationError(
            f"no model provider {provider!r} (providers: {', '.join(sorted(PROVIDERS))}); use PROVIDER:MODEL",
            key="model",
        )
    return PROVIDERS[name](model.strip(), **{k: v for k, v in options.items() if v is not None})
