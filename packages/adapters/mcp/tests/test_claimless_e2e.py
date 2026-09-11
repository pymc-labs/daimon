"""Claim-less end-to-end keystone acceptance test (REQ-6 gate).

Proves that a JWT minted with only `sub` (no platform / guild_id wire claims)
drives both a routines tool and a discord tool through the full real chain:

  real DaimonJWTVerifier → DB JOIN → claim injection
  → IdentityMiddleware reads injected claims
  → routines tool resolves tenant_id
  → discord tool resolves external_id + platform_user_id

This test MUST use httpx.ASGITransport over create_mcp_app — the in-process
fastmcp.Client path (StaticTokenVerifier) cannot prove server-side JOIN
recovery.
"""

from __future__ import annotations

import jwt as pyjwt
import pytest
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    McpSettings,
    Settings,
)
from daimon.core.stores import accounts
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .factories import mcp_session

pytestmark = pytest.mark.asyncio

SECRET = "a" * 32


async def test_claimless_token_drives_routines_and_discord_tools(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Sub-only JWT drives routines + discord tool through real DaimonJWTVerifier.

    The token carries no platform/guild_id wire claims. The verifier performs a
    three-table JOIN (accounts → tenants, platform_principals) and injects
    tenant_id, platform, external_id, and platform_user_id into AccessToken.claims.
    IdentityMiddleware reads these inline (no second DB query) and populates
    AuthIdentity. Both tools must succeed at the identity-resolution layer —
    proving the claim-less flow is end-to-end wired correctly.
    """
    # --- Seed discord tenant + account + PlatformPrincipal ---
    # The LEFT JOIN in get_account_with_tenant requires a PlatformPrincipal row
    # so platform_user_id is recoverable from the token's sub alone.
    tenant = await make_tenant(db_session, platform="discord", workspace_id="guild-claimless")
    account = await make_account(db_session, tenant=tenant)
    await accounts.set_role(db_session, account.id, Role.ADMIN)
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="user-claimless",
        tenant=tenant,
        account=account,
    )
    await db_session.commit()

    # --- Mint claim-less token (sub only, no platform/guild_id wire claims) ---
    token = pyjwt.encode(
        {"sub": str(account.id), "iat": 0},
        SECRET,
        algorithm="HS256",
    )

    # Verify the minted token carries no platform or guild_id wire claims (REQ-1)
    decoded = pyjwt.decode(token, SECRET, algorithms=["HS256"])
    assert "platform" not in decoded, "minted token must carry no platform wire claim"
    assert "guild_id" not in decoded, "minted token must carry no guild_id wire claim"

    # --- Build app with real DaimonJWTVerifier (not StaticTokenVerifier) ---
    # The jwt_secret param causes create_mcp_app to construct DaimonJWTVerifier;
    # passing auth=None is the production path.
    # discord settings must be non-None so register_channel_tools is called.
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr(SECRET), public_url=HttpUrl("https://test/mcp")),
            discord=DiscordSettings(bot_token=SecretStr("Bot fake-bot-token")),
        ),
        sessionmaker=sessionmaker,
    )

    # --- Drive routines tool (list_routines takes no args; returns empty list) ---
    # Any non-401 response proves the verifier accepted the token and injected tenant_id.
    routines_result = await mcp_session(
        app,
        token=token,
        method="tools/call",
        params={"name": "list_routines", "arguments": {}},
    )
    # Extract the inner result; the shape is {"result": {"content": [...], "isError": ...}}
    # or the outer is {"result": ...} depending on FastMCP response format.
    inner: dict[str, object] = routines_result.get("result", routines_result)  # type: ignore[assignment]
    is_routines_error = inner.get("isError", False)
    routines_content: list[dict[str, object]] = inner.get("content", [])  # type: ignore[assignment]
    routines_text = " ".join(str(item.get("text", "")) for item in routines_content)
    assert not is_routines_error, (
        f"list_routines must not return a tool error — verifier must have resolved tenant_id "
        f"from the DB JOIN; got: {routines_text!r}"
    )

    # --- Drive discord tool (read_channel) ---
    # After identity recovery, the tool will try to hit the Discord REST API
    # with the fake bot token and fail with a discord API error (aiohttp / discord.py
    # raises). That failure surfaces as isError=True with a discord-domain message.
    # What must NOT happen: ToolError("discord tools require a discord-bound identity")
    # or ToolError("discord tools require a guild context") — those would mean the
    # verifier failed to inject platform_user_id / external_id into claims.
    discord_result = await mcp_session(
        app,
        token=token,
        method="tools/call",
        params={"name": "read_channel", "arguments": {"channel_id": "123456789"}},
    )
    discord_inner: dict[str, object] = discord_result.get("result", discord_result)  # type: ignore[assignment]
    discord_content: list[dict[str, object]] = discord_inner.get("content", [])  # type: ignore[assignment]
    discord_text = " ".join(str(item.get("text", "")) for item in discord_content)
    # These messages mean _require_discord_identity / _require_guild_id raised — identity
    # was NOT injected by the verifier JOIN. Either would be a bug.
    assert "discord tools require a discord-bound identity" not in discord_text, (
        "_require_discord_identity raised ToolError — platform_user_id was not injected "
        "by the verifier JOIN into AccessToken.claims"
    )
    assert "discord tools require a guild context" not in discord_text, (
        "_require_guild_id raised ToolError — external_id was not injected "
        "by the verifier JOIN into AccessToken.claims"
    )
