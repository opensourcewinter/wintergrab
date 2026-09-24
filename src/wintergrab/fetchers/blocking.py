"""Heuristics for spotting bot walls, CAPTCHAs and rate limiting."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .response import Response

# Phrases that appear on common "checking your browser" / block pages.
CHALLENGE_MARKERS: tuple[str, ...] = (
    "just a moment...",
    "checking your browser",
    "attention required! | cloudflare",
    "cf-browser-verification",
    "challenge-platform",
    "cf-chl-",
    "verify you are human",
    "are you a robot",
    "ddos-guard",
    "please enable js and disable any ad blocker",
    "px-captcha",
    "_incapsula_resource",
    "request unsuccessful. incapsula",
    "<title>access denied</title>",
    "<title>client challenge</title>",  # Fastly (served with status 200)
    "/_fs-ch-",  # Fastly challenge assets
    "captcha-delivery.com",  # DataDome
)

BLOCK_STATUSES = frozenset({403, 429})


_BYTE_MARKERS: tuple[bytes, ...] = tuple(marker.encode() for marker in CHALLENGE_MARKERS)


def has_challenge_markers(text: str | bytes) -> bool:
    """``True`` if ``text`` (HTML or visible text, str or raw bytes) looks like a bot-check page.

    Raw bytes are searched without decoding them first: the markers are ASCII.
    """
    if isinstance(text, bytes):
        raw = text[:60_000].lower()
        return any(marker in raw for marker in _BYTE_MARKERS)
    low = text[:60_000].lower()
    return any(marker in low for marker in CHALLENGE_MARKERS)


def looks_blocked(response: Response) -> bool:
    """Best-effort guess that a response is a block/challenge page, not content.

    * 429 (Too Many Requests) is always a block.
    * 403/503 count when the body looks like a challenge page (or is empty).
    * A 200 counts only if the page is small and full of challenge markers.
    """
    status = response.status
    if status == 429:
        return True
    if status in (403, 503):
        return len(response.body) < 512 or has_challenge_markers(response.body)
    if status == 200 and len(response.body) < 30_000 and response.is_html:
        return has_challenge_markers(response.body)
    return False
