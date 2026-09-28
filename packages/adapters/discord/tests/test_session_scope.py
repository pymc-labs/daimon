"""E2E test: Discord turn -> vault credential JWT contains no platform/guild_id wire claims
and resolves to an AuthIdentity through the production verifier path.

Unlike test_orchestration.py which patches create_session, this test lets the
real create_session run against a fake MA over httpx.MockTransport so the JWT
actually gets minted and lands in a recorded credential POST.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import jwt as pyjwt
from daimon.adapters.mcp.auth.resolver import AuthIdentity, resolve_role
from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
from daimon.core.config import McpSettings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.sessions import create_session
from daimon.testing import (
    build_fake_anthropic,
    ma_agent,
    ma_environment,
    ma_session,
    ma_session_agent,
)
from daimon.testing.factories import make_account, make_tenant
from pydantic import HttpUrl, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TEST_GUILD_ID = 123456789012345678  # arbitrary placeholder guild id
SECRET = b"a" * 32
PUBLIC_URL = "https://mcp.example.com/mcp"


def _session_body(*, session_id: str, agent_id: str, environment_id: str) -> dict[str, Any]:
    return ma_session(
        id=session_id,
        agent=ma_session_agent(id=agent_id, name="discord-agent", model="claude-opus-4-7"),
        environment_id=environment_id,
    ).model_dump(mode="json")


def _vault_obj(vault_id: str, display_name: str, created_at: str) -> dict[str, Any]:
    return {
        "id": vault_id,
        "type": "vault",
        "display_name": display_name,
        "metadata": None,
        "archived_at": None,
        "created_at": created_at,
    }


def _credential_obj(credential_id: str, vault_id: str) -> dict[str, Any]:
    return {
        "id": credential_id,
        "type": "vault_credential",
        "vault_id": vault_id,
        "metadata": {},
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-04-01T00:00:00Z",
        "auth": {
            "type": "static_bearer",
            "mcp_server_url": PUBLIC_URL,
        },
    }


def _cold_path_handler(
    *,
    account_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    captured_credential_bodies: list[dict[str, Any]],
    session_id: str = "sess_discord",
) -> Callable[[httpx.Request], httpx.Response]:
    """No existing vault: GET returns empty, POST creates vault + credential, then session."""
    display = f"daimon-mcp:{account_id}:{agent_uuid}"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(200, json={"data": [], "has_more": False})
        if req.method == "POST" and req.url.path == "/v1/vaults":
            return httpx.Response(200, json=_vault_obj("vlt_new", display, "2026-04-24T00:00:00Z"))
        if req.method == "POST" and req.url.path == "/v1/vaults/vlt_new/credentials":
            captured_credential_bodies.append(json.loads(req.content))
            return httpx.Response(200, json=_credential_obj("vcrd_new", "vlt_new"))
        if req.method == "POST" and req.url.path.endswith("/v1/sessions"):
            body = json.loads(req.content)
            return httpx.Response(
                200,
                json=_session_body(
                    session_id=session_id,
                    agent_id=body["agent"],
                    environment_id=body["environment_id"],
                ),
            )
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    return handler


def _warm_rebind_handler(
    *,
    account_id: uuid.UUID,
    agent_uuid: uuid.UUID,
    call_log: list[tuple[str, str]],
    captured_credential_bodies: list[dict[str, Any]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Existing vault with an upgraded chat credential stays stable."""
    display = f"daimon-mcp:{account_id}:{agent_uuid}"

    def handler(req: httpx.Request) -> httpx.Response:
        call_log.append((req.method, req.url.path))
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "data": [_vault_obj("vlt_warm", display, "2026-04-01T00:00:00Z")],
                    "has_more": False,
                },
            )
        if req.method == "GET" and req.url.path == "/v1/vaults/vlt_warm/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            **_credential_obj("vcrd_old", "vlt_warm"),
                            "metadata": {"daimon_chat_identity": str(agent_uuid)},
                        }
                    ],
                    "has_more": False,
                },
            )
        if req.method == "DELETE" and req.url.path == "/v1/vaults/vlt_warm/credentials/vcrd_old":
            return httpx.Response(200, json=_credential_obj("vcrd_old", "vlt_warm"))
        if req.method == "POST" and req.url.path == "/v1/vaults/vlt_warm/credentials":
            captured_credential_bodies.append(json.loads(req.content))
            return httpx.Response(200, json=_credential_obj("vcrd_new", "vlt_warm"))
        if req.method == "POST" and req.url.path.endswith("/v1/sessions"):
            body = json.loads(req.content)
            return httpx.Response(
                200,
                json=_session_body(
                    session_id="sess_warm",
                    agent_id=body["agent"],
                    environment_id=body["environment_id"],
                ),
            )
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    return handler


