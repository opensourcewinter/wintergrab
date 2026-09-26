"""Which technologies a site uses: CMS, e-commerce platform, JavaScript frameworks, analytics, CDN...

::

    >>> detect_technologies(response)
    [Technology('Cloudflare', 'cdn', confidence=0.99, evidence=['header server: cloudflare', 'header cf-ray']),
     Technology('WordPress', 'cms', version='6.4.2', confidence=0.99, evidence=['meta generator: WordPress 6.4.2', ...]),
     ...]

Detection reads what the server sent: headers, cookie names, ``<meta
name=generator>``, the URLs of scripts and stylesheets, distinctive HTML
markers and the page URL, and matches them against fingerprints
(:data:`~wintergrab.intel.techrules.RULES`; add your own with
:class:`TechDetector`). Nothing is executed and no extra request is made.

Confidence combines the evidence: a header or a generator tag is strong
(0.9), a script URL good (0.8), a cookie name fair (0.75), an HTML marker
weaker (0.6); several signals combine as ``1 - (1 - p1)(1 - p2)...``.
Technologies implied by others (WooCommerce implies WordPress) get half their
parent's confidence unless seen themselves.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..extraction.page import PageContext
from .techrules import RULES, TechRule

__all__ = ["TechDetector", "Technology", "detect_technologies"]

_WEIGHTS = {"header": 0.9, "meta": 0.9, "url": 0.9, "script": 0.8, "cookie": 0.75, "html": 0.6}
_MAX_HTML = 500_000


@dataclass
class Technology:
    """A detected technology.

    Attributes:
        name: e.g. ``"WordPress"``.
        category: e.g. ``"cms"``, ``"ecommerce"``, ``"js-framework"``, ``"analytics"``, ``"cdn"``.
        confidence: 0-1, from the evidence (see the module docs).
        version: When a signal showed it (``"6.4.2"``).
        evidence: What was seen: ``"header server: nginx/1.25"``, ``"script cdn.shopify.com/s/..."``.
    """

    name: str
    category: str
    confidence: float
    version: str | None = None
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "category": self.category, "confidence": self.confidence}
        if self.version:
            out["version"] = self.version
        out["evidence"] = list(self.evidence)
        return out

    def __repr__(self) -> str:
        version = f", version={self.version!r}" if self.version else ""
        return (
            f"Technology({self.name!r}, {self.category!r}{version}, confidence={self.confidence}, "
            f"evidence={self.evidence!r})"
        )


_INLINE_FLAGS = re.compile(r"\(\?-?[aiLmsux]")
_RUN_ENDING_ESCAPES = frozenset("bBdDsSwWAZ")


def _skip_group(pattern: str, i: int) -> int:
    """Index just past the group or character class opening at ``pattern[i]``."""
    depth = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "[":
            i += 1
            if i < len(pattern) and pattern[i] == "^":
                i += 1
            if i < len(pattern) and pattern[i] == "]":
                i += 1  # a leading "]" is literal
            while i < len(pattern) and pattern[i] != "]":
                i += 2 if pattern[i] == "\\" else 1
            if depth == 0:
                return i + 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def _literals(pattern: str) -> tuple[str | None, list[str]]:
    """The literal texts every match of ``pattern`` contains, lower-cased: ``(prefix, required)``.

    ``required`` lists the literal runs (at least 3 characters) a match must
    contain, longest first; ``prefix`` is the one a match starts with, if any.
    They let a case-insensitive pattern be skipped, or started late, with a
    fast substring search: ``re`` cannot search for a literal prefix when
    ignoring case. Patterns with a top-level alternative, inline flags or
    unusual escapes give ``(None, [])``: always run.
    """
    if _INLINE_FLAGS.search(pattern):
        return None, []
    runs: list[str] = []
    current: list[str] = []
    prefix_open = True  # nothing but \b seen yet: the first run is the prefix
    prefix: str | None = None

    def end_run() -> None:
        nonlocal prefix, prefix_open
        if current:
            text = "".join(current).lower()
            runs.append(text)
            if prefix_open:
                prefix = text
            current.clear()
        prefix_open = False

    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "|":
            return None, []  # alternatives: no literal is required
        if ch in "([":
            end_run()
            i = _skip_group(pattern, i)
            continue
        if ch in ".^$?*+{}":
            end_run()
            if ch == "{":
                i = pattern.find("}", i) + 1 or len(pattern)
                continue
            i += 1
            continue
        if ch == "\\":
            if i + 1 >= len(pattern):
                return None, []
            escaped = pattern[i + 1]
            if escaped.isalnum():
                if escaped not in _RUN_ENDING_ESCAPES:
                    return None, []  # \x41, \n, \1...: not worth reading
                if not (escaped == "b" and not current and prefix_open):
                    end_run()  # (a leading \b keeps the prefix open)
                i += 2
                continue
            ch, i = escaped, i + 1
        following = pattern[i + 1] if i + 1 < len(pattern) else ""
        if following and following in "?*{":
            end_run()  # this character is optional or repeated
        else:
            current.append(ch)
            if following == "+":
                end_run()  # required once, then repeated
        i += 1
    end_run()
    required = sorted(dict.fromkeys(run for run in runs if len(run) >= 3 and run.isascii()), key=len, reverse=True)
    return (prefix if prefix in required else None), required


@dataclass
class _Compiled:
    rule: TechRule
    headers: list[tuple[str, re.Pattern[str] | None]]
    cookies: list[re.Pattern[str]]
    meta: list[tuple[str, re.Pattern[str]]]
    scripts: list[re.Pattern[str]]
    html: list[tuple[str | None, list[str], re.Pattern[str]]]
    url: list[re.Pattern[str]]


def _compile(rule: TechRule) -> _Compiled:
    def one(pattern: str) -> re.Pattern[str]:
        return re.compile(pattern, re.I)

    return _Compiled(
        rule,
        [(name.lower(), one(p) if p else None) for name, p in rule.headers.items()],
        [one(p) for p in rule.cookies],
        [(name.lower(), one(p)) for name, p in rule.meta.items()],
        [one(p) for p in rule.scripts],
        [(*_literals(p), one(p)) for p in rule.html],
        [one(p) for p in rule.url],
    )


def _cookie_names(headers: Mapping[str, str], cookies: Iterable[str]) -> set[str]:
    names = set(cookies)
    get_list = getattr(headers, "get_list", None)
    values = (
        get_list("set-cookie") if callable(get_list) else [v for k, v in headers.items() if k.lower() == "set-cookie"]
    )
    for value in values:
        name = value.split("=", 1)[0].strip()
        if name:
            names.add(name)
    return names


class _Evidence:
    """What one rule saw on the page."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []
        self.version: str | None = None

    def add(self, kind: str, text: str, match: re.Match[str] | None = None) -> None:
        self.items.append((kind, text))
        if match is not None:
            self.read_version(match)

    def read_version(self, match: re.Match[str]) -> None:
        if self.version is None and "version" in match.re.groupindex:
            self.version = match.group("version") or None


