"""`explain_dm`: a DM decision for one person, asked by an admin, with groups read live."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from cryptography.fernet import Fernet, MultiFernet
from daimon.core.authz import AgentRef
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    DiscordSettings,
    Settings,
    TeamsSettings,
)
from daimon.core.dm_access import DmAgent
from daimon.core.dm_explain import explain_dm
from daimon.core.dm_routing import DmChoose, DmStart, DmUnchecked
from daimon.core.github_credentials import encrypt_token
from daimon.core.stores import dm_access as store
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.stores.domain import Platform, TenantRow
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing.factories import make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 1, 1, tzinfo=UTC)
KEY = Fernet.generate_key().decode()
FERNET = MultiFernet([Fernet(KEY)])
ENTRA = str(uuid.UUID(int=0xABC))
GUILD = "700000202"


def _settings(**kwargs: object) -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "crypto": CryptoSettings(keys=(SecretStr(KEY),)),
            **kwargs,
        }
    )


BASE = _settings()
DISCORD = _settings(discord=DiscordSettings(bot_token=SecretStr("bot")))
TEAMS = _settings(
    teams=TeamsSettings(
        client_id=str(uuid.UUID(int=1)), client_secret=SecretStr("s"), tenant_id=ENTRA
    )
)

Handler = Callable[[httpx.Request], httpx.Response]


async def _workspace(
    factory: async_sessionmaker[AsyncSession], *, platform: Platform, workspace_id: str
) -> tuple[TenantRow, list[DmAgent]]:
    """DMs on, and a locked rule sending group `g-1` (or role `55`) to agent `staff`."""
    group = "55" if platform == "discord" else "g-1"
    async with factory.begin() as session:
        tenant = await make_tenant(session, platform=platform, workspace_id=workspace_id)
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
        await store.add_rule(
            session, tenant_id=tenant.id, subject="group", subject_id=group, agent_name="staff",
            locked=True, set_by=None, position=None, now=NOW,
        )  # fmt: skip
    agents = [DmAgent(n, f"ag_{n}", AgentRef.of(n)) for n in ("staff", "help")]
    return tenant, agents


def _guild(request: httpx.Request, *, roles: list[str]) -> httpx.Response:
    """Discord's member, guild and roles endpoints for member `11` holding `roles`."""
    path = request.url.path.removeprefix(f"/api/v10/guilds/{GUILD}")
    if path == "/members/11":
        return httpx.Response(200, json={"user": {"id": "11"}, "roles": roles})
    if path == "":
        return httpx.Response(200, json={"id": GUILD, "owner_id": "1"})
    assert path == "/roles", request.url
    every = [GUILD, *roles]
    return httpx.Response(200, json=[{"id": r, "permissions": "0"} for r in every])


async def _explain(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    handler: Handler,
    tenant: TenantRow,
    agents: list[DmAgent],
    user_id: str,
):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        return await explain_dm(
            factory, settings, http, tenant=tenant, agents=agents, user_id=user_id
        )


