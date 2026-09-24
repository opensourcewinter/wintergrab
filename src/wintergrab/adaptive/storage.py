"""Where adaptive selectors remember the elements they matched."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit


@runtime_checkable
class AdaptiveStorage(Protocol):
    """Anything with ``save``/``load``/``delete`` can store element fingerprints."""

    def save(self, domain: str, identifier: str, record: dict[str, Any]) -> None: ...

    def load(self, domain: str, identifier: str) -> dict[str, Any] | None: ...

    def delete(self, domain: str, identifier: str) -> None: ...


class MemoryStorage:
    """Keeps fingerprints in a dict. Handy for tests and short scripts."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def save(self, domain: str, identifier: str, record: dict[str, Any]) -> None:
        with self._lock:
            self._data[(domain, identifier)] = json.loads(json.dumps(record))

    def load(self, domain: str, identifier: str) -> dict[str, Any] | None:
        with self._lock:
            return self._data.get((domain, identifier))

    def delete(self, domain: str, identifier: str) -> None:
        with self._lock:
            self._data.pop((domain, identifier), None)

    def __len__(self) -> int:
        return len(self._data)


class SQLiteStorage:
    """Stores fingerprints in a small SQLite database (safe across threads).

    Args:
        path: Database file. Defaults to :func:`default_storage_path`.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path else default_storage_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS elements ("
            " domain TEXT NOT NULL, identifier TEXT NOT NULL, record TEXT NOT NULL, updated REAL NOT NULL,"
            " PRIMARY KEY (domain, identifier))"
        )

    def save(self, domain: str, identifier: str, record: dict[str, Any]) -> None:
        payload = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO elements (domain, identifier, record, updated) VALUES (?, ?, ?, ?)",
                (domain, identifier, payload, time.time()),
            )

    def load(self, domain: str, identifier: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT record FROM elements WHERE domain = ? AND identifier = ?", (domain, identifier)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def delete(self, domain: str, identifier: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM elements WHERE domain = ? AND identifier = ?", (domain, identifier))

    def identifiers(self, domain: str | None = None) -> list[tuple[str, str]]:
        """List saved ``(domain, identifier)`` pairs."""
        with self._lock:
            if domain is None:
                rows = self._conn.execute("SELECT domain, identifier FROM elements ORDER BY domain, identifier")
            else:
                rows = self._conn.execute(
                    "SELECT domain, identifier FROM elements WHERE domain = ? ORDER BY identifier", (domain,)
                )
            return [(r[0], r[1]) for r in rows.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __repr__(self) -> str:
        return f"SQLiteStorage({str(self.path)!r})"


def default_storage_path() -> Path:
    """``$WINTERGRAB_ADAPTIVE_DB`` or a file in the user's cache directory."""
    env = os.environ.get("WINTERGRAB_ADAPTIVE_DB")
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "wintergrab" / "adaptive.sqlite3"


_default: SQLiteStorage | None = None
_default_lock = threading.Lock()


def default_storage() -> SQLiteStorage:
    """The shared default :class:`SQLiteStorage` (created on first use)."""
    global _default
    with _default_lock:
        path = default_storage_path()
        if _default is None or _default.path != path:
            _default = SQLiteStorage(path)
        return _default


def storage_key(url: str | None) -> str:
    """The domain part of the storage key for a page URL."""
    if not url:
        return "default"
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or "default"