async def _seed_tenant_and_account(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = await make_tenant(session, platform="discord", workspace_id="test-guild-scope")
    account = await make_account(session, tenant=tenant)
    return tenant.id, account.id


async def test_discord_turn_creates_session_with_discord_guild_id_scope(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _seed_tenant_and_account(db_session)
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_discord")

    captured: list[dict[str, Any]] = []
    client = build_fake_anthropic(
        _cold_path_handler(
            account_id=account_id,
            agent_uuid=agent_uuid,
            captured_credential_bodies=captured,
        )
    )

    await create_session(
        client,
        agent=ma_agent(id="ag_discord", name="discord-agent", model="claude-opus-4-7"),
        environment=ma_environment(id="env_discord", name="discord-env"),
        account_id=account_id,
        agent_uuid=agent_uuid,
        mcp_settings=McpSettings(
            jwt_secret=SecretStr(SECRET.decode()),
            public_url=HttpUrl(PUBLIC_URL),
        ),
        session_factory=db_session_factory,
    )

    assert len(captured) == 1, "exactly one credential POST on cold path"
    token = captured[0]["auth"]["token"]
    claims = pyjwt.decode(token, SECRET, algorithms=["HS256"])
    assert "platform" not in claims, "minted token must carry no platform wire claim"
    assert "guild_id" not in claims, "minted token must carry no guild_id wire claim"


async def test_discord_turn_with_existing_matching_url_vault_skips_rebind(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Existing vault with credential at the current URL: no DELETE, no new credential POST.

    The credential re-stamp limb was removed (per-turn delete+recreate keyed on
    is_admin claim). The credential is identity-stable — only a URL mismatch triggers
    a new credential. Same-URL warm path is now a no-op on vault credentials.
    """
    tenant_id, account_id = await _seed_tenant_and_account(db_session)
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_discord")

    call_log: list[tuple[str, str]] = []
    client = build_fake_anthropic(
        _warm_rebind_handler(
            account_id=account_id,
            agent_uuid=agent_uuid,
            call_log=call_log,
            captured_credential_bodies=[],
        )
    )

    await create_session(
        client,
        agent=ma_agent(id="ag_discord", name="discord-agent", model="claude-opus-4-7"),
        environment=ma_environment(id="env_discord", name="discord-env"),
        account_id=account_id,
        agent_uuid=agent_uuid,
        mcp_settings=McpSettings(
            jwt_secret=SecretStr(SECRET.decode()),
            public_url=HttpUrl(PUBLIC_URL),
        ),
        session_factory=db_session_factory,
    )

    delete_calls = [c for c in call_log if c[0] == "DELETE"]
    post_cred_calls = [c for c in call_log if c == ("POST", "/v1/vaults/vlt_warm/credentials")]
    assert len(delete_calls) == 0, (
        f"existing credential at same URL must NOT be deleted (credential is identity-stable); "
        f"got {delete_calls}"
    )
    assert len(post_cred_calls) == 0, (
        f"no new credential POST expected when URL matches; got {post_cred_calls}"
    )


async def test_minted_jwt_resolves_to_discord_scoped_identity(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Round-trip: mint via create_session, then verify+resolve to AuthIdentity."""
    tenant_id, account_id = await _seed_tenant_and_account(db_session)
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_discord")

    captured: list[dict[str, Any]] = []
    client = build_fake_anthropic(
        _cold_path_handler(
            account_id=account_id,
            agent_uuid=agent_uuid,
            captured_credential_bodies=captured,
        )
    )

    await create_session(
        client,
        agent=ma_agent(id="ag_discord", name="discord-agent", model="claude-opus-4-7"),
        environment=ma_environment(id="env_discord", name="discord-env"),
        account_id=account_id,
        agent_uuid=agent_uuid,
        mcp_settings=McpSettings(
            jwt_secret=SecretStr(SECRET.decode()),
            public_url=HttpUrl(PUBLIC_URL),
        ),
        session_factory=db_session_factory,
    )

    assert len(captured) == 1
    token = captured[0]["auth"]["token"]

    verifier = DaimonJWTVerifier(secret=SECRET, sessionmaker=db_session_factory)
    access = await verifier.verify_token(token)
    assert access is not None, "verifier must accept token minted for a known account"

    # Wire claims: the minted token itself carries no platform/guild_id (decode raw token).
    wire_claims = pyjwt.decode(token, SECRET, algorithms=["HS256"])
    assert "platform" not in wire_claims, "minted token must carry no platform wire claim"
    assert "guild_id" not in wire_claims, "minted token must carry no guild_id wire claim"

    # The verifier recovers identity via the three-table JOIN and injects platform/external_id
    # into the access claims. Build AuthIdentity the same way IdentityMiddleware does.
    claims = access.claims
    sub = claims.get("sub")
    assert isinstance(sub, str)
    tid_claim = claims.get("tenant_id")
    assert isinstance(tid_claim, str)
    role_str = claims.get("role")
    assert role_str is None or isinstance(role_str, str)
    platform_claim = claims.get("platform")
    assert platform_claim is None or isinstance(platform_claim, str)
    external_id_claim = claims.get("external_id")
    assert external_id_claim is None or isinstance(external_id_claim, str)

    identity = AuthIdentity(
        account_id=uuid.UUID(sub),
        tenant_id=uuid.UUID(tid_claim),
        role=resolve_role(role_str),
        platform=platform_claim,
        external_id=external_id_claim,
    )

    assert identity.account_id == account_id, "resolved account must match seed"
    assert identity.tenant_id == tenant_id, "resolved tenant must match seed"
    assert identity.platform == "discord", "verifier injects platform from the tenant JOIN"
    assert identity.external_id == "test-guild-scope", (
        "verifier injects external_id from the tenant JOIN"
    )
