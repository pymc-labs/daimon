"""Outbound connections to admin-entered hosts stay off loopback, private and metadata
addresses, unless the deployment allows them in `DAIMON_DATA__PRIVATE_HOSTS`."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Sequence
from typing import Final

PRIVATE_NAMES: Final = ("localhost", ".localhost", ".internal", ".flycast", ".local")
type Net = ipaddress.IPv4Network | ipaddress.IPv6Network


class NotPublic(ValueError):
    """The host is missing, unknown, or reaches a private address no one allowed."""


def _allowed(entries: Sequence[str]) -> tuple[set[str], list[Net]]:
    """Split the allow-list into host names and address ranges
    (a bare address is a range of one)."""
    names: set[str] = set()
    nets: list[Net] = []
    for entry in entries:
        entry = entry.strip().strip("[]").lower().rstrip(".")
        if not entry:
            continue
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            names.add(entry)
    return names, nets


async def resolve(host: str, port: int, private_hosts: Sequence[str] = ()) -> str:
    """The address `host` resolves to now, to connect to, so a name that later points inward
    is never looked up a second time. Raises `NotPublic` for a missing, private or unknown
    host; a name or address listed in `private_hosts` may be private."""
    host = (host or "").strip("[]").lower().rstrip(".")
    if not host:
        raise NotPublic("Use a public host")
    names, nets = _allowed(private_hosts)
    named = host in names
    if not named and (
        host.endswith(PRIVATE_NAMES) or host in {n.lstrip(".") for n in PRIVATE_NAMES}
    ):
        raise NotPublic("Use a public host")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise NotPublic("Host not found") from e
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not (ip.is_global or named or _in(ip, nets)):
            raise NotPublic("Use a public host")
    return str(infos[0][4][0]).split("%")[0]


def _in(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, nets: list[Net]) -> bool:
    return any(ip in net for net in nets if net.version == ip.version)
