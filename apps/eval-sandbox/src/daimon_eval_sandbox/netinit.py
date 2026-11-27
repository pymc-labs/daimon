"""Packet rules for an eval pod's shared network namespace. Any failure exits non-zero.

Only the gateway uid reaches the network: the world, the resolvers, and web ports on
public addresses. Web ports are redirected to the gateway: the executor (root and uids up
to `EXEC_UID`) to its port, everyone else to the agent port. DNS goes to the gateway's
fake resolver. Inbound, only the harness port accepts new connections. Everything else,
IPv6 included, is rejected. Our chains are inserted first and nothing is flushed, so a
runtime's own rules stay.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from daimon_eval_sandbox.gateway import AGENT_PORT, DNS_PORT, EXEC_PORT, EXEC_UID, GATEWAY_UID

HARNESS_PORT: Final = 8088
_EXEC = f"0-{EXEC_UID}"
_GW = str(GATEWAY_UID)
_WEB = "80,443"
#: Addresses the gateway may reach only when they are the world or a resolver.
_PRIVATE: Final = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "169.254.0.0/16", "172.16.0.0/12",
    "192.168.0.0/16", "224.0.0.0/3",
)  # fmt: skip


def world_addresses(config: Path) -> list[tuple[str, int]]:
    """The IPv4 addresses and port of the world URL in the gateway's config."""
    url = urlsplit(str(json.loads(config.read_text())["world_url"]))
    if not url.hostname:
        raise SystemExit("the world URL has no host")
    port = url.port or (443 if url.scheme == "https" else 80)
    found = socket.getaddrinfo(url.hostname, port, socket.AF_INET, socket.SOCK_STREAM)
    return sorted({(str(info[4][0]), port) for info in found})


def nameservers(resolv: Path = Path("/etc/resolv.conf")) -> list[str]:
    lines = resolv.read_text().splitlines() if resolv.exists() else []
    found = [line.split()[1] for line in lines if line.startswith("nameserver ")]
    return [ip for ip in found if ":" not in ip]


def _gateway_rules(world: Sequence[tuple[str, int]], resolvers: Sequence[str]) -> list[str]:
    return [
        "-A EVAL_GW -o lo -j ACCEPT",
        "-A EVAL_GW -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        *(f"-A EVAL_GW -d {ip} -p {proto} --dport 53 -j ACCEPT"
          for ip in resolvers for proto in ("udp", "tcp")),
        *(f"-A EVAL_GW -d {ip} -p tcp --dport {port} -j ACCEPT" for ip, port in world),
        *(f"-A EVAL_GW -d {net} -j REJECT" for net in _PRIVATE),
        f"-A EVAL_GW -p tcp -m multiport --dports {_WEB} -j ACCEPT",
        "-A EVAL_GW -p tcp -j REJECT --reject-with tcp-reset",
        "-A EVAL_GW -j REJECT",
    ]  # fmt: skip


def ipv4_rules(world: Sequence[tuple[str, int]], resolvers: Sequence[str]) -> str:
    return "\n".join([
        "*nat",
        ":EVAL_OUT - [0:0]",
        "-I OUTPUT 1 -j EVAL_OUT",
        f"-A EVAL_OUT -m owner --uid-owner {_GW} -j RETURN",
        f"-A EVAL_OUT -p udp --dport 53 -j REDIRECT --to-ports {DNS_PORT}",
        f"-A EVAL_OUT -p tcp --dport 53 -j REDIRECT --to-ports {DNS_PORT}",
        "-A EVAL_OUT -d 127.0.0.0/8 -j RETURN",
        f"-A EVAL_OUT -p tcp -m multiport --dports {_WEB} -m owner --uid-owner {_EXEC}"
        f" -j REDIRECT --to-ports {EXEC_PORT}",
        f"-A EVAL_OUT -p tcp -m multiport --dports {_WEB} -j REDIRECT --to-ports {AGENT_PORT}",
        "COMMIT",
        "*filter",
        ":EVAL_GW - [0:0]",
        ":EVAL_IN - [0:0]",
        ":EVAL_FILTER - [0:0]",
        *_gateway_rules(world, resolvers),
        "-I INPUT 1 -j EVAL_IN",
        "-A EVAL_IN -i lo -j ACCEPT",
        "-A EVAL_IN -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        f"-A EVAL_IN -p tcp --dport {HARNESS_PORT} -j ACCEPT",
        "-A EVAL_IN -j DROP",
        "-I OUTPUT 1 -j EVAL_FILTER",
        f"-A EVAL_FILTER -m owner --uid-owner {_GW} -j EVAL_GW",
        # The executor's port tunnels to control-plane hosts: nobody else may dial it.
        f"-A EVAL_FILTER -p tcp --dport {EXEC_PORT} -m owner ! --uid-owner {_EXEC}"
        " -j REJECT --reject-with tcp-reset",
        # A runtime's own resolver (127.0.0.11 on Docker) would answer real names.
        "-A EVAL_FILTER -d 127.0.0.11 -j REJECT",
        f"-A EVAL_FILTER -p udp ! --dport {DNS_PORT} -j REJECT",
        # A redirected packet keeps its first output interface, so match its new address.
        "-A EVAL_FILTER -d 127.0.0.0/8 -j ACCEPT",
        "-A EVAL_FILTER -o lo -j ACCEPT",
        "-A EVAL_FILTER -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        "-A EVAL_FILTER -p tcp -j REJECT --reject-with tcp-reset",
        "-A EVAL_FILTER -j REJECT",
        "COMMIT",
        "",
    ])  # fmt: skip


def ipv6_rules() -> str:
    return (
        "*filter\n:INPUT DROP [0:0]\n:OUTPUT DROP [0:0]\n"
        "-A INPUT -i lo -j ACCEPT\n-A OUTPUT -o lo -j ACCEPT\nCOMMIT\n"
    )


def _variant() -> str:
    """The iptables backend the runtime already uses, so our chains run before its own."""
    legacy = shutil.which("iptables-legacy-save")
    if legacy:
        saved = subprocess.run([legacy, "-t", "nat"], capture_output=True, text=True).stdout
        if "DOCKER_OUTPUT" in saved:
            return "-legacy"
    return ""


def apply(config: Path) -> None:
    rules = ipv4_rules(world_addresses(config), nameservers())
    suffix = _variant()
    subprocess.run([f"iptables{suffix}-restore", "--noflush"], input=rules, text=True, check=True)
    ipv6 = subprocess.run([f"ip6tables{suffix}-restore"], input=ipv6_rules(), text=True)
    inet6 = Path("/proc/net/if_inet6")
    if ipv6.returncode != 0 and inet6.exists() and inet6.read_text().strip():
        raise SystemExit("IPv6 is up but its rules failed; refusing to start")
