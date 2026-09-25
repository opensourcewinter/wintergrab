"""Failure intelligence: turn a crawl's raw errors into diagnoses.

Instead of "403", a :class:`FailureTracker` reports::

    HTTP 403 on shop.example
      affected URLs: 14,921 (first 12:04:11, last 12:22:40)
      previous success: 18 min before the first failure
      likely cause: server-side access policy or rate limiting
      evidence: "Server: cloudflare"; challenge-page markers in the body
      crawler state: backing off (delay 8.0s, concurrency 1)

Causes are either **confirmed** (the evidence is conclusive: a 429 is the
server saying "too many requests"; a name that does not resolve does not
exist) or **likely** (a hypothesis from the evidence: a 403 may be a bot wall,
an IP ban or a real permission error). The two are kept apart, so a report
never claims more certainty than it has.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from ..errors import FetchError, HTTPStatusError, PolicyError, WintergrabError, category_of

if TYPE_CHECKING:
    from ..fetchers.response import Response
    from .throttle import AutoThrottle

__all__ = ["FailureDiagnosis", "FailureTracker"]

_MAX_SAMPLES = 5
_MAX_TRACKED_URLS = 50_000
_CDN_HEADERS = ("server", "via", "x-cache", "cf-ray", "x-served-by", "x-amz-cf-id", "x-akamai-transformed")


@dataclass
class FailureDiagnosis:
    """One kind of failure on one domain, with what is known and what is guessed."""

    domain: str
    signature: str  # "HTTP 403", "FetchTimeout", "NetworkError:dns", "blocked", "robots"...
    category: str
    attempts: int  # failed attempts, retries included
    failed_urls: int  # URLs given up on
    affected_urls: int  # distinct URLs that failed at least once
    sample_urls: list[str]
    first_seen: float
    last_seen: float
    last_success: float | None  # last success on the domain before the first failure
    confirmed_cause: str | None
    likely_cause: str | None
    evidence: list[str]
    crawler_state: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def describe(self) -> str:
        """A multi-line, human-readable report."""
        lines = [f"{self.signature} on {self.domain}"]
        lines.append(
            f"  affected URLs: {self.affected_urls:,} ({self.attempts:,} failed attempts, "
            f"{self.failed_urls:,} given up)"
        )
        if self.last_success is not None:
            lines.append(f"  previous success: {_ago(self.first_seen - self.last_success)} before the first failure")
        else:
            lines.append("  previous success: none on this domain")
        if self.confirmed_cause:
            lines.append(f"  cause (confirmed): {self.confirmed_cause}")
        if self.likely_cause:
            lines.append(f"  likely cause: {self.likely_cause}")
        if self.evidence:
            lines.append("  evidence: " + "; ".join(self.evidence))
        lines.append(f"  crawler state: {self.crawler_state}")
        if self.sample_urls:
            lines.append("  e.g. " + ", ".join(self.sample_urls[:3]))
        return "\n".join(lines)


def _ago(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


@dataclass
class _Group:
    domain: str
    signature: str
    category: str
    first_seen: float
    last_seen: float
    last_success: float | None
    attempts: int = 0
    failed: int = 0
    urls: set[str] = field(default_factory=set)
    url_count: int = 0
    samples: list[str] = field(default_factory=list)
    evidence: Counter[str] = field(default_factory=Counter)
    status: int | None = None
    kind: str | None = None
    error_type: str | None = None
    reason: str | None = None


def signature_of(
    error: BaseException | None, response: Response | None, blocked: bool, stage: str = "fetch"
) -> tuple[str, str]:
    """``(signature, category)`` grouping a failure with others of its kind."""
    if stage == "callback" and error is not None:
        return f"callback {type(error).__name__}", "callback"
    if response is not None:
        # The status says the most (a 429 is explicit rate limiting); a block page is evidence on top.
        if blocked and response.status < 300:
            return f"blocked (HTTP {response.status})", "blocked"
        return f"HTTP {response.status}", "http"
    if blocked:
        return "blocked", "blocked"
    if isinstance(error, HTTPStatusError):
        return f"HTTP {error.status}", "http"
    if isinstance(error, PolicyError):
        return error.policy if error.policy != "network" else "network policy", "policy"
    if isinstance(error, FetchError):
        name = type(error).__name__
        if error.kind and error.kind not in ("unknown", "timeout", "proxy", "policy", "cache", "browser"):
            name = f"{name}:{error.kind}"
        return name, error.category
    if error is not None:
        return type(error).__name__, category_of(error)
    return "unknown", "internal"


class FailureTracker:
    """Collects failures during a crawl and explains them (see the module docs)."""

    def __init__(self) -> None:
        self._groups: dict[tuple[str, str], _Group] = {}
        self._last_success: dict[str, float] = {}

    # -- recording -------------------------------------------------------- #
    def success(self, domain: str, when: float | None = None) -> None:
        self._last_success[domain] = when if when is not None else time.time()

    def failure(
        self,
        domain: str,
        url: str,
        *,
        error: BaseException | None = None,
        response: Response | None = None,
        blocked: bool = False,
        final: bool = False,
        when: float | None = None,
        stage: str = "fetch",
    ) -> None:
        """Record one failed attempt (``final`` when the URL was given up on).

        ``stage="callback"`` records an exception raised by the spider's own callback.
        """
        now = when if when is not None else time.time()
        signature, category = signature_of(error, response, blocked, stage)
        key = (domain, signature)
        group = self._groups.get(key)
        if group is None:
            group = self._groups[key] = _Group(domain, signature, category, now, now, self._last_success.get(domain))
        group.last_seen = now
        group.attempts += 1
        if final:
            group.failed += 1
        if url not in group.urls:
            if len(group.urls) < _MAX_TRACKED_URLS:
                group.urls.add(url)
            group.url_count += 1
            if len(group.samples) < _MAX_SAMPLES:
                group.samples.append(url)
        if response is not None:
            group.status = response.status
            self._collect_response_evidence(group, response, blocked)
        if error is not None:
            group.error_type = type(error).__name__
            if isinstance(error, FetchError):
                group.kind = error.kind
            if isinstance(error, PolicyError):
                group.reason = error.reason
            if isinstance(error, HTTPStatusError):
                group.status = error.status
                self._collect_response_evidence(group, error.response, blocked or bool(error.detail))
            elif not isinstance(error, WintergrabError) or stage == "callback":
                group.reason = f"{type(error).__name__}: {str(error)[:200]}"
                group.evidence[group.reason] += 1

    @staticmethod
    def _collect_response_evidence(group: _Group, response: Response, blocked: bool) -> None:
        from ..fetchers.blocking import has_challenge_markers

        headers = response.headers
        retry_after = headers.get("retry-after")
        if retry_after:
            group.evidence[f"Retry-After: {retry_after}"] += 1
        for name in _CDN_HEADERS:
            value = headers.get(name)
            if value and name in ("server", "via"):
                group.evidence[f"{name.title()}: {value[:60]}"] += 1
            elif value:
                group.evidence[f"{name} header present"] += 1
        if blocked or (response.body and has_challenge_markers(response.body)):
            group.evidence["challenge-page markers in the body"] += 1
        if response.status in (401, 407) and headers.get("www-authenticate"):
            group.evidence[f"WWW-Authenticate: {headers.get('www-authenticate', '')[:60]}"] += 1

    # -- reporting -------------------------------------------------------- #
    def __len__(self) -> int:
        return len(self._groups)

    def diagnose(self, throttle: AutoThrottle | None = None) -> list[FailureDiagnosis]:
        """One :class:`FailureDiagnosis` per (domain, kind of failure), most affected first."""
        out = []
        for group in self._groups.values():
            confirmed, likely = _cause(group)
            out.append(
                FailureDiagnosis(
                    domain=group.domain,
                    signature=group.signature,
                    category=group.category,
                    attempts=group.attempts,
                    failed_urls=group.failed,
                    affected_urls=group.url_count,
                    sample_urls=list(group.samples),
                    first_seen=group.first_seen,
                    last_seen=group.last_seen,
                    last_success=group.last_success,
                    confirmed_cause=confirmed,
                    likely_cause=likely,
                    evidence=[e for e, _ in group.evidence.most_common(4)],
                    crawler_state=_state(group.domain, throttle),
                )
            )
        out.sort(key=lambda d: (-d.affected_urls, -d.attempts, d.domain, d.signature))
        return out

    def report(self, throttle: AutoThrottle | None = None, limit: int = 10) -> str:
        diagnoses = self.diagnose(throttle)
        if not diagnoses:
            return "no failures"
        parts = [d.describe() for d in diagnoses[:limit]]
        if len(diagnoses) > limit:
            parts.append(f"... and {len(diagnoses) - limit} more kinds of failure")
        return "\n\n".join(parts)


def _state(domain: str, throttle: AutoThrottle | None) -> str:
    if throttle is None or domain not in throttle.slots:
        return "unknown"
    return throttle.describe(domain)


def _cause(group: _Group) -> tuple[str | None, str | None]:
    """``(confirmed, likely)`` causes for a failure group."""
    status = group.status
    markers = any("challenge-page markers" in e for e in group.evidence)
    signature = group.signature
    if group.category == "callback":
        return f"the spider's callback raised {group.reason}", None
    if group.category == "blocked":
        return None, "bot protection: a challenge or block page was served" + (
            f" with status {status}" if status else ""
        )
    if signature == "robots":
        return "disallowed by robots.txt", None
    if signature == "network policy":
        return (
            f"refused by the network policy ({group.reason})" if group.reason else "refused by the network policy",
            None,
        )
    if status is not None:
        if status == 429:
            return "the server is rate limiting (HTTP 429 Too Many Requests)", None
        if status == 403:
            if markers:
                return None, "bot protection or a web application firewall (challenge page served)"
            return None, "server-side access policy or rate limiting"
        if status in (401, 407):
            what = "proxy authentication" if status == 407 else "authentication"
            return f"{what} required (HTTP {status})", None
        if status in (404, 410):
            return f"the page does not exist (HTTP {status})", None
        if status == 503 and markers:
            return None, "bot protection challenge (HTTP 503 with a challenge page)"
        if status in (502, 504):
            return None, f"gateway failure between a proxy/CDN and the origin server (HTTP {status})"
        if status >= 500:
            return None, f"server error or overload (HTTP {status})"
        if status >= 400:
            return None, f"request rejected by the server (HTTP {status})"
    kind = group.kind
    if group.category == "timeout" or kind == "timeout":
        return None, "the server is slow or overloaded, or the network is congested"
    if kind == "dns":
        return "the host name does not resolve (DNS)", None
    if kind == "tls":
        if group.error_type and "Certificate" in (group.error_type or ""):
            return "invalid TLS certificate", None
        return None, "TLS handshake failure"
    if kind == "proxy":
        return None, "the proxy failed or refused the connection"
    if kind == "connect":
        return None, "the server is down or refusing connections"
    if kind == "redirects":
        return "redirect loop (too many redirects)", None
    if kind == "invalid_url":
        return "invalid URL", None
    if kind == "cache":
        return "not in the HTTP cache (offline mode)", None
    if group.category == "browser":
        return None, "the page failed to load or render in the browser"
    return None, None
