"""Several Gemini MCP servers may bind when they share one host credential."""

import pytest
from mux.contracts.ids import Scope
from mux.contracts.resources import MCPConnection
from mux.drivers.gemini.mcp_auth import (
    Object,
    authenticated_template,
    compile_mcp,
    refresh_bound_request,
)
from mux.errors import UnsupportedCapability
from pydantic import JsonValue

URL = "https://daimon.invalid/mcp"
REF = "host:owned-token"
SCOPE = Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="allowed")
DAIMON = MCPConnection(
    name="daimon-mcp", url=URL, credential_ref=REF, tool_policy={"allowed_tools": ["describe"]}
)
SHARED = (
    DAIMON,
    MCPConnection(name="drive", url=URL + "/connectors/drive", credential_ref=REF),
    MCPConnection(name="notion", url=URL + "/connectors/notion", credential_ref=REF),
)


def _headers(request: Object) -> list[tuple[JsonValue, str]]:
    tools = request["tools"]
    assert isinstance(tools, list), "tools stay a list"
    rows: list[tuple[JsonValue, str]] = []
    for tool in tools:
        assert isinstance(tool, dict) and isinstance(tool["headers"], dict), "each tool binds"
        rows.append((tool["url"], str(tool["headers"]["Authorization"])))
    return rows


async def test_each_shared_server_gets_its_own_resolved_bearer_on_every_dispatch() -> None:
    resolved: list[str] = []

    async def resolve(scope: Scope, ref: str, destination: str) -> str:
        assert scope == SCOPE and ref == REF, "only the shared reference resolves"
        resolved.append(destination)
        return f"bearer-{len(resolved)}"

    tools = compile_mcp(SHARED, enabled=True)
    assert [t for t in tools if isinstance(t, dict) and "allowed_tools" not in t] == tools[1:], (
        "relay servers after the first carry no allowlist"
    )
    expected: Object = {"agent": "a", "agent_config": {"model": "m"}, "tools": tools}
    request = await authenticated_template(SCOPE, expected, expected, SHARED, resolve, overrides={})
    assert _headers(request) == [(c.url, f"Bearer bearer-{n}") for n, c in enumerate(SHARED, 1)]
    fresh = await refresh_bound_request(request)
    assert _headers(fresh) == [(c.url, f"Bearer bearer-{n}") for n, c in enumerate(SHARED, 4)]
    assert resolved == [c.url for c in SHARED] * 2, "every destination is authorized each time"


@pytest.mark.parametrize(
    "second",
    [
        MCPConnection(name="drive", url=URL + "/connectors/drive", credential_ref="host:other"),
        MCPConnection(name="drive", url=URL + "/connectors/drive"),
    ],
)
def test_servers_on_different_or_no_credentials_still_refuse(second: MCPConnection) -> None:
    with pytest.raises(UnsupportedCapability) as refused:
        compile_mcp((DAIMON, second), enabled=True)
    assert refused.value.missing == ("one_resolver_bound_mcp_server",), "one reference only"


def test_the_first_bound_server_still_needs_an_allowlist() -> None:
    bare = DAIMON.model_copy(update={"tool_policy": {}})
    with pytest.raises(UnsupportedCapability) as refused:
        compile_mcp((bare, *SHARED[1:]), enabled=True)
    assert refused.value.missing == ("mcp_tool_policy",), "the host server stays allowlisted"
