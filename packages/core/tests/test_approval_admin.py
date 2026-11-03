"""Fresh native admin evidence cannot cross users or workspaces."""

from __future__ import annotations

import uuid

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.core.approval_admin import approval_admin
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    DiscordSettings,
    Settings,
    TeamsSettings,
)
from daimon.core.stores.daimon_admins import change_grant
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing.factories import make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD = "123456789012345678"
USER = "223456789012345678"


def settings(**kwargs: object) -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            **kwargs,
        }
    )


@pytest.mark.parametrize("signal", ["admin", "owner", "manage", "member", "foreign", "error"])
async def test_discord_uses_registered_guild_and_live_permissions(
    db_session_factory: async_sessionmaker[AsyncSession],
    signal: str,
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, workspace_id=GUILD)
        if signal == "member":
            await change_grant(
                session,
                tenant_id=tenant.id,
                platform=tenant.platform,
                subject_kind="user",
                subject_id=USER,
                actor_user_id="platform-owner",
                is_platform_admin=True,
            )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bot token"
        if signal == "error":
            return httpx.Response(503)
        if request.url.path.endswith("/roles"):
            return httpx.Response(
                200,
                json=[
                    {"id": GUILD, "permissions": "0"},
                    {
                        "id": "323456789012345678",
                        "permissions": "8"
                        if signal == "admin"
                        else "32"
                        if signal == "manage"
                        else "0",
                    },
                ],
            )
        if "/members/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "user": {"id": GUILD if signal == "foreign" else USER},
                    "roles": ["323456789012345678"],
                },
            )
        return httpx.Response(
            200, json={"id": GUILD, "owner_id": USER if signal == "owner" else GUILD}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        subject = await approval_admin(
            db_session_factory,
            settings(discord=DiscordSettings(bot_token=SecretStr("token"))),
            tenant_id=tenant.id,
            user_id=USER,
            http_client=http,
        )
    assert subject.is_admin is (signal in ("admin", "owner", "manage"))
    assert all(f"/guilds/{GUILD}" in path for path in calls)


@pytest.mark.parametrize(
    "signal", ["admin", "owner", "member", "foreign_team", "foreign_user", "deleted", "error"]
)
async def test_slack_does_not_trust_foreign_or_deleted_admins(
    db_session_factory: async_sessionmaker[AsyncSession],
    signal: str,
) -> None:
    key = Fernet.generate_key()
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id="T1")
        if signal == "member":
            await change_grant(
                session,
                tenant_id=tenant.id,
                platform=tenant.platform,
                subject_kind="user",
                subject_id="U1",
                actor_user_id="platform-owner",
                is_platform_admin=True,
            )
        await upsert_slack_bot_token(
            session, team_id="T1", encrypted_token=Fernet(key).encrypt(b"bot-token")
        )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["user"] == "U1"
        assert request.headers["Authorization"] == "Bearer bot-token"
        if signal == "error":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "user": {
                    "id": "U2" if signal == "foreign_user" else "U1",
                    "team_id": "T2" if signal == "foreign_team" else "T1",
                    "is_admin": signal != "member" and signal != "owner",
                    "is_owner": signal == "owner",
                    "deleted": signal == "deleted",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        subject = await approval_admin(
            db_session_factory,
            settings(crypto=CryptoSettings(keys=(SecretStr(key.decode()),))),
            tenant_id=tenant.id,
            user_id="U1",
            http_client=http,
        )
    assert subject.is_admin is (signal in ("admin", "owner"))


@pytest.mark.parametrize("signal", ["owner", "member", "error"])
async def test_teams_reads_current_admin_team_owners(
    db_session_factory: async_sessionmaker[AsyncSession],
    signal: str,
) -> None:
    org, team, user = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="teams", workspace_id=org)
        if signal == "member":
            await change_grant(
                session,
                tenant_id=tenant.id,
                platform=tenant.platform,
                subject_kind="user",
                subject_id=user,
                actor_user_id="platform-owner",
                is_platform_admin=True,
            )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            assert org in request.url.path
            return httpx.Response(200, json={"access_token": "graph-token"})
        assert f"/teams/{team}/members" in request.url.path
        if signal == "error":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "@odata.type": "#microsoft.graph.aadUserConversationMember",
                        "userId": user,
                        "roles": ["owner"] if signal == "owner" else [],
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        subject = await approval_admin(
            db_session_factory,
            settings(
                teams=TeamsSettings(
                    tenant_id=org,
                    admin_team_id=team,
                    client_id="app",
                    client_secret=SecretStr("secret"),
                )
            ),
            tenant_id=tenant.id,
            user_id=user,
            http_client=http,
        )
    assert subject.is_admin is (signal == "owner")