class TechDetector:
    """Technology detection with a set of fingerprints (the built-in ones plus ``extra``)."""

    def __init__(self, rules: Iterable[TechRule] = RULES, extra: Iterable[TechRule] = ()) -> None:
        self.rules = [*rules, *extra]
        self._compiled = [_compile(rule) for rule in self.rules]
        self._by_name = {rule.name: rule for rule in self.rules}

    def detect(
        self,
        page: Any,
        *,
        url: str | None = None,
        headers: Mapping[str, str] | None = None,
        cookies: Iterable[str] = (),
        html: str | None = None,
    ) -> list[Technology]:
        """Technologies seen on ``page`` (a :class:`~wintergrab.Response`, a Selector or HTML), best first.

        With a Response, its headers, cookies and body are used; otherwise pass
        ``headers`` (and ``cookies``) yourself.
        """
        ctx = page if isinstance(page, PageContext) else PageContext(page, url=url)
        response = ctx.response
        header_map: Mapping[str, str] = {}
        if headers is not None:
            header_map = headers
        elif response is not None:
            header_map = response.headers
        lowered = {str(k).lower(): str(v) for k, v in header_map.items()}
        cookie_names = _cookie_names(header_map, [*cookies, *(response.cookies if response is not None else {})])
        if html is None:
            if response is not None:
                html = response.text
            elif isinstance(page, str):
                html = page
            else:
                html = ctx.selector.html
        html = html[:_MAX_HTML]
        html_lower = html.lower()
        aligned = len(html_lower) == len(html)  # lower() can lengthen a few characters
        root = ctx.selector
        assets = [el.attr("src") or "" for el in root.css("script[src]")]
        assets += [el.attr("href") or "" for el in root.css("link[href]")]
        meta: dict[str, str] = {}
        for el in root.css("meta[name], meta[property]"):
            key = (el.attr("name") or el.attr("property") or "").strip().lower()
            if key and key not in meta:
                meta[key] = el.attr("content") or ""
        page_url = ctx.url or ""
        found: dict[str, Technology] = {}
        for compiled in self._compiled:
            seen = _Evidence()
            for name, pattern in compiled.headers:
                value = lowered.get(name)
                if value is None:
                    continue
                if pattern is None:
                    seen.add("header", f"header {name}")
                elif (m := pattern.search(value)) is not None:
                    seen.add("header", f"header {name}: {value[:60]}", m)
            for pattern in compiled.cookies:
                hit = next((c for c in cookie_names if pattern.search(c)), None)
                if hit:
                    seen.add("cookie", f"cookie {hit}")
            for name, pattern in compiled.meta:
                value = meta.get(name)
                if value is not None and (m := pattern.search(value)) is not None:
                    seen.add("meta", f"meta {name}: {value[:60]}", m)
            counted: set[str] = set()
            for pattern in compiled.scripts:
                for asset in assets:
                    if (m := pattern.search(asset)) is not None:
                        if asset in counted:
                            seen.read_version(m)  # one asset is one observation, whatever matches it
                        else:
                            counted.add(asset)
                            seen.add("script", f"asset {asset[:80]}", m)
                        break
            for prefix, required, pattern in compiled.html:
                if any(literal not in html_lower for literal in required):
                    continue  # the pattern cannot match: don't scan the page with it
                start = html_lower.find(prefix) if prefix is not None and aligned else 0
                if (m := pattern.search(html, start)) is not None:
                    seen.add("html", f"html {m.group(0)[:60]!r}", m)
            for pattern in compiled.url:
                if (m := pattern.search(page_url)) is not None:
                    seen.add("url", f"url {page_url[:80]}", m)
            if seen.items:
                confidence = 1 - math.prod(1 - _WEIGHTS[kind] for kind, _ in seen.items)
                rule = compiled.rule
                texts = [text for _, text in seen.items]
                found[rule.name] = Technology(
                    rule.name, rule.category, round(min(confidence, 0.99), 3), seen.version, texts
                )
        self._add_implied(found)
        return sorted(found.values(), key=lambda t: (-t.confidence, t.name))

    def _add_implied(self, found: dict[str, Technology]) -> None:
        """Technologies implied by others (WooCommerce -> WordPress -> PHP), with half the parent's confidence."""
        queue = list(found.values())
        while queue:
            parent = queue.pop()
            parent_rule = self._by_name.get(parent.name)
            for implied in parent_rule.implies if parent_rule is not None else ():
                confidence = round(parent.confidence * 0.5, 3)
                existing = found.get(implied)
                if existing is None:
                    rule = self._by_name.get(implied)
                    category = rule.category if rule is not None else "other"
                    tech = Technology(implied, category, confidence, None, [f"implied by {parent.name}"])
                    found[implied] = tech
                    queue.append(tech)
                elif f"implied by {parent.name}" not in existing.evidence:
                    existing.evidence.append(f"implied by {parent.name}")
                    existing.confidence = round(min(0.99, 1 - (1 - existing.confidence) * (1 - confidence)), 3)


_DEFAULT: TechDetector | None = None


def detect_technologies(page: Any, **kwargs: Any) -> list[Technology]:
    """:meth:`TechDetector.detect` with the built-in fingerprints."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = TechDetector()
    return _DEFAULT.detect(page, **kwargs)
