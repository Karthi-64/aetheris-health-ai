"""Who is on the other end of a request — the one place that decides.

Three things depend on the answer: the anonymous rate limit, the
authentication throttle (``app/services/auth_throttle.py``) and the address
recorded against a session. They must agree, and none of them may take the
caller's word for it.

``X-Forwarded-For`` is written by whoever sends the request. It means
something only where a proxy **we operate** appended to it, and only the part
that proxy appended. So:

* it is ignored unless ``RATE_LIMIT_TRUST_PROXY_HEADER`` is on;
* it is ignored unless the connection itself comes from one of our proxies
  (``RATE_LIMIT_TRUSTED_PROXY_CIDRS``) — a client that reaches the application
  directly cannot name its own address;
* when used, the address is read from the **right-hand end** — the entries our
  own proxies added — counting ``RATE_LIMIT_TRUSTED_PROXY_HOPS`` back. Anything
  further left was supplied by the client and is never used;
* whatever is found must parse as an IP address. If it does not, or the header
  is shorter than the configured chain, the socket peer is used instead: that
  one cannot be forged.

Whether there is a proxy at all is not guessed. Outside development the
application refuses to start until ``RATE_LIMIT_TRUST_PROXY_HEADER`` has been
set, to true or to false (``app/core/config.py``): behind an undeclared proxy
every caller would share the proxy's address, and no heuristic can tell that
apart from a client that merely sends the header.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

from app.core.config import settings

if TYPE_CHECKING:
    from starlette.requests import Request

__all__ = ["UNKNOWN_SOURCE", "client_ip", "parse_ip", "source_of"]

#: The source every request with no usable address shares. Sharing is the
#: point: an attempt that hides where it came from gets no budget of its own.
UNKNOWN_SOURCE = "unknown"

#: IPv6 is handed out to a site or a device by the /64. Counting single
#: addresses would give one machine eighteen quintillion separate sources.
_IPV6_SOURCE_PREFIX = 64

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def parse_ip(value: str | None) -> _IPAddress | None:
    """Parse one address as a proxy or socket would write it.

    Accepts a bare address, ``addr:port`` (IPv4), ``[addr]:port`` (IPv6) and a
    zone suffix (``%eth0``). An IPv4 address carried inside IPv6
    (``::ffff:203.0.113.7``) is returned as the IPv4 address, so one machine
    is not two sources depending on the socket family.

    :param value: The text to parse.
    :returns: The address, or ``None`` if it is not one.
    """
    if not value:
        return None
    text = value.strip()
    if text.startswith("["):
        text = text[1:].partition("]")[0]
    elif text.count(":") == 1:
        text = text.partition(":")[0]
    text = text.partition("%")[0]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _is_trusted_proxy(peer: _IPAddress | None) -> bool:
    """Whether a socket peer is one of the proxies we operate."""
    if peer is None:
        return False
    return any(
        peer in ipaddress.ip_network(network, strict=False)
        for network in settings.RATE_LIMIT_TRUSTED_PROXY_CIDRS
        if ipaddress.ip_network(network, strict=False).version == peer.version
    )


def _forwarded_entries(request: Request) -> list[str]:
    """Every ``X-Forwarded-For`` entry, in order.

    A request can carry the header more than once; the proxy's own entry is at
    the end of the last one, so they are read as one list.
    """
    return [
        part.strip()
        for header in request.headers.getlist("x-forwarded-for")
        for part in header.split(",")
        if part.strip()
    ]


def client_ip(request: Request) -> str | None:
    """Resolve the caller's IP address.

    :param request: The incoming request.
    :returns: A normalised IP address, or ``None`` when there is none to be
        had. Never text that is not an address, so it is safe to store in an
        ``INET`` column.
    """
    peer = parse_ip(request.client.host if request.client else None)
    entries = _forwarded_entries(request)

    if not settings.RATE_LIMIT_TRUST_PROXY_HEADER:
        return str(peer) if peer is not None else None

    address: _IPAddress | None = None
    hops = settings.RATE_LIMIT_TRUSTED_PROXY_HOPS
    if _is_trusted_proxy(peer) and len(entries) >= hops:
        address = parse_ip(entries[-hops])
    if address is None:
        address = peer
    return str(address) if address is not None else None


def source_of(ip: str | None) -> str:
    """Reduce an address to the *source* it is counted under.

    IPv4 addresses stand for themselves. An IPv6 address is counted as its
    /64. No address at all is :data:`UNKNOWN_SOURCE`.

    :param ip: An address from :func:`client_ip`, or ``None``.
    :returns: A short string naming the source.
    """
    address = parse_ip(ip)
    if address is None:
        return UNKNOWN_SOURCE
    if isinstance(address, ipaddress.IPv6Address):
        network = ipaddress.ip_network((address, _IPV6_SOURCE_PREFIX), strict=False)
        return str(network)
    return str(address)
