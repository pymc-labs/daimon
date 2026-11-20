"""The gateway addon: flows become world requests; only the executor reaches control-plane hosts."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from daimon_eval_sandbox.gateway import (
    AGENT_PORT,
    AGENT_UID,
    EXEC_PORT,
    Gateway,
    GatewayConfig,
    fake_ip,
)
from mitmproxy import options, tls
from mitmproxy.proxy.context import Context
from mitmproxy.test import taddons, tflow

CONFIG = GatewayConfig(
    world_url="http://world.test",
    world_token="tok",
    item="item-1",
    control_plane=frozenset({"api.anthropic.com"}),
)


def _flow(port: int, *, host: str = "api.github.com", method: str = "POST"):
    flow = tflow.tflow()
    flow.client_conn.sockname = ("127.0.0.1", port)
    flow.client_conn.sni = host
    flow.request.scheme, flow.request.port, flow.request.host = "https", 443, "198.18.0.7"
    flow.request.headers["host"] = host  # as in transparent mode: the address is an IP
    flow.request.method = method
    flow.request.path = "/repos/acme/app/issues"
    flow.request.content = b'{"title":"x"}'
    return flow


def _world(seen: list[httpx.Request], answer: dict[str, object]) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=answer)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def test_flow_becomes_world_request_and_its_answer() -> None:
    seen: list[httpx.Request] = []
    reply = {"status": 201, "headers": {"x-id": "7"}, "body_b64": base64.b64encode(b"made").decode()}
    gateway = Gateway(CONFIG, _world(seen, {**reply, "refused": False}))
    flow = _flow(AGENT_PORT)
    with taddons.context(gateway):
        await gateway.request(flow)
    assert len(seen) == 1, "one world call per flow"
    sent = json.loads(seen[0].content)
    assert str(seen[0].url) == "http://world.test/world/item-1/http", "posted to the item's world route"
    assert seen[0].headers["authorization"] == "Bearer tok", "authenticated with the world token"
    assert sent["url"] == "https://api.github.com/repos/acme/app/issues", "url uses the host name"
    assert base64.b64decode(sent["body_b64"]) == b'{"title":"x"}', "body passed through"
    assert sent["client_uid"] == AGENT_UID and sent["sni"] == "api.github.com", "origin recorded"
    assert flow.response is not None and flow.response.status_code == 201, "world status used"
    assert flow.response.content == b"made" and flow.response.headers["x-id"] == "7", "world body"


async def test_refused_world_answer_closes_the_connection() -> None:
    gateway = Gateway(CONFIG, _world([], {"status": 403, "refused": True}))
    flow = _flow(AGENT_PORT)
    with taddons.context(gateway):
        await gateway.request(flow)
    assert flow.response is None and flow.error is not None, "a refused call is killed"


@pytest.mark.parametrize(("port", "tunnelled"), [(EXEC_PORT, True), (AGENT_PORT, False)])
def test_control_plane_sni_passes_through_only_for_the_executor(port: int, tunnelled: bool) -> None:
    client = tflow.tclient_conn()
    client.sockname = ("127.0.0.1", port)
    context = Context(client, options.Options())
    context.server.address = ("198.18.0.9", 443)
    hello = tls.ClientHelloData(context, SimpleNamespace(sni="api.anthropic.com"))  # type: ignore[arg-type]
    Gateway(CONFIG).tls_clienthello(hello)
    assert hello.ignore_connection is tunnelled, "only the executor port tunnels untouched"
    wanted = ("api.anthropic.com", 443) if tunnelled else ("198.18.0.9", 443)
    assert context.server.address == wanted, "a tunnel dials the real host by name"


async def test_executor_traffic_to_other_hosts_is_closed() -> None:
    seen: list[httpx.Request] = []
    gateway = Gateway(CONFIG, _world(seen, {}))
    flow = _flow(EXEC_PORT)
    with taddons.context(gateway):
        await gateway.request(flow)
    assert flow.error is not None and not seen, "the executor reaches control-plane hosts only"


@pytest.mark.parametrize(("mode", "method", "upstream"), [
    ("passthrough", "GET", True), ("passthrough", "POST", False), ("deny", "GET", False),
])  # fmt: skip
async def test_registry_reads_follow_the_feature(mode: str, method: str, upstream: bool) -> None:
    seen: list[httpx.Request] = []
    config = GatewayConfig(**{**CONFIG.__dict__, "registries": mode})
    gateway = Gateway(config, _world(seen, {"status": 404, "refused": False}))
    flow = _flow(AGENT_PORT, host="pypi.org", method=method)
    with taddons.context(gateway):
        await gateway.request(flow)
    went_up = flow.response is None and flow.error is None and flow.request.host == "pypi.org"
    assert went_up is upstream, f"{mode} {method} registry call goes upstream: {upstream}"
    if mode == "deny":
        assert len(seen) == 1, "a denied registry call is answered by the world"


def test_fake_addresses_are_stable_and_unroutable() -> None:
    assert fake_ip("api.github.com") == fake_ip("API.github.com."), "stable per name"
    assert str(fake_ip("x.test")).startswith("198.1"), "inside the benchmarking range"