async def test_explain_dm_on_discord_when_the_member_holds_the_role_then_the_lock_decides(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The member's roles are fetched from Discord; one a locked rule names sends them there."""
    tenant, agents = await _workspace(db_session_factory, platform="discord", workspace_id=GUILD)
    asked: list[str] = []

    def discord(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        assert request.headers["Authorization"] == "Bot bot", request.headers
        return _guild(request, roles=["55", "66"])

    found = await _explain(db_session_factory, DISCORD, discord, tenant, agents, "11")

    assert asked[0] == f"/api/v10/guilds/{GUILD}/members/11", asked
    assert found.decision.route == DmStart("staff", "locked"), found.decision
    assert found.groups == frozenset({"55"}), "only the groups a rule names are reported"


async def test_explain_dm_on_discord_when_discord_fails_then_it_is_unchecked(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A failed member read leaves the roles unknown, as a real DM would: never a guess."""
    tenant, agents = await _workspace(db_session_factory, platform="discord", workspace_id=GUILD)

    found = await _explain(
        db_session_factory, DISCORD, lambda _: httpx.Response(500), tenant, agents, "11"
    )

    assert found.decision.route == DmUnchecked(), found.decision
    assert found.groups is None, "unknown groups are reported as unknown"


async def test_explain_dm_on_discord_when_not_in_the_server_then_they_hold_no_role(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Someone Discord does not list holds no role, so a lock on a role passes them by."""
    tenant, agents = await _workspace(db_session_factory, platform="discord", workspace_id=GUILD)

    found = await _explain(
        db_session_factory, DISCORD, lambda _: httpx.Response(404), tenant, agents, "11"
    )

    assert found.groups == frozenset(), "a known answer: no roles"
    assert found.decision.route == DmChoose(("help", "staff")), found.decision


async def test_explain_dm_on_slack_reads_the_user_group_with_the_stored_bot_token(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Slack user groups are read with the workspace's own bot token."""
    tenant, agents = await _workspace(db_session_factory, platform="slack", workspace_id="T1")
    async with db_session_factory.begin() as session:
        await upsert_slack_bot_token(
            session, team_id="T1", encrypted_token=encrypt_token(FERNET, "xoxb-1")
        )
    auth: list[str | None] = []

    def slack(request: httpx.Request) -> httpx.Response:
        auth.append(request.headers.get("Authorization"))
        assert request.url.params["usergroup"] == "g-1", request.url
        return httpx.Response(200, json={"ok": True, "users": ["U1"]})

    member = await _explain(db_session_factory, BASE, slack, tenant, agents, "U1")
    other = await _explain(db_session_factory, BASE, slack, tenant, agents, "U2")

    assert auth == ["Bearer xoxb-1", "Bearer xoxb-1"], auth
    assert member.decision.route == DmStart("staff", "locked"), member.decision
    assert other.groups == frozenset(), other
    assert other.decision.route == DmChoose(("help", "staff")), "no rule matches: they pick"


async def test_explain_dm_on_slack_without_a_readable_bot_token_then_it_is_unchecked(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A token sealed with a key this deployment lacks cannot read the group."""
    tenant, agents = await _workspace(db_session_factory, platform="slack", workspace_id="T1")
    other = MultiFernet([Fernet(Fernet.generate_key())])
    async with db_session_factory.begin() as session:
        await upsert_slack_bot_token(
            session, team_id="T1", encrypted_token=encrypt_token(other, "xoxb-1")
        )

    def nothing(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request expected, got {request.url}")

    found = await _explain(db_session_factory, BASE, nothing, tenant, agents, "U1")

    assert (found.groups, found.decision.route) == (None, DmUnchecked()), found


async def test_explain_dm_on_teams_reads_the_team_roster_through_graph(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A Teams person's teams come from the roster, with the deployment's app token."""
    tenant, agents = await _workspace(db_session_factory, platform="teams", workspace_id=ENTRA)
    person = str(uuid.UUID(int=7))

    def graph(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "app"})
        assert request.headers["Authorization"] == "Bearer app", request.headers
        assert request.url.path == "/v1.0/teams/g-1/members", request.url
        return httpx.Response(200, json={"value": [{"userId": person, "roles": []}]})

    found = await _explain(db_session_factory, TEAMS, graph, tenant, agents, person.upper())

    assert found.decision.route == DmStart("staff", "locked"), found.decision
    assert found.groups == frozenset({"g-1"}), "Teams ids compare in lower case"


async def test_explain_dm_on_teams_without_the_app_then_it_is_unchecked(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Without Teams settings the roster cannot be read, so the rule cannot be checked."""
    tenant, agents = await _workspace(db_session_factory, platform="teams", workspace_id=ENTRA)

    def nothing(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request expected, got {request.url}")

    found = await _explain(db_session_factory, BASE, nothing, tenant, agents, "x")

    assert (found.groups, found.decision.route) == (None, DmUnchecked()), found
