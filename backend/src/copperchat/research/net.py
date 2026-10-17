"""Outbound URL checks for the part-researcher (no requests into the local network)."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

Resolver = Callable[[str, int], Awaitable[list[str]]]


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def url_refusal(url: str, *, resolver: Resolver | None = None) -> str | None:
    """None if ``url`` is an http(s) URL whose host resolves only to public addresses,
    else the reason it is refused. Loopback, private, link-local, multicast, reserved and
    unspecified addresses (incl. IPv4-mapped IPv6) are refused, so a page or PDF cannot
    steer the researcher at CopperChat itself or other hosts on the user's network."""
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return "malformed URL"
    if parts.scheme not in ("http", "https"):
        return "only http(s) URLs are allowed"
    host = parts.hostname
    if not host:
        return "URL has no host"
    if parts.username or parts.password:
        return "URLs with credentials are not allowed"
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    try:
        addresses = [host] if literal else await (resolver or _resolve)(host, port)
    except (OSError, UnicodeError) as exc:
        return f"host {host!r} does not resolve: {exc}"
    if not addresses:
        return f"host {host!r} does not resolve"
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            return f"host {host!r} resolves to an invalid address"
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            return f"host {host!r} resolves to a non-public address ({ip})"
    return None
