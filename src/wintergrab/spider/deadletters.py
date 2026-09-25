"""The dead-letter queue: requests a crawl gave up on, kept so they can be retried later.

With a ``crawl_dir``, every request that finally fails (after its retries) is
appended to ``crawl_dir/dead_letters.jsonl``: a readable summary (URL, error,
status, attempts...) plus the full request, so ``Spider(retry_dead_letters=True)``
(``wintergrab crawl ... --retry-failed``) can queue them again without
re-running the whole crawl.

The ``request`` field is a pickled request, like the rest of the crawl state:
only load dead letters you created.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import pickle
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import CheckpointError, FetchError, HTTPStatusError, category_of, describe
from ..request import Request

if TYPE_CHECKING:
    from .spider import Spider

__all__ = ["DeadLetterQueue"]

log = logging.getLogger("wintergrab.spider")


class DeadLetterQueue:
    """Append-only JSON Lines file of failed requests."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.added = 0

    def add(self, request: Request, error: BaseException, spider: Spider | None = None) -> None:
        """Record a request that was given up on."""
        entry: dict[str, Any] = {
            "time": round(time.time(), 3),
            "url": request.url,
            "method": request.method,
            "error": describe(error),
            "category": category_of(error),
            "attempts": request.retries + 1,
            "depth": request.depth,
        }
        if isinstance(error, FetchError):
            entry["kind"] = error.kind
        if isinstance(error, HTTPStatusError):
            entry["status"] = error.status
        try:
            blob = pickle.dumps(request.to_dict(spider), protocol=pickle.HIGHEST_PROTOCOL)
            entry["request"] = base64.b64encode(blob).decode("ascii")
        except Exception as exc:  # a lambda callback, unpicklable meta: keep the summary anyway
            entry["request_error"] = describe(exc)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self.added += 1

    def entries(self) -> Iterator[dict[str, Any]]:
        """The recorded entries (malformed lines, e.g. from a crash mid-write, are skipped)."""
        if not self.path.exists():
            return
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    yield entry

    def __len__(self) -> int:
        return sum(1 for _ in self.entries())

    def requests(self, spider: Spider | None = None) -> list[Request]:
        """The failed requests, ready to queue again (retry counters reset, duplicate filter bypassed)."""
        out: list[Request] = []
        seen: set[bytes] = set()
        for entry in self.entries():
            blob = entry.get("request")
            if not blob:
                log.warning("dead letter for %s cannot be retried: %s", entry.get("url"), entry.get("request_error"))
                continue
            try:
                data = pickle.loads(base64.b64decode(blob))
                request = Request.from_dict(data, spider)
            except CheckpointError:
                raise
            except Exception as exc:
                log.warning("skipping unreadable dead letter for %s: %s", entry.get("url"), describe(exc))
                continue
            fp = request.fingerprint()
            if fp in seen:
                continue
            seen.add(fp)
            request.meta.pop("retry_times", None)
            request.dont_filter = True
            out.append(request)
        return out

    def clear(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)

    def __repr__(self) -> str:
        return f"DeadLetterQueue({str(self.path)!r})"
