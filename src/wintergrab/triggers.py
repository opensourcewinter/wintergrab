"""Jobs that run when asked over HTTP: by another program, a service's webhook, another project's.

::

    export WINTERGRAB_TRIGGER_TOKEN=...          # a long random secret
    wintergrab schedule --listen 127.0.0.1:8765

    curl -X POST -H "Authorization: Bearer $WINTERGRAB_TRIGGER_TOKEN" http://127.0.0.1:8765/jobs/listing/run

``POST /jobs/NAME/run`` asks for a job of the project to run: it runs in turn with the scheduler's others
(one job at a time), and the request is answered at once, ``202`` with ``{"job": ..., "queued": true}``
(``false`` when it was already waiting to run). ``GET /jobs`` lists the jobs, and when each runs next.

Every request must carry the token: as a bearer token (``Authorization: Bearer ...``), or as the HMAC-SHA256
signature of its body, the way another project's webhooks sign their deliveries when the token is their
``secret`` (``X-Wintergrab-Signature: sha256=...``) and GitHub's do (``X-Hub-Signature-256``). Anything
else is answered ``401``, before the job is even looked up. The server listens on the loopback address unless
told otherwise, and speaks plain HTTP: put a TLS proxy in front of it on any other network.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from .errors import ConfigurationError
from .webhooks import SIGNATURE_HEADER, verify

if TYPE_CHECKING:
    from .project import Scheduler

__all__ = ["TOKEN_VARIABLE", "TriggerServer"]

log = logging.getLogger(__name__)

#: The environment variable holding the token requests must carry.
TOKEN_VARIABLE = "WINTERGRAB_TRIGGER_TOKEN"
#: The largest request body read (a webhook's delivery), in bytes.
MAX_BODY = 256 * 1024
_SIGNATURE_HEADERS = (SIGNATURE_HEADER, "X-Hub-Signature-256")
_RUN = re.compile(r"/jobs/([^/]+)/run/?")


class TriggerServer(ThreadingHTTPServer):
    """Runs a :class:`~wintergrab.project.Scheduler`'s jobs when asked over HTTP (see the module docs).

    Args:
        scheduler: Whose jobs run (:meth:`~wintergrab.project.Scheduler.request`).
        host, port: Where to listen (the loopback address by default; port 0: any free one).
        token: What requests must carry (by default ``WINTERGRAB_TRIGGER_TOKEN``): 16 characters at least.

    Serve with ``serve_forever()`` (in a thread of its own: the scheduler's loop runs the jobs).
    """

    daemon_threads = True

    def __init__(self, scheduler: Scheduler, host: str = "127.0.0.1", port: int = 8765, *, token: str | None = None):
        token = token or os.environ.get(TOKEN_VARIABLE)
        if not token:
            raise ConfigurationError(f"set {TOKEN_VARIABLE} to a secret: requests to run jobs must carry it")
        if len(token) < 16:
            raise ConfigurationError(f"{TOKEN_VARIABLE} is too short to guard anything: 16 characters at least")
        self.scheduler = scheduler
        self.token = token
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__((host, port), _Handler)

    @property
    def local(self) -> bool:
        """Whether only this machine can reach it (it listens on a loopback address)."""
        host = self.server_address[0]
        name = host.decode() if isinstance(host, (bytes, bytearray)) else str(host)
        try:
            return ipaddress.ip_address(name).is_loopback
        except ValueError:
            return False

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        name = host.decode() if isinstance(host, bytes) else str(host)
        return f"http://{f'[{name}]' if ':' in name else name}:{port}"


class _Handler(BaseHTTPRequestHandler):
    server: TriggerServer
    server_version = "wintergrab"
    timeout = 30  # (seconds: a client that sends nothing does not hold a thread for ever)

    def version_string(self) -> str:
        return self.server_version  # (the Server header names no Python version)

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s: %s", self.client_address[0], format % args)

    def _answer(self, status: int, data: Any) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes | None:
        """The request's body (``None``: too large to read)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            return None
        return self.rfile.read(length) if length else b""

    def _allowed(self, body: bytes) -> bool:
        token = self.server.token
        scheme, _, given = (self.headers.get("Authorization") or "").partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(given.strip().encode("utf-8"), token.encode("utf-8")):
            return True
        return any(verify(body, self.headers.get(header), token) for header in _SIGNATURE_HEADERS)

    def do_POST(self) -> None:
        body = self._body()
        if body is None:
            self.close_connection = True  # (its body was not read)
            return self._answer(413, {"error": f"a body of {MAX_BODY:,} bytes at most"})
        if not self._allowed(body):
            log.warning("%s: a request without the token (%s %s)", self.client_address[0], self.command, self.path)
            return self._answer(401, {"error": "the token, or a signature made with it, is needed"})
        match = _RUN.fullmatch(urlsplit(self.path).path)
        if match is None:
            return self._answer(404, {"error": "POST /jobs/NAME/run runs a job"})
        name = unquote(match.group(1))
        try:
            queued = self.server.scheduler.request(name, reason=_reason(body, self.client_address[0]))
        except KeyError:
            return self._answer(404, {"error": f"no job {name!r} in the project"})
        except ConfigurationError as exc:
            return self._answer(409, {"error": str(exc)})
        return self._answer(202, {"job": name, "queued": queued})

    def do_GET(self) -> None:
        if not self._allowed(b""):
            return self._answer(401, {"error": "the token is needed"})
        if urlsplit(self.path).path.rstrip("/") != "/jobs":
            return self._answer(404, {"error": "GET /jobs lists the jobs"})
        return self._answer(200, {"jobs": self.server.scheduler.listing()})

    def _refuse(self) -> None:
        self._answer(405, {"error": "POST /jobs/NAME/run, or GET /jobs"})

    do_PUT = do_DELETE = do_PATCH = _refuse


def _reason(body: bytes, client: str) -> str:
    """Why a job was asked for: the body's ``reason``, what a webhook's delivery tells, or where from."""
    try:
        data = json.loads(body) if body.strip() else None
    except ValueError:
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("reason"), str) and data["reason"].strip():
            return data["reason"].strip()[:200]
        events = data.get("events")
        if isinstance(events, list) and events and isinstance(events[-1], dict):  # a project's webhook
            last = events[-1]
            origin = f" of {last['origin']}" if isinstance(last.get("origin"), str) else ""
            return f"{last.get('event')}{origin} ({len(events)} event{'s' if len(events) > 1 else ''})"[:200]
    return f"asked by {client}"
