"""Ordinary agent operation and configuration paths, using offline providers."""

from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import httpx
import pytest
from daimon.adapters.discord.bot import DaimonBot, admission_refusal_message
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agents import _fork_agent_impl  # pyright: ignore[reportPrivateUsage]
from daimon.core import agent_fork
from daimon.core.channel_backend import load_backend_agent, make_backend_service
from daimon.core.config import McpSettings, Settings, TurnSettings
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.provider_keys import DEFAULT_KEY_ENV
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import tenants
from daimon.core.stores.domain import AccountRow, TenantRow
from daimon.core.tenant_balance import is_over_balance
from daimon.testing.db import db_committing_engine as db_committing_engine
from daimon.testing.db import db_committing_pool as db_committing_pool
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.factories import make_platform_principal
from daimon.testing.ma_models import ma_agent
from daimon.testing.pytest_plugin import db_preflight as db_preflight
from fastmcp.exceptions import ToolError
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response
from sqlalchemy.ext.asyncio import async_sessionmaker

#: This deployment's settings, as the bot reads them on a join (defaults only).
SETTINGS = Settings(
    database={"url": "postgresql+asyncpg://fixture:fixture@localhost/fixture"},
    anthropic={"api_key": "test-fixture-key"},
    turn=TurnSettings(path="mux", channel_backends=True),
    mcp=McpSettings(public_url="https://mcp.invalid"),
)
#: Each channel the member uses: a fresh public channel and a private one.
CHANNELS = {
    "public": ("j8-public-channel", "j8-public-thread"),
    "private": ("j8-private-channel", "j8-private-thread"),
}


async def join(sm, settings: Settings = SETTINGS):
    """What `on_guild_join` does for a guild it has never seen, before its seed runs."""
    guild = SimpleNamespace(id=int(uuid4().int % 10**18))
    locks: dict[int, asyncio.Lock] = {}
    bot = SimpleNamespace(
        runtime=SimpleNamespace(sessionmaker=sm, settings=settings),
        get_guild=lambda guild_id: guild if guild_id == guild.id else None,
        _guild_lifecycle_lock_for=lambda guild_id: locks.setdefault(guild_id, asyncio.Lock()),
    )
    tenant_id = await DaimonBot._provision_joined_guild(bot, guild)  # pyright: ignore[reportPrivateUsage, reportArgumentType]
    assert tenant_id is not None
    return tenant_id


@pytest.fixture
def keyed(monkeypatch):
    """The deployment keys every provider (as staging and prod do)."""
    for provider in ("openai", "gemini"):
        monkeypatch.setenv(DEFAULT_KEY_ENV[provider], "offline")


@pytest.fixture
async def joined(db_preflight, db_committing_engine, keyed):
    sm = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    require_owned_sessionmaker(sm)
    tenant_id = await join(sm)
    # The join made the server's one account under its derived id.
    account_id = derive_guild_account_uuid(tenant_id)
    async with sm.begin() as db:
        # The member who speaks first: their principal is made on their first message.
        await make_platform_principal(
            db,
            tenant=cast(TenantRow, SimpleNamespace(id=tenant_id)),
            account=cast(AccountRow, SimpleNamespace(id=account_id)),
            platform="discord",
            external_id="requester",
        )
    # The background seed flips the server to ready once its defaults are reconciled.
    await tenants.set_provision_status(sm, tenant_id=tenant_id, status="ready")
    return sm, tenant_id, account_id


async def test_a_server_joined_without_signup_credit_is_refused_in_plain_words(
    db_preflight, db_committing_engine
):
    sm = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    require_owned_sessionmaker(sm)
    unfunded = SETTINGS.model_copy(
        update={"billing": SETTINGS.billing.model_copy(update={"signup_credit": Decimal(0)})}
    )
    tenant_id = await join(sm, unfunded)
    assert await is_over_balance(sessionmaker=sm, tenant_id=tenant_id)
    said = admission_refusal_message("balance_depleted", unfunded)
    assert said.startswith("Your team's Daimon credit has run out.")
    assert "/billing" in said


def _copy_router(tenant_id, created):
    """Anthropic's agents API with the new server's default Daimon agent on it."""
    source = ma_agent(
        id="ag_daimon",
        name="Daimon",
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "Daimon"},
    )

    def create(request: httpx.Request, _match: re.Match[str]) -> httpx.Response:
        import json

        payload = json.loads(request.content)
        created.append(payload)
        copy = ma_agent(
            id="ag_copy",
            name=payload["name"],
            model=payload["model"],
            system=payload.get("system"),
            metadata=payload.get("metadata"),
        )
        return httpx.Response(200, json=copy.model_dump(mode="json"))

    def read(_request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        if match.group(1) == "ag_copy" and created:
            payload = created[-1]
            copy = ma_agent(
                id="ag_copy",
                name=payload["name"],
                model=payload["model"],
                system=payload.get("system"),
                metadata=payload.get("metadata"),
            )
            return httpx.Response(200, json=copy.model_dump(mode="json"))
        return httpx.Response(200, json=source.model_dump(mode="json"))

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda *_: list_response([source.model_dump(mode="json")]))
    router.add("GET", r"/v1/agents/([^/]+)", read)
    router.add("POST", r"/v1/agents", create)
    return router


@pytest.mark.parametrize("channel", sorted(CHANNELS))
async def test_make_a_copy_of_yourself_on_gpt(joined, monkeypatch, channel):
    sm, tenant_id, account_id = joined
    monkeypatch.setattr(agent_fork, "queue_agent_face", lambda *args, **kwargs: None)
    created: list[dict] = []
    client = build_fake_anthropic(_copy_router(tenant_id, created).dispatch)
    runtime = McpRuntime(
        sm, client, SETTINGS, DeploymentDefault(agent_name="Daimon", environment_name="default")
    )

    def person(*, admin: bool) -> AuthIdentity:
        return AuthIdentity(
            tenant_id=tenant_id,
            account_id=account_id,
            role=Role.ADMIN if admin else Role.USER,
            is_admin=admin,
            platform="discord",
            platform_user_id="requester",
        )

    # A member can't make a saved copy: they're told who can, nothing is created.
    with pytest.raises(ToolError, match="requires a server admin"):
        await _fork_agent_impl(
            runtime, person(admin=False), "Daimon", "Daimon", new_copy_if_taken=True
        )
    assert created == []
    # The server's admin can: the copy is made, under a free name, owned by them.
    # (In chat the agent names the exact source it read with list_agents.)
    copy = await _fork_agent_impl(
        runtime,
        person(admin=True),
        "Daimon",
        "Daimon",
        expected_ma_agent_id="ag_daimon",
        new_copy_if_taken=True,
    )
    assert copy.name == "Daimon-2" and len(created) == 1
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores import agent_owners

    async with sm() as db:
        owner = await agent_owners.owner_of(
            db, tenant_id, derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ag_copy")
        )
    assert owner == account_id
    # ...and moving it to GPT is offered on this brand-new server's defaults.
    from daimon.core.mux_backend import resource_scope
    from daimon.core.mux_compat import retrieve_agent

    scope = resource_scope(tenant_id=str(tenant_id), account_id=str(account_id))
    native = await retrieve_agent(client, "ag_copy", scope=scope)
    agent = await load_backend_agent(
        client, sm, tenant_id=tenant_id, agent_name=copy.name, actual_agent=native
    )
    service = make_backend_service(sm, SETTINGS.turn)
    preview = await service.preview(agent, "openai")
    assert preview.allowed, preview
    assert preview.target_backend == "openai"
