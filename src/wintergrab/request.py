"""The :class:`Request` object used by spiders (and ``Response.follow``)."""

from __future__ import annotations

import dataclasses
import hashlib
import json as _json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from .errors import CheckpointError
from .utils import add_params, canonicalize_url

if TYPE_CHECKING:
    from .spider.spider import Spider

Callback = Callable[..., Any] | str | None


@dataclass(eq=False)
class Request:
    """Something to download, plus what to do with the result.

    Args:
        url: Absolute URL (query ``params`` are merged into it).
        callback: Spider method (or its name) that receives the response.
            Defaults to ``parse``.
        method: HTTP method.
        headers: Extra headers for this request.
        params: Query-string parameters.
        data: Form dict or raw body (``str``/``bytes``).
        json: A JSON body.
        cookies: Extra cookies for this request.
        meta: Free-form data carried to the response (``response.meta``).
        cb_kwargs: Extra keyword arguments passed to the callback.
        priority: Higher runs first.
        dont_filter: Skip duplicate filtering (fetch even if seen before).
        session: Name of the spider session to use (e.g. ``"browser"``).
        proxy: Force a proxy for this request.
        errback: Called with ``(request, exception)`` if the request fails.
        options: Extra fetch options, e.g. ``{"wait_for": ".item"}`` for
            browser sessions or ``{"timeout": 60}``.
    """

    url: str
    callback: Callback = None
    method: str = "GET"
    headers: dict[str, str] | None = None
    params: Mapping[str, Any] | None = None
    data: Any = None
    json: Any = None
    cookies: dict[str, str] | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    cb_kwargs: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    dont_filter: bool = False
    session: str | None = None
    proxy: str | None = None
    errback: Callback = None
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.method = self.method.upper()
        if self.params:
            self.url = add_params(self.url, self.params)
            self.params = None
        if not self.url.startswith(("http://", "https://")):
            raise ValueError(f"Request URL must be absolute http(s): {self.url!r}")

    @property
    def depth(self) -> int:
        """How many links away from a start URL this request is."""
        return int(self.meta.get("depth", 0))

    @property
    def retries(self) -> int:
        return int(self.meta.get("retry_times", 0))

    def replace(self, **changes: Any) -> Request:
        """A copy with some fields changed (``meta``/``options`` are copied)."""
        changes.setdefault("meta", dict(self.meta))
        changes.setdefault("options", dict(self.options))
        changes.setdefault("cb_kwargs", dict(self.cb_kwargs))
        return dataclasses.replace(self, **changes)

    def body_bytes(self) -> bytes:
        if self.json is not None:
            return _json.dumps(self.json, sort_keys=True, separators=(",", ":")).encode()
        if self.data is None:
            return b""
        if isinstance(self.data, bytes):
            return self.data
        if isinstance(self.data, str):
            return self.data.encode()
        if isinstance(self.data, Mapping):
            return urlencode(sorted((str(k), str(v)) for k, v in self.data.items())).encode()
        return repr(self.data).encode()

    def fingerprint(self) -> bytes:
        """Identity used for duplicate filtering (method + canonical URL + body)."""
        h = hashlib.sha1(self.method.encode())
        h.update(b"\0")
        h.update(canonicalize_url(self.url).encode())
        h.update(b"\0")
        h.update(self.body_bytes())
        return h.digest()

    # ------------------------------------------------------------------ #
    # checkpoint serialization
    # ------------------------------------------------------------------ #
    def to_dict(self, spider: Spider | None = None) -> dict[str, Any]:
        """Serialize (callbacks become spider method names)."""
        data = {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}
        data["callback"] = _callback_name(self.callback, spider, "callback")
        data["errback"] = _callback_name(self.errback, spider, "errback")
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any], spider: Spider | None = None) -> Request:
        data = dict(data)
        for key in ("callback", "errback"):
            name = data.get(key)
            if isinstance(name, str) and spider is not None:
                if not callable(getattr(spider, name, None)):
                    raise CheckpointError(f"Spider has no method {name!r} (needed by a saved request)")
                data[key] = getattr(spider, name)
        return cls(**data)

    def __repr__(self) -> str:
        extra = f" session={self.session}" if self.session else ""
        return f"<Request {self.method} {self.url}{extra}>"


def _callback_name(cb: Callback, spider: Spider | None, what: str) -> str | None:
    if cb is None or isinstance(cb, str):
        return cb
    owner = getattr(cb, "__self__", None)
    name = getattr(cb, "__name__", None)
    if owner is not None and (spider is None or owner is spider) and name and getattr(owner, name, None) == cb:
        return name
    raise CheckpointError(
        f"Request {what} {cb!r} is not a method of the spider, so the crawl cannot be paused/resumed. "
        f"Use a spider method (or its name) as the {what}."
    )
