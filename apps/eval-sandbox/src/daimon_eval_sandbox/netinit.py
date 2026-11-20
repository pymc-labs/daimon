"""Packet rules for an eval pod's shared network namespace. Any failure exits non-zero.

Only the gateway uid reaches the network. Web ports are redirected to the gateway: the
executor (root and uids up to `EXEC_UID`) to its port, everyone else to the agent port.
DNS goes to the gateway's fake resolver. Everything else, IPv6 included, is rejected.
Our chains are inserted first and nothing is flushed, so a runtime's own rules stay.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from daimon_eval_sandbox.gateway import AGENT_PORT, DNS_PORT, EXEC_PORT, EXEC_UID, GATEWAY_UID

_EXEC = f"0-{EXEC_UID}"
_GW = str(GATEWAY_UID)
_WEB = "80,443"


def ipv4_rules() -> str:
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
        ":EVAL_FILTER - [0:0]",
        "-I OUTPUT 1 -j EVAL_FILTER",
        f"-A EVAL_FILTER -m owner --uid-owner {_GW} -j ACCEPT",
        # The executor's port tunnels to control-plane hosts: nobody else may dial it.
        f"-A EVAL_FILTER -o lo -p tcp --dport {EXEC_PORT} -m owner ! --uid-owner {_EXEC}"
        " -j REJECT --reject-with tcp-reset",
        # A runtime's own resolver (127.0.0.11 on Docker) would answer real names.
        "-A EVAL_FILTER -d 127.0.0.11 -j REJECT",
        f"-A EVAL_FILTER -o lo -p udp ! --dport {DNS_PORT} -j REJECT",
        "-A EVAL_FILTER -o lo -j ACCEPT",
        "-A EVAL_FILTER -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        "-A EVAL_FILTER -p tcp -j REJECT --reject-with tcp-reset",
        "-A EVAL_FILTER -j REJECT",
        "COMMIT",
        "",
    ])  # fmt: skip


def ipv6_rules() -> str:
    return "*filter\n:OUTPUT DROP [0:0]\n-A OUTPUT -o lo -j ACCEPT\nCOMMIT\n"


def _variant() -> str:
    """The iptables backend the runtime already uses, so our chains run before its own."""
    legacy = shutil.which("iptables-legacy-save")
    if legacy:
        saved = subprocess.run([legacy, "-t", "nat"], capture_output=True, text=True).stdout
        if "DOCKER_OUTPUT" in saved:
            return "-legacy"
    return ""


def apply() -> None:
    suffix = _variant()
    subprocess.run(
        [f"iptables{suffix}-restore", "--noflush"], input=ipv4_rules(), text=True, check=True
    )
    ipv6 = subprocess.run([f"ip6tables{suffix}-restore"], input=ipv6_rules(), text=True)
    inet6 = Path("/proc/net/if_inet6")
    if ipv6.returncode != 0 and inet6.exists() and inet6.read_text().strip():
        raise SystemExit("IPv6 is up but its rules failed; refusing to start")
