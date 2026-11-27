"""mitmproxy addon for an eval pod: every connection is answered by the eval world.

The gateway dials out in two cases only, both checked in `server_connect`, which every
upstream connection passes (raw TCP, failed hooks and tunnels included): TLS to a
control-plane host on the executor's port, tunnelled untouched, and package-registry reads
when the `eval_package_registries` feature allows them. Every other dial is refused.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import httpx
from mitmproxy import dns, http, tls
from mitmproxy.net.dns import types as dns_types
from mitmproxy.proxy.server_hooks import ServerConnectionHookData

AGENT_PORT: Final = 8080
EXEC_PORT: Final = 8081
DNS_PORT: Final = 5353
EXEC_UID: Final = 10000
AGENT_UID: Final = 10001
GATEWAY_UID: Final = 61000
#: Hosts an agent may read packages from when registries pass through. No module proxy
#: that fetches arbitrary hosts on request (the Go proxy does) belongs here.
REGISTRY_HOSTS: Final = frozenset({
    "pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "registry.yarnpkg.com",
    "index.crates.io", "static.crates.io",
})  # fmt: skip
_READS: Final = frozenset({"GET", "HEAD"})
_FAKE_NET: Final = ipaddress.IPv4Network("198.18.0.0/15")
_SKIP_HEADERS: Final = frozenset({"content-length", "transfer-encoding", "content-encoding"})


@dataclass(frozen=True)
class GatewayConfig:
    world_url: str
    world_token: str
    item: str
    control_plane: frozenset[str]
    registries: Literal["passthrough", "deny"] = "deny"

    @classmethod
    def load(cls, path: Path) -> GatewayConfig:
        raw = json.loads(path.read_text())
        registries = raw.get("registries", "deny")
        if registries not in ("passthrough", "deny"):
            raise ValueError(f"unknown registries mode {registries!r}")
        return cls(
            world_url=str(raw["world_url"]).rstrip("/"), world_token=str(raw["world_token"]),
            item=str(raw["item"]), registries=registries,
            control_plane=frozenset(str(h).lower() for h in raw.get("control_plane", [])),
        )  # fmt: skip


def fake_ip(name: str) -> ipaddress.IPv4Address:
    """A stable address for a name, inside a range that never routes anywhere."""
    digest = int.from_bytes(hashlib.sha256(name.lower().rstrip(".").encode()).digest()[:4])
    return _FAKE_NET[1 + digest % (_FAKE_NET.num_addresses - 2)]


def client_uid(port: int) -> int:
    return EXEC_UID if port == EXEC_PORT else AGENT_UID


def world_request(flow: http.HTTPFlow) -> dict[str, object]:
    request = flow.request
    headers: dict[str, str] = {}
    for raw_key, raw_value in request.headers.fields:
        name, value = raw_key.decode("latin-1").lower(), raw_value.decode("latin-1")
        headers[name] = f"{headers[name]}, {value}" if name in headers else value
    return {
        "method": request.method, "url": request.pretty_url, "headers": headers,
        "body_b64": base64.b64encode(request.raw_content or b"").decode(),
        "sni": flow.client_conn.sni, "client_uid": client_uid(flow.client_conn.sockname[1]),
    }  # fmt: skip


def _log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, "t": round(time.time(), 3), **fields}), flush=True)


class Gateway:
    def __init__(self, config: GatewayConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client or httpx.AsyncClient(timeout=180, trust_env=False)

    def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        sni = (data.client_hello.sni or "").lower()
        if data.context.client.sockname[1] == EXEC_PORT and sni in self.config.control_plane:
            data.ignore_connection = True
            data.context.server.address = (sni, 443)
            _log("gateway.passthrough_opened", host=sni, uid=EXEC_UID)

    async def request(self, flow: http.HTTPFlow) -> None:
        port = flow.client_conn.sockname[1]
        host = flow.request.pretty_host.lower()
        if port == EXEC_PORT:
            _log("gateway.exec_refused", host=host)
            flow.kill()
            return
        if host in REGISTRY_HOSTS and self.config.registries == "passthrough":
            if flow.request.method not in _READS:
                flow.kill()
                return
            flow.request.host = host
            _log("gateway.registry_read", host=host, path=flow.request.path)
            return
        url = f"{self.config.world_url}/world/{self.config.item}/http"
        try:
            reply = await self.client.post(
                url, json=world_request(flow),
                headers={"authorization": f"Bearer {self.config.world_token}"},
            )  # fmt: skip
            reply.raise_for_status()
            answer = reply.json()
        except (httpx.HTTPError, ValueError) as error:
            _log("gateway.world_failed", host=host, error=type(error).__name__)
            flow.kill()
            return
        if answer.get("refused"):
            flow.kill()
            return
        headers = {
            str(k): str(v) for k, v in dict(answer.get("headers") or {}).items()
            if str(k).lower() not in _SKIP_HEADERS
        }  # fmt: skip
        body = base64.b64decode(str(answer.get("body_b64") or ""))
        flow.response = http.Response.make(int(answer.get("status", 502)), body, headers)

    def may_dial(self, port: int, address: tuple[str, int] | None) -> bool:
        """The only upstream connections the gateway opens; anything else stays in the world."""
        if address is None:
            return False
        host, server_port = address[0].lower(), address[1]
        if port == EXEC_PORT:
            return server_port == 443 and host in self.config.control_plane
        return (
            port == AGENT_PORT
            and self.config.registries == "passthrough"
            and host in REGISTRY_HOSTS
            and server_port in (80, 443)
        )

    def server_connect(self, data: ServerConnectionHookData) -> None:
        # flow.kill() in a TCP or failed HTTP hook does not stop the dial; this does.
        if not self.may_dial(data.client.sockname[1], data.server.address):
            _log("gateway.dial_refused", address=str(data.server.address))
            data.server.error = "the eval world answers every connection"

    def dns_request(self, flow: dns.DNSFlow) -> None:
        question = flow.request.question
        if question is not None and question.type == dns_types.A:
            flow.response = flow.request.succeed(
                [dns.ResourceRecord.A(question.name, fake_ip(question.name))]
            )
        else:
            flow.response = flow.request.succeed([])
