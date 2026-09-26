"""Object storage: any file output in an S3 bucket (``output = "s3://bucket/crawls/books.jsonl"``).

Needs s3fs (``pip install "wintergrab[s3]"``). The key's extension picks the format, as a file's does:
``.jsonl``, ``.json``, ``.csv``, ``.parquet``, ``.xlsx``, ``.duckdb``, ``.sqlite``... While the crawl runs, the items are
written to a local file under ``.wintergrab/uploads/`` (``WINTERGRAB_UPLOADS`` to put it elsewhere), and the
file is uploaded when the crawl ends: the object appears whole. A crawl that stops keeps its items there, and
the resumed crawl continues them (from the object itself, if the local file is gone) and uploads them.

Credentials are the environment's, as the AWS tools read them: ``AWS_ACCESS_KEY_ID`` and
``AWS_SECRET_ACCESS_KEY``, ``AWS_PROFILE`` and ``~/.aws/credentials``, a machine's role... never the URL.
S3-compatible stores (MinIO, Cloudflare R2, Backblaze B2...) take ``?endpoint_url=https://...`` (or
``AWS_ENDPOINT_URL``); ``?region=`` and ``?profile=`` are read too. Any other parameter is an error.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from ..errors import ConfigurationError, ExportError
from ..spider.exporters import EXPORTERS, Exporter, open_exporter
from .common import require

__all__ = ["ObjectExporter", "read_object"]

#: The URL's parameters, as s3fs takes them.
_OPTIONS = ("endpoint_url", "region", "profile")


def _s3fs() -> Any:
    return require("s3fs", "s3", "S3 output")


def parse_target(url: str) -> tuple[dict[str, Any], str]:
    """s3fs's options for ``url``, and the object's path (``bucket/key``)."""
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise ConfigurationError(
            "an S3 URL holds no credentials: they are read from the environment (AWS_ACCESS_KEY_ID...)",
            key="output",
        )
    bucket, key = parts.hostname or "", unquote(parts.path.lstrip("/"))
    if not bucket or not key or key.endswith("/"):
        raise ConfigurationError(f"{url}: name the bucket and the object, as in s3://BUCKET/items.jsonl", key="output")
    options: dict[str, Any] = {}
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if name == "endpoint_url":
            options.setdefault("client_kwargs", {})["endpoint_url"] = value
        elif name == "region":
            options.setdefault("client_kwargs", {})["region_name"] = value
        elif name == "profile":
            options["profile"] = value
        else:
            raise ConfigurationError(
                f"unknown option {name!r} in the S3 URL (known: {', '.join(_OPTIONS)})", key="output"
            )
    return options, f"{bucket}/{key}"


def _filesystem(options: dict[str, Any]) -> Any:
    s3fs = _s3fs()
    return s3fs.S3FileSystem(**options)


def _suffix(url: str, path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix not in EXPORTERS:
        known = ", ".join(sorted(EXPORTERS))
        raise ConfigurationError(f"{url}: the object's extension picks the format: {known}", key="output")
    return suffix


def _staging(url: str, path: str) -> Path:
    """Where the items wait before the upload: the same place for the same URL, run after run."""
    root = Path(os.environ.get("WINTERGRAB_UPLOADS") or Path(".wintergrab") / "uploads")
    return root / hashlib.sha1(url.encode()).hexdigest()[:16] / Path(path).name


class ObjectExporter(Exporter):
    """Items in an object of an S3 bucket, in the format its extension names (see the module docs)."""

    supports_unique_key = True

    def __init__(self, url: str, *, append: bool, unique_key: str | None = None) -> None:
        options, self.object = parse_target(url)
        _suffix(url, self.object)
        super().__init__(Path(url.split("?")[0]), append=append)
        self.url = url
        self._fs = _filesystem(options)
        _check_bucket(self._fs, self.object.split("/")[0], url, options.get("profile"))
        self.staging = _staging(url, self.object)
        self.staging.parent.mkdir(parents=True, exist_ok=True)
        if append and not self.staging.exists() and not _spooled(self.staging):
            try:
                if self._fs.exists(self.object):  # a resumed crawl whose local file is gone: continue the object
                    self._fs.get_file(self.object, str(self.staging))
            except Exception as exc:
                raise ExportError(f"could not read {self.url} to continue it: {exc}") from None
        self._inner = open_exporter(self.staging, append=append, unique_key=unique_key)
        self._start = self._size() if append else 0
        self._measure()

    def _size(self) -> int:
        """The staged file's bytes, its write-ahead log included (SQLite)."""
        return sum(p.stat().st_size for p in (self.staging, self.staging.with_name(self.staging.name + "-wal"))
                   if p.exists())  # fmt: skip

    def _measure(self) -> None:
        """``bytes_written`` as the format counts it, or, for one that does not (SQLite), as its file grows:
        ``max_output_bytes`` holds for an object as for a file."""
        written = self._inner.bytes_written
        self.bytes_written = written if written is not None else max(0, self._size() - self._start)

    def write(self, item: Any) -> None:
        self._inner.write(item)
        self.count = self._inner.count
        self._measure()

    def flush(self) -> None:
        self._inner.flush()
        self._measure()

    def close(self) -> None:
        self._inner.close()
        try:
            self._fs.put_file(str(self.staging), self.object)
        except Exception as exc:
            raise ExportError(f"could not upload to {self.url} (the items are kept in {self.staging}): {exc}") from None
        shutil.rmtree(self.staging.parent, ignore_errors=True)


def _check_bucket(fs: Any, bucket: str, url: str, profile: str | None = None) -> None:
    """Fail now, not when the crawl ends, when the bucket is missing or no credentials are found. A key that may
    only write (it cannot look at the bucket) is let through: the upload will tell."""
    try:
        found = fs.exists(bucket)
    except PermissionError:
        return
    except Exception as exc:
        if type(exc).__name__ in ("NoCredentialsError", "PartialCredentialsError"):
            raise ConfigurationError(
                f"{url}: no AWS credentials found (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, AWS_PROFILE...)",
                key="output",
            ) from None
        raise ConfigurationError(f"{url}: {exc}", key="output") from None
    if not found:
        if _no_credentials(profile):  # (s3fs says "no such bucket" for that too)
            raise ConfigurationError(
                f"{url}: no AWS credentials found (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, AWS_PROFILE...)",
                key="output",
            )
        raise ConfigurationError(f"{url}: there is no bucket {bucket!r} (or these credentials cannot see it)",
                                 key="output")  # fmt: skip


def _no_credentials(profile: str | None) -> bool:
    """Whether the AWS credential chain (the environment, the shared files, a machine's role) finds nothing."""
    try:
        import botocore.session

        return botocore.session.Session(profile=profile).get_credentials() is None
    except Exception:
        return False


def _spooled(path: Path) -> bool:
    """Whether a format written at the end (Parquet, Excel) has items waiting for it beside ``path``."""
    return path.with_name(f".{path.name}.spool.jsonl").exists()


def read_object(url: str) -> Iterator[dict[str, Any]]:
    """The records in an object of an S3 bucket, read as a file of its extension is."""
    from ..data.io import read_records

    options, path = parse_target(url)
    suffix = Path(path).suffix.lower()
    fs = _filesystem(options)
    with tempfile.TemporaryDirectory(prefix="wintergrab-") as directory:
        local = Path(directory) / f"object{suffix}"
        try:
            fs.get_file(path, str(local))
        except FileNotFoundError:
            raise ConfigurationError(f"cannot read {url}: no such object") from None
        except Exception as exc:
            raise ConfigurationError(f"cannot read {url}: {exc}") from None
        yield from read_records(local)
