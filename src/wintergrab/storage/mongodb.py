"""MongoDB: items as the documents of a collection (``output = "mongodb://crawler@db.example/shop?collection=products"``).

Needs pymongo (``pip install "wintergrab[mongodb]"``). The database is the URL's path; the collection is
``items`` unless the URL says ``?collection=NAME``. The URL's other parameters (``authSource``, ``tls``,
``replicaSet``...) are the connection's, checked by pymongo: an option it does not know is an error, not
ignored. ``mongodb+srv://`` URLs work too.

Each item is a document, its values as JSON has them: nested objects and lists stay nested, and integers
beyond 64 bits (which MongoDB cannot hold) are text. A record's own ``_id`` field is kept as ``_id_`` (the
document's ``_id`` is MongoDB's), and a name beginning with ``$`` (an operator or a reference to MongoDB) with a
full-width dollar sign (U+FF04); both are read back as they were.

With ``unique_key`` (``--unique-key url``), documents are upserted on that field: crawling again replaces
them. A unique index on it keeps it so (documents without a value for it are left out of the index).
Without it, a fresh crawl empties its collection first, as a file output is replaced; a resumed crawl adds
to it.

The exporter only writes to collections it created: it names them in a ``_wintergrab_collections``
collection beside them, and refuses a collection of the same name that it did not create. Keep the password
out of the URL (``WINTERGRAB_MONGODB_PASSWORD`` is read when the URL names a user without one); where the URL
is shown or kept (logs, run records), its user and password are left out.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from ..errors import ConfigurationError, ExportError
from ..redact import redact_url
from ..spider.exporters import Exporter, _size, dumps, to_dict
from .common import require

__all__ = ["MongoExporter", "read_mongodb"]

#: The environment variable holding the password of a URL that names a user without one.
PASSWORD_VARIABLE = "WINTERGRAB_MONGODB_PASSWORD"
_META = "_wintergrab_collections"
_OPTIONS = ("collection",)  # the URL's own query parameters (the others are the connection's)
_DATABASE = re.compile(r"^[A-Za-z0-9_-]{1,63}$")
_COLLECTION = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,119}$")
_ID = re.compile(r"^_id_*$")  # a record's _id is kept as _id_ (and its _id_ as _id__...)
_WIDE = "\uff04"  # a name's leading $ is kept as a full-width one, as MongoDB's own guidance had it
_INT64 = (-(2**63), 2**63 - 1)
#: The values a unique key has in the index: documents whose key is null (or missing) stay out of it.
_INDEXED = {"$type": ["string", "number", "bool", "object", "array"]}
_TIMEOUT_MS = 10_000  # to find a server, unless the URL says (serverSelectionTimeoutMS)


def _pymongo() -> Any:
    return require("pymongo", "mongodb", "MongoDB output")


def parse_target(url: str) -> tuple[str, str, str]:
    """The connection string (without wintergrab's options), the database and the collection."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    options = {k: v for k, v in query if k in _OPTIONS}
    dsn = urlunsplit(parts._replace(query=urlencode([(k, v) for k, v in query if k not in _OPTIONS])))
    database = unquote(parts.path.lstrip("/"))
    if not database:
        raise ConfigurationError(
            f"{redact_url(url)}: name the database, as in mongodb://host/DATABASE?collection=NAME", key="output"
        )
    if not _DATABASE.match(database):
        raise ConfigurationError(f"database {database!r}: use letters, digits, _ and -", key="output")
    collection = options.get("collection") or "items"
    if not _COLLECTION.match(collection) or collection.startswith("system.") or collection == _META:
        raise ConfigurationError(
            f"collection {collection!r}: use letters, digits, _, - and . (not system.*)", key="output"
        )
    return dsn, database, collection


def _connect(url: str) -> tuple[Any, str, str]:
    """A client for ``url`` that has reached its server, the database and the collection."""
    pymongo = _pymongo()
    from pymongo import uri_parser

    dsn, database, collection = parse_target(url)
    options: dict[str, Any] = {}
    try:
        parsed = uri_parser.parse_uri(dsn, warn=False)  # an unknown option is an error, not a warning
    except (pymongo.errors.ConfigurationError, pymongo.errors.InvalidURI, ValueError) as exc:
        raise ConfigurationError(f"{redact_url(url)}: {exc}", key="output") from None
    if parsed.get("username") and not parsed.get("password") and os.environ.get(PASSWORD_VARIABLE):  # (none is "")
        options["password"] = os.environ[PASSWORD_VARIABLE]
    if "serverselectiontimeoutms" not in {k.lower() for k in parsed.get("options", {})}:
        options["serverSelectionTimeoutMS"] = _TIMEOUT_MS
    client = None
    try:
        client = pymongo.MongoClient(dsn, **options)
        client[database].command("ping")
    except pymongo.errors.PyMongoError as exc:
        if client is not None:
            client.close()
        raise ConfigurationError(f"cannot connect to {redact_url(url)}: {_message(exc)}", key="output") from None
    return client, database, collection


def _message(exc: BaseException) -> str:
    """An error's first line (pymongo's go on with the whole topology)."""
    return str(exc).split(", Timeout:")[0].split(", full error:")[0].strip()


def _index_name(key: str) -> str:
    return "wg_unique_" + hashlib.sha1(key.encode()).hexdigest()[:12]


def _stored_key(key: str, *, top: bool = True) -> str:
    """A field's name in a document: a record's ``_id`` is ``_id_``; a leading ``$`` (an operator, or a
    reference, to MongoDB) is a full-width dollar sign (U+FF04)."""
    if top and _ID.match(key):
        return key + "_"
    return _WIDE + key[1:] if key.startswith("$") else key


def _field_name(key: str, *, top: bool = True) -> str:
    """The record's own name for a document's field (``_stored_key`` undone)."""
    if top and _ID.match(key) and key != "_id":
        return key[:-1]
    return "$" + key[1:] if key.startswith(_WIDE) else key


def _storable(value: Any) -> Any:
    """A JSON value as MongoDB can hold it: integers beyond 64 bits as text, names beginning with ``$``
    with a full-width dollar sign (U+FF04)."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value if _INT64[0] <= value <= _INT64[1] else str(value)
    if isinstance(value, dict):
        return {_stored_key(k, top=False): _storable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_storable(v) for v in value]
    return value


class MongoExporter(Exporter):
    """Items as the documents of a MongoDB collection (see the module docs)."""

    supports_unique_key = True

    def __init__(self, url: str, *, append: bool, unique_key: str | None = None) -> None:
        super().__init__(Path(redact_url(url)), append=append)
        self.url = url
        if unique_key is not None and (unique_key.startswith("$") or "." in unique_key or not unique_key):
            raise ConfigurationError(
                f"unique key {unique_key!r}: a top-level field's name (MongoDB reads $ as an operator, a dot as a path)",
                key="unique_key",
            )
        self.unique_key = unique_key
        self.bytes_written = 0  # the items as JSON
        self._pymongo = _pymongo()
        self._client, database, name = _connect(url)
        self.collection_name = f"{database}.{name}"
        self._db = self._client[database]
        self._name = name
        self._collection = self._db[name]
        self._docs: list[dict[str, Any]] = []
        try:
            self._open(append)
        except self._pymongo.errors.PyMongoError as exc:
            self._client.close()
            raise ExportError(f"{self.collection_name}: {_message(exc)}") from None
        except Exception:
            self._client.close()
            raise

    def _open(self, append: bool) -> None:
        meta = self._db[_META]
        exists = bool(self._db.list_collection_names(filter={"name": self._name}))
        ours = meta.find_one({"_id": self._name}) is not None
        if exists and not ours:
            raise ConfigurationError(
                f"{self.collection_name} already exists and wintergrab did not create it; name another "
                "(?collection=NAME)",
                key="output",
            )
        if not exists:
            self._db.create_collection(self._name)
            created = datetime.now(timezone.utc).isoformat(timespec="seconds")
            meta.replace_one({"_id": self._name}, {"_id": self._name, "created": created}, upsert=True)
        elif not append and not self.unique_key:
            self._collection.delete_many({})  # a fresh crawl replaces its documents
        self._unique_index()

    def _unique_index(self) -> None:
        """The unique index on ``unique_key`` (others it made for another key go)."""
        wanted = _index_name(self.unique_key) if self.unique_key else None
        for index in self._collection.list_indexes():
            if index["name"].startswith("wg_unique_") and index["name"] != wanted:
                self._collection.drop_index(index["name"])  # unique_key changed since the last run
        if self.unique_key is None:
            return
        field = _stored_key(self.unique_key)
        try:
            self._collection.create_index(
                [(field, 1)], name=wanted, unique=True, partialFilterExpression={field: _INDEXED}
            )
        except self._pymongo.errors.OperationFailure as exc:
            if exc.code in (11000, 11001):
                raise ConfigurationError(
                    f"{self.collection_name} already holds duplicate values of {self.unique_key!r}; it cannot be "
                    "the unique key",
                    key="unique_key",
                ) from None
            raise

    # -- writing ------------------------------------------------------------------------------ #
    def write(self, item: Any) -> None:
        row = to_dict(item)
        if not isinstance(row, dict):
            row = {"value": row}
        text = dumps(row)
        self.bytes_written += _size(text) + 1  # type: ignore[operator]
        record = json.loads(text)  # JSON values, as every output gets them
        document = {_stored_key(str(k)): _storable(v) for k, v in record.items()}
        self._docs.append(document)
        self.count += 1
        self._maybe_flush()

    def flush(self) -> None:
        if not self._docs:
            return
        from bson import ObjectId
        from pymongo import InsertOne, ReplaceOne, UpdateOne

        field = _stored_key(self.unique_key) if self.unique_key else None
        operations: list[Any] = []
        owners: list[int] = []  # the document each operation writes
        for number, document in enumerate(self._docs):
            key = document.get(field) if field is not None else None
            if field is not None and key is not None:
                # created with an _id made here, as inserts' are, so that _id order is the order of first writes
                # (the server's own would sort at random among them); then replaced whole, its _id kept
                operations.append(UpdateOne({field: key}, {"$setOnInsert": {"_id": ObjectId()}}, upsert=True))
                operations.append(ReplaceOne({field: key}, document))
                owners += [number, number]
            else:
                operations.append(InsertOne(document))  # its _id made here, in this order
                owners.append(number)
        try:
            self._collection.bulk_write(operations, ordered=True)  # in order: of two with one key, the last stays
        except self._pymongo.errors.BulkWriteError as exc:
            errors = exc.details.get("writeErrors") or [{}]
            written = owners[errors[0].get("index", 0)]
            raise ExportError(
                f"{self.collection_name}: {len(self._docs) - written} item(s) not written: "
                f"{errors[0].get('errmsg', _message(exc))}",
                items=len(self._docs) - written,
            ) from None
        except self._pymongo.errors.PyMongoError as exc:
            raise ExportError(
                f"{self.collection_name}: {len(self._docs)} item(s) not written: {_message(exc)}",
                items=len(self._docs),
            ) from None
        finally:
            self._docs.clear()

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self._client.close()


def _plain(value: Any) -> Any:
    """A document's value as JSON holds it (for collections wintergrab did not write: ids, dates...)."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        return {_field_name(k, top=False): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return str(value)  # ObjectId, Decimal128 (exact), Timestamp...


def read_mongodb(url: str) -> Iterator[dict[str, Any]]:
    """The documents of a collection as records, in the order they were first written (without MongoDB's
    ``_id``)."""
    pymongo = _pymongo()
    client, database, name = _connect(url)
    try:
        for document in client[database][name].find({}, sort=[("_id", 1)], batch_size=2_000):
            document.pop("_id", None)
            yield {_field_name(k): _plain(v) for k, v in document.items() if v is not None}
    except pymongo.errors.PyMongoError as exc:
        raise ConfigurationError(f"cannot read {redact_url(url)}: {_message(exc)}") from None
    finally:
        client.close()
