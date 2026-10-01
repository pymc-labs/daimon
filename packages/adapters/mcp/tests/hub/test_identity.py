"""HubIdentityMiddleware turns proxy token claims into a HubIdentity in request state."""

from __future__ import annotations

import uuid

from daimon.adapters.mcp.hub.app import build_hub_app
from daimon.adapters.mcp.hub.claims import decode_hub_claims, encode_hub_claims
from daimon.adapters.mcp.hub.identity import HubIdentity, HubIdentityMiddleware, _hub_auth
from daimon.core.hub_identity import HubTenant
from daimon.testing.asgi import call_mcp_tool
from fastmcp import Context, FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_app import _runtime

_TENANT = HubTenant(
    tenant_id=uuid.uuid4(), account_id=uuid.uuid4(), workspace_id="g1", workspace_name="PyMC"
)


def test_claims_round_trip() -> None:
    claims = encode_hub_claims(platform="discord", platform_user_id="u1", tenants=[_TENANT])
    identity = decode_hub_claims({"sub": "u1", "upstream_claims": claims})
    assert identity == HubIdentity(platform="discord", platform_user_id="u1", tenants=(_TENANT,)), (
        f"round trip changed the identity: {identity!r}"
    )


def test_decode_returns_none_without_upstream_claims() -> None:
    assert decode_hub_claims({"sub": "u1"}) is None, "missing upstream_claims must decode to None"


def test_decode_returns_none_on_malformed_tenant() -> None:
    bad = {"platform": "discord", "platform_user_id": "u1", "tenants": [{"tenant_id": "nope"}]}
    assert decode_hub_claims({"upstream_claims": bad}) is None, "malformed tenant must fail closed"


def _app_with_tokens(tokens: dict[str, dict[str, object]]) -> FastMCP:
    mcp = FastMCP(name="hub-test", auth=StaticTokenVerifier(tokens=tokens))
    mcp.add_middleware(HubIdentityMiddleware("slack"))

    @mcp.tool
    async def whoami(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        return (await _hub_auth(ctx)).platform_user_id

    return mcp


async def test_middleware_exposes_identity_to_tools() -> None:
    claims = encode_hub_claims(platform="slack", platform_user_id="U1", tenants=[_TENANT])
    mcp = _app_with_tokens({"tok": {"sub": "U1", "client_id": "c", "upstream_claims": claims}})
    result = await call_mcp_tool(mcp.http_app(), token="tok", name="whoami")
    payload = result.get("result", result)
    assert isinstance(payload, dict), f"unexpected tools/call shape: {result!r}"
    assert not payload.get("isError"), (
        f"whoami should succeed for a hub-claims token; got {payload!r}"
    )
    content = payload.get("content") or []
    text = content[0]["text"] if content else None  # type: ignore[index]
    assert text == "U1", f"tool must see the login identity, got {payload!r}"


async def test_middleware_rejects_token_without_hub_claims() -> None:
    mcp = _app_with_tokens({"tok": {"sub": "U1", "client_id": "c"}})
    result = await call_mcp_tool(mcp.http_app(), token="tok", name="whoami")
    # The middleware raises AuthorizationError before the tool is dispatched, so
    # the request fails at the JSON-RPC level rather than surfacing as a tool
    # result with isError=True.
    assert "error" in result, f"token without hub claims must be rejected; got {result!r}"


async def test_middleware_rejects_a_token_issued_for_another_platform(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    claims = encode_hub_claims(platform="slack", platform_user_id="U1", tenants=[_TENANT])
    auth = StaticTokenVerifier(
        tokens={"tok": {"sub": "U1", "client_id": "c", "upstream_claims": claims}}
    )
    mcp = build_hub_app(
        platform="discord", runtime=_runtime(sessionmaker), auth=auth, billing_config=None
    )

    result = await call_mcp_tool(
        mcp.http_app(path="/mcp", stateless_http=True, json_response=True),
        token="tok",
        name="list_daimons",
    )

    payload = result.get("result", result)
    assert isinstance(payload, dict), f"unexpected tools/call shape: {result!r}"
    assert payload.get("isError"), (
        f"a Slack login must not be accepted by the Discord mount: {result!r}"
    )
