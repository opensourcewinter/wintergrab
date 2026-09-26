"""Network policy: which destinations a crawl may reach (SSRF / internal-network protection).

A crawler follows links that strangers wrote. A page can link to - or redirect
to - ``http://169.254.169.254/latest/meta-data/`` (a cloud instance's
credentials), ``http://localhost:6379`` or ``http://10.0.0.5/admin``. Run from
a machine inside a private network, an unrestricted crawler becomes a proxy
into that network.

:class:`NetworkPolicy` stops that. Before each request (and each redirect hop)
it resolves the host name and refuses the request if *any* of its addresses is
not allowed. After the connection it checks the address curl actually
connected to, which defeats DNS rebinding (a host that resolves to a public
address for the check and to an internal one a moment later): the response is
dropped and :class:`~wintergrab.errors.NetworkPolicyError` raised.

What the checks cannot see: when the request goes through a proxy, the proxy
resolves the name and connects, so the policy only checks what it can resolve
locally. A browser resolves and connects by itself too: the policy checks every
URL the browser requests (pages, frames, scripts, API calls), but cannot pin
the address Chromium connects to. Run crawls of untrusted sites from a network
that cannot reach sensitive services when that matters.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Collection, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from .errors import ConfigurationError, NetworkError, NetworkPolicyError

__all__ = ["NetworkPolicy", "address_category"]

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Special-purpose ranges (IANA IPv4/IPv6 special-purpose address registries), by category.
# An explicit table keeps the answer the same on every Python version (``is_private`` and
# ``is_global`` changed meaning across 3.10-3.13).
_V4_RANGES: tuple[tuple[str, str], ...] = (
    ("0.0.0.0/8", "unspecified"),  # "this network"; 0.0.0.0 reaches the local host on Linux
    ("10.0.0.0/8", "private"),
    ("100.64.0.0/10", "private"),  # carrier-grade NAT (shared address space; Alibaba Cloud metadata lives here)
    ("127.0.0.0/8", "loopback"),
    ("169.254.0.0/16", "link_local"),  # includes 169.254.169.254, the AWS/GCP/Azure metadata service
    ("172.16.0.0/12", "private"),
    ("192.0.0.0/24", "reserved"),  # IETF protocol assignments (Oracle Cloud metadata: 192.0.0.192)
    ("192.0.2.0/24", "reserved"),  # TEST-NET-1
    ("192.88.99.0/24", "reserved"),  # 6to4 relay anycast (deprecated)
    ("192.168.0.0/16", "private"),
    ("198.18.0.0/15", "reserved"),  # benchmarking
    ("198.51.100.0/24", "reserved"),  # TEST-NET-2
    ("203.0.113.0/24", "reserved"),  # TEST-NET-3
    ("224.0.0.0/4", "multicast"),
    ("240.0.0.0/4", "reserved"),  # future use; includes 255.255.255.255
)
_V6_RANGES: tuple[tuple[str, str], ...] = (
    ("::/128", "unspecified"),
    ("::1/128", "loopback"),
    ("64:ff9b:1::/48", "private"),  # local-use NAT64
    ("100::/64", "reserved"),  # discard-only
    ("2001:db8::/32", "reserved"),  # documentation
    ("2001::/23", "reserved"),  # IETF protocol assignments (Teredo is handled separately)
    ("fc00::/7", "private"),  # unique local (AWS IPv6 metadata: fd00:ec2::254)
    ("fe80::/10", "link_local"),
    ("fec0::/10", "private"),  # site-local (deprecated)
    ("ff00::/8", "multicast"),
)
_TABLE: list[tuple[IPNetwork, str]] = [(ipaddress.ip_network(net), cat) for net, cat in _V4_RANGES + _V6_RANGES]
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_TEREDO = ipaddress.ip_network("2001::/32")
_6TO4 = ipaddress.ip_network("2002::/16")

#: Categories and the ``NetworkPolicy`` switch that allows them.
CATEGORIES = ("public", "private", "loopback", "link_local", "reserved", "multicast", "unspecified")


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address carries (mapped, NAT64, 6to4, Teredo), if any."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _6TO4:
        return ip.sixtofour
    if ip in _TEREDO and ip.teredo is not None:
        return ip.teredo[1]  # the client address, obfuscated in the address and restored by the stdlib
    return None


def address_category(address: str | IPAddress) -> str:
    """``"public"`` or the special-purpose category of an IP address.

    Categories: ``"private"``, ``"loopback"``, ``"link_local"``, ``"reserved"``,
    ``"multicast"`` and ``"unspecified"``. IPv6 addresses that embed an IPv4
    address (``::ffff:127.0.0.1``, NAT64, 6to4, Teredo) are judged by the IPv4 one.
    """
    ip = ipaddress.ip_address(address) if isinstance(address, str) else address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id:  # "fe80::1%eth0"
            ip = ipaddress.IPv6Address(str(ip).split("%", 1)[0])
        inner = _embedded_v4(ip)
        if inner is not None:
            category = address_category(inner)
            if category != "public":
                return category
    for network, category in _TABLE:
        if ip.version == network.version and ip in network:
            return category
    return "public"


def _parse_ip(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return None


def _networks(values: Iterable[str | IPNetwork]) -> list[IPNetwork]:
    out: list[IPNetwork] = []
    for value in values:
        try:
            out.append(ipaddress.ip_network(value, strict=False) if isinstance(value, str) else value)
        except ValueError as exc:
            raise ConfigurationError(f"not an IP network: {value!r}", key="network_policy") from exc
    return out


def _host_matches(host: str, patterns: Collection[str]) -> bool:
    """``host`` equals a pattern or is a subdomain of it (``".example.com"`` and ``"example.com"`` alike)."""
    for pattern in patterns:
        pattern = pattern.lower().lstrip(".")
        if host == pattern or host.endswith("." + pattern):
            return True
    return False


class NetworkPolicy:
    """Rules for which hosts and addresses requests may reach.

    The default instance (also :meth:`public`) allows public internet
    addresses only: private (RFC 1918, carrier-grade NAT, unique-local IPv6),
    loopback, link-local (cloud metadata services), multicast, unspecified and
    reserved/documentation addresses are refused.

    Args:
        allow_private: Allow private network addresses (10/8, 172.16/12, 192.168/16, 100.64/10, fc00::/7).
        allow_loopback: Allow 127.0.0.0/8, ``::1`` and ``localhost`` names.
        allow_link_local: Allow 169.254.0.0/16 and fe80::/10 (this includes cloud metadata services!).
        allow_reserved: Allow documentation, benchmarking and other reserved ranges.
        allowed_networks: Networks always allowed (e.g. ``["10.1.2.0/24"]`` for one internal site).
        denied_networks: Networks always refused, even if public.
        allowed_hosts: Host names (and their subdomains) allowed whatever they resolve to.
        denied_hosts: Host names (and their subdomains) always refused.
        allowed_ports: If given, only these ports may be used.
        schemes: URL schemes that may be fetched.
        resolve: Resolve host names and check every address (``False`` checks IP literals only).
        dns_cache_ttl: Seconds to reuse a resolution.
    """

    def __init__(
        self,
        *,
        allow_private: bool = False,
        allow_loopback: bool = False,
        allow_link_local: bool = False,
        allow_reserved: bool = False,
        allowed_networks: Iterable[str | IPNetwork] = (),
        denied_networks: Iterable[str | IPNetwork] = (),
        allowed_hosts: Iterable[str] = (),
        denied_hosts: Iterable[str] = (),
        allowed_ports: Iterable[int] | None = None,
        schemes: Iterable[str] = ("http", "https"),
        resolve: bool = True,
        dns_cache_ttl: float = 60.0,
    ) -> None:
        self.allowed_categories = {"public"}
        if allow_private:
            self.allowed_categories.add("private")
        if allow_loopback:
            self.allowed_categories.update(("loopback", "unspecified"))
        if allow_link_local:
            self.allowed_categories.add("link_local")
        if allow_reserved:
            self.allowed_categories.add("reserved")
        self.allowed_networks = _networks(allowed_networks)
        self.denied_networks = _networks(denied_networks)
        self.allowed_hosts = frozenset(h.lower().lstrip(".") for h in allowed_hosts)
        self.denied_hosts = frozenset(h.lower().lstrip(".") for h in denied_hosts)
        self.allowed_ports = frozenset(int(p) for p in allowed_ports) if allowed_ports is not None else None
        self.schemes = frozenset(s.lower() for s in schemes)
        self.resolve = resolve
        self.dns_cache_ttl = dns_cache_ttl
        self._dns: OrderedDict[tuple[str, int], tuple[float, list[str]]] = OrderedDict()
        self._dns_lock = threading.Lock()
        self.stats: dict[str, int] = {"checked": 0, "refused": 0}

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    @classmethod
    def public(cls) -> NetworkPolicy:
        """Public internet addresses only (the recommended setting for untrusted sites)."""
        return cls()

    @classmethod
    def coerce(cls, value: Any) -> NetworkPolicy | None:
        """``None``/``False``/``"any"`` -> no policy; ``True``/``"public"`` -> :meth:`public`;
        ``"private"`` -> also private networks and loopback; a dict -> keyword arguments."""
        if value is None or value is False or value == "any":
            return None
        if isinstance(value, NetworkPolicy):
            return value
        if value is True or value == "public":
            return cls.public()
        if value == "private":
            return cls(allow_private=True, allow_loopback=True)
        if isinstance(value, Mapping):
            try:
                return cls(**value)
            except TypeError as exc:
                raise ConfigurationError(str(exc), key="network_policy") from exc
        raise ConfigurationError(
            f"expected 'public', 'private', 'any', a dict or a NetworkPolicy, got {value!r}", key="network_policy"
        )

    # ------------------------------------------------------------------ #
    # decisions
    # ------------------------------------------------------------------ #
    def address_reason(self, address: str | IPAddress) -> str | None:
        """Why ``address`` is refused, or ``None`` if it is allowed."""
        try:
            ip = ipaddress.ip_address(address) if isinstance(address, str) else address
        except ValueError:
            return f"invalid address {address!r}"
        for network in self.denied_networks:
            if ip.version == network.version and ip in network:
                return f"{ip} is in the denied network {network}"
        for network in self.allowed_networks:
            if ip.version == network.version and ip in network:
                return None
        category = address_category(ip)
        if category in self.allowed_categories:
            return None
        return f"{ip} is a {category.replace('_', '-')} address"

    def _static_reason(self, url: str) -> tuple[str, int, str | None, bool]:
        """``(host, port, reason, decided)``: checks that need no DNS. ``decided`` means no DNS check is needed."""
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in self.schemes:
            return "", 0, f"scheme {scheme or '(none)'!r} is not allowed", True
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            return "", 0, "URL has no host", True
        try:
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError:
            return host, 0, "invalid port", True
        if self.allowed_ports is not None and port not in self.allowed_ports:
            return host, port, f"port {port} is not allowed", True
        if self.denied_hosts and _host_matches(host, self.denied_hosts):
            return host, port, f"host {host} is denied", True
        if self.allowed_hosts and _host_matches(host, self.allowed_hosts):
            return host, port, None, True
        ip = _parse_ip(host)
        if ip is not None:
            return host, port, self.address_reason(ip), True
        if (host == "localhost" or host.endswith(".localhost")) and "loopback" not in self.allowed_categories:
            return host, port, f"{host} is a loopback name", True
        return host, port, None, not self.resolve

    def is_allowed_address(self, address: str) -> bool:
        return self.address_reason(address) is None

    # ------------------------------------------------------------------ #
    # checking URLs
    # ------------------------------------------------------------------ #
    async def check(self, url: str, *, proxied: bool = False) -> None:
        """Raise :class:`NetworkPolicyError` if ``url`` may not be requested.

        Host names are resolved (asynchronously) and every address must be
        allowed. With ``proxied=True`` a name that does not resolve locally is
        allowed (the proxy resolves it); one that does resolve is still checked.
        """
        host, port, reason, decided = self._static_reason(url)
        self.stats["checked"] += 1
        if reason is None and not decided:
            addresses = await self._resolve_async(host, port, url, proxied)
            reason = self._addresses_reason(addresses)
        if reason is not None:
            self._refuse(url, reason)

    def check_sync(self, url: str, *, proxied: bool = False) -> None:
        """Blocking version of :meth:`check` (for the synchronous :class:`~wintergrab.Fetcher`)."""
        host, port, reason, decided = self._static_reason(url)
        self.stats["checked"] += 1
        if reason is None and not decided:
            addresses = self._cached(host, port)
            if addresses is None:
                try:
                    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                except OSError as exc:
                    addresses = self._resolution_failed(host, url, proxied, exc)
                else:
                    addresses = self._remember(host, port, infos)
            reason = self._addresses_reason(addresses)
        if reason is not None:
            self._refuse(url, reason)

    def check_connected(self, url: str, address: str | None, *, proxied: bool = False) -> None:
        """After connecting: refuse if curl ended up at a forbidden address (DNS rebinding)."""
        if proxied or not address:
            return  # the proxy's address says nothing about the target's
        host = (urlsplit(url).hostname or "").lower()
        if self.allowed_hosts and _host_matches(host, self.allowed_hosts):
            return
        reason = self.address_reason(address)
        if reason is not None:
            self._refuse(url, f"connected to {reason}", address)

    def reason(self, url: str) -> str | None:
        """Static verdict for ``url`` (no DNS): the refusal reason, or ``None``."""
        return self._static_reason(url)[2]

    def _refuse(self, url: str, reason: str, address: str | None = None) -> None:
        self.stats["refused"] += 1
        if address is None:
            first = reason.split(" ", 1)[0]
            address = first if _parse_ip(first) is not None else None
        raise NetworkPolicyError(url, f"Blocked by network policy: {reason}", reason=reason, address=address)

    def _addresses_reason(self, addresses: list[str]) -> str | None:
        for address in addresses:
            reason = self.address_reason(address)
            if reason is not None:
                return reason
        return None

    # ------------------------------------------------------------------ #
    # DNS
    # ------------------------------------------------------------------ #
    async def _resolve_async(self, host: str, port: int, url: str, proxied: bool) -> list[str]:
        cached = self._cached(host, port)
        if cached is not None:
            return cached
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            return self._resolution_failed(host, url, proxied, exc)
        return self._remember(host, port, infos)

    def _resolution_failed(self, host: str, url: str, proxied: bool, exc: OSError) -> list[str]:
        if proxied:
            return []  # the proxy resolves it
        raise NetworkError(url, f"Could not resolve host {host}: {exc}", cause=exc, kind="dns") from exc

    def _cached(self, host: str, port: int) -> list[str] | None:
        with self._dns_lock:
            entry = self._dns.get((host, port))
            if entry is None:
                return None
            if time.monotonic() - entry[0] > self.dns_cache_ttl:
                del self._dns[(host, port)]
                return None
            return entry[1]

    def _remember(self, host: str, port: int, infos: list[Any]) -> list[str]:
        addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
        with self._dns_lock:
            self._dns[(host, port)] = (time.monotonic(), addresses)
            self._dns.move_to_end((host, port))
            while len(self._dns) > 4096:
                self._dns.popitem(last=False)
        return addresses

    def __repr__(self) -> str:
        allowed = sorted(self.allowed_categories)
        return f"NetworkPolicy(allowed={allowed}, allowed_hosts={sorted(self.allowed_hosts)})"
