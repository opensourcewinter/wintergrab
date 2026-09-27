"""Per-domain speed control that backs off when a site pushes back."""

from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DomainSlot:
    """Live throttle state for one domain."""

    domain: str
    concurrency: int
    delay: float
    active: int = 0
    next_start: float = 0.0
    paused_until: float = 0.0
    successes: int = 0
    backoffs: int = 0
    min_delay: float = 0.0
    latencies: list[float] = field(default_factory=list)
    requests: int = 0  # requests started (for rate measurements)
    last_pushback: float = 0.0  # monotonic time of the last push-back
    outcomes: deque[bool] = field(default_factory=lambda: deque(maxlen=20))  # the last responses: ok, or failed
    rate_limit: tuple[int | None, float | None] | None = None  # the site's last RateLimit headers: remaining, reset
    rate_limited: int = 0  # pauses because the site's rate limit was used up

    def ready_at(self) -> float:
        return max(self.next_start, self.paused_until)

    @property
    def avg_latency(self) -> float | None:
        return sum(self.latencies) / len(self.latencies) if self.latencies else None


class AutoThrottle:
    """Adaptive, per-domain request pacing (AIMD - like TCP congestion control).

    * Each domain gets a *slot* with an allowed concurrency and a delay
      between request starts.
    * While responses are healthy the delay drifts towards
      ``latency / target_concurrency`` and concurrency creeps back up by one
      every ``increase_every`` successes.
    * On push-back (HTTP 429/503, a detected block page, timeouts) the delay
      is multiplied by ``backoff_factor`` and concurrency is halved. A
      ``Retry-After`` header pauses the domain for that long.
    * ``RateLimit`` headers (and ``X-RateLimit-``) are the site's own limit: a
      used-up window pauses the domain until it resets, a low remainder spaces
      requests so that the window lasts. They are honoured with ``enabled=False``
      too, like ``Retry-After``.
    * Errors that keep coming (timeouts, connection errors: at least
      ``error_rate_backoff`` of the last responses, ten or more seen) halve
      concurrency too, so a site in trouble is not hit in parallel.

    Args:
        enabled: With ``False`` the delay/concurrency stay fixed (``Retry-After``
            is still honoured).
        base_delay: Minimum delay between requests to one domain (seconds).
        max_delay: Upper bound for the delay.
        max_concurrency: Maximum parallel requests per domain.
        target_concurrency: Average parallel requests to aim for (defaults to
            ``max_concurrency``). Lower it to be gentler.
        backoff_factor: Delay multiplier on push-back.
        min_backoff_delay: Delay used on the first push-back if the current
            delay is smaller.
        recovery: Factor applied to an inflated delay on each success.
        increase_every: Successes needed before concurrency grows by one.
        randomize: Jitter each delay by +/-50% so traffic looks less robotic.
        error_rate_backoff: The share of failed responses among the last ones from which concurrency halves.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        base_delay: float = 0.0,
        max_delay: float = 60.0,
        max_concurrency: int = 4,
        target_concurrency: float | None = None,
        backoff_factor: float = 2.0,
        min_backoff_delay: float = 1.0,
        recovery: float = 0.85,
        increase_every: int = 10,
        randomize: bool = True,
        error_rate_backoff: float = 0.5,
    ) -> None:
        self.enabled = enabled
        self.base_delay = max(0.0, base_delay)
        self.max_delay = max(self.base_delay, max_delay)
        self.max_concurrency = max(1, max_concurrency)
        self.target_concurrency = max(0.1, target_concurrency or float(self.max_concurrency))
        self.backoff_factor = max(1.0, backoff_factor)
        self.min_backoff_delay = min_backoff_delay
        self.recovery = min(max(recovery, 0.1), 1.0)
        self.increase_every = max(1, increase_every)
        self.randomize = randomize
        self.error_rate_backoff = min(max(error_rate_backoff, 0.05), 1.0)
        self.slots: dict[str, DomainSlot] = {}

    def slot(self, domain: str) -> DomainSlot:
        slot = self.slots.get(domain)
        if slot is None:
            slot = self.slots[domain] = DomainSlot(domain, self.max_concurrency, self.base_delay)
        return slot

    # -- dispatch --------------------------------------------------------- #
    def can_start(self, slot: DomainSlot, now: float) -> bool:
        return slot.active < slot.concurrency and now >= slot.ready_at()

    def on_start(self, slot: DomainSlot, now: float) -> None:
        slot.active += 1
        slot.requests += 1
        delay = slot.delay
        if self.randomize and delay > 0:
            delay *= random.uniform(0.5, 1.5)
        slot.next_start = now + delay

    def on_finish(self, slot: DomainSlot) -> None:
        slot.active = max(0, slot.active - 1)

    # -- feedback --------------------------------------------------------- #
    def on_success(self, domain: str, latency: float) -> None:
        slot = self.slot(domain)
        slot.latencies = [*slot.latencies[-19:], latency]
        slot.outcomes.append(True)
        if not self.enabled:
            return
        floor = max(self.base_delay, slot.min_delay)
        target = max(floor, latency / self.target_concurrency)
        if slot.delay > target:
            slot.delay = max(target, slot.delay * self.recovery)
        else:
            slot.delay = (slot.delay + target) / 2
        slot.delay = min(self.max_delay, max(floor, slot.delay))
        slot.successes += 1
        if slot.concurrency < self.max_concurrency and slot.successes >= self.increase_every:
            slot.concurrency += 1
            slot.successes = 0

    def on_pushback(self, domain: str, retry_after: float | None = None) -> None:
        """The site said "slow down" (429/503/block page)."""
        slot = self.slot(domain)
        slot.backoffs += 1
        slot.successes = 0
        slot.last_pushback = time.monotonic()
        if retry_after:
            slot.paused_until = max(slot.paused_until, time.monotonic() + retry_after)
        if not self.enabled:
            return
        slot.concurrency = max(1, slot.concurrency // 2)
        slot.delay = min(
            self.max_delay, max(slot.delay * self.backoff_factor, self.min_backoff_delay, retry_after or 0.0)
        )
        # Apply the new delay right away, not only after the next request starts.
        slot.next_start = max(slot.next_start, time.monotonic() + slot.delay)

    def on_error(self, domain: str) -> None:
        """A timeout or connection error: back off gently; errors that keep coming halve concurrency too."""
        slot = self.slot(domain)
        slot.outcomes.append(False)
        if not self.enabled:
            return
        slot.successes = 0
        slot.delay = min(self.max_delay, max(slot.delay * 1.5, self.min_backoff_delay / 2))
        rate = self.error_rate(slot)
        if rate is not None and rate >= self.error_rate_backoff and len(slot.outcomes) >= 10 and slot.concurrency > 1:
            slot.concurrency = max(1, slot.concurrency // 2)
            slot.outcomes.clear()  # (judged again on the next ten)

    def on_rate_limit(self, domain: str, remaining: int | None, reset: float | None) -> bool:
        """The site's ``RateLimit`` headers: ``remaining`` requests in the window that ends in ``reset`` seconds.
        Used up, the domain pauses until then (``True``); low, requests are spaced so that the window lasts."""
        slot = self.slot(domain)
        slot.rate_limit = (remaining, reset)
        if remaining is None or not reset:
            return False
        now = time.monotonic()
        if remaining <= 0:
            slot.rate_limited += 1
            slot.paused_until = max(slot.paused_until, now + reset)
            return True
        spacing = reset / remaining
        if spacing > slot.delay:
            slot.delay = min(self.max_delay, spacing)
            slot.next_start = max(slot.next_start, now + slot.delay)
        return False

    @staticmethod
    def error_rate(slot: DomainSlot) -> float | None:
        """The share of the last responses (at most 20) that failed; ``None`` before any."""
        outcomes = slot.outcomes
        return round(sum(1 for ok in outcomes if not ok) / len(outcomes), 3) if outcomes else None

    def set_min_delay(self, domain: str, delay: float) -> None:
        """Enforce a floor (e.g. a robots.txt ``Crawl-delay``)."""
        slot = self.slot(domain)
        slot.min_delay = max(slot.min_delay, delay)
        slot.delay = max(slot.delay, delay)

    # -- introspection ---------------------------------------------------- #
    def target_delay(self, slot: DomainSlot) -> float:
        """The delay healthy responses pull towards: ``latency / target_concurrency`` (at least the floor)."""
        floor = max(self.base_delay, slot.min_delay)
        latency = slot.avg_latency
        if latency is None or not self.enabled:
            return max(floor, slot.delay if not self.enabled else floor)
        return min(self.max_delay, max(floor, latency / self.target_concurrency))

    def mode(self, slot: DomainSlot, now: float | None = None) -> str:
        """``"paused"`` (honouring Retry-After), ``"backing off"`` (push-back in the last 30 s),
        ``"recovering"`` (slower than the target after a push-back) or ``"normal"``."""
        now = time.monotonic() if now is None else now
        if slot.paused_until > now:
            return "paused"
        if slot.last_pushback and now - slot.last_pushback < 30:
            return "backing off"
        if slot.last_pushback and (
            slot.concurrency < self.max_concurrency or slot.delay > self.target_delay(slot) * 1.5 + 1e-9
        ):
            return "recovering"
        return "normal"

    def state(self, domain: str, now: float | None = None) -> dict[str, Any]:
        """Live throttle state of a domain: delays, concurrency, latency, target rate and back-off mode."""
        slot = self.slot(domain)
        now = time.monotonic() if now is None else now
        latency = slot.avg_latency
        target_delay = self.target_delay(slot)
        # Requests per second the current settings allow: spacing (1/delay) and parallelism
        # (concurrency/latency), whichever binds first.
        limits = []
        if slot.delay > 0:
            limits.append(1.0 / slot.delay)
        if latency:
            limits.append(slot.concurrency / latency)
        return {
            "domain": domain,
            "mode": self.mode(slot, now),
            "active": slot.active,
            "concurrency": slot.concurrency,
            "max_concurrency": self.max_concurrency,
            "delay": round(slot.delay, 4),
            "target_delay": round(target_delay, 4),
            "min_delay": slot.min_delay,
            "avg_latency": round(latency, 4) if latency is not None else None,
            "allowed_rate": round(min(limits), 3) if limits else None,
            "paused_for": round(max(0.0, slot.paused_until - now), 2),
            "backoffs": slot.backoffs,
            "requests": slot.requests,
            "error_rate": self.error_rate(slot),
            "rate_limit": (
                {"remaining": slot.rate_limit[0], "reset": slot.rate_limit[1], "pauses": slot.rate_limited}
                if slot.rate_limit is not None
                else None
            ),
        }

    def describe(self, domain: str) -> str:
        """One line for reports, e.g. ``"backing off (delay 8.0s, concurrency 1/4)"``."""
        st = self.state(domain)
        detail = f"delay {st['delay']:.1f}s, concurrency {st['concurrency']}/{st['max_concurrency']}"
        if st["paused_for"]:
            detail = f"paused {st['paused_for']:.0f}s more, " + detail
        return f"{st['mode']} ({detail})"

    # -- persistence ------------------------------------------------------ #
    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            d: {"delay": s.delay, "concurrency": s.concurrency, "min_delay": s.min_delay, "backoffs": s.backoffs}
            for d, s in self.slots.items()
        }

    def restore(self, data: dict[str, dict[str, Any]]) -> None:
        for domain, values in (data or {}).items():
            slot = self.slot(domain)
            slot.delay = float(values.get("delay", slot.delay))
            slot.concurrency = int(values.get("concurrency", slot.concurrency))
            slot.min_delay = float(values.get("min_delay", 0.0))
            slot.backoffs = int(values.get("backoffs", 0))
