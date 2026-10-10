"""Tests for live setup-panel write helpers."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from daimon.adapters.discord.agent_setup import write as write_mod
from daimon.adapters.discord.agent_setup.write import (
    create_blank_agent,
)
from daimon.adapters.discord.errors import render_error
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.errors import AgentNameCollision
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.specs import AgentSpec
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_stub_anthropic
from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _agent_dict(
    *,
    id_: str,
    name: str,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    managed: bool = False,
) -> dict[str, Any]:
    """Build a real BetaManagedAgentsAgent and dump to JSON for the MockTransport.

    Inlined (no factory) per testing skill — every call site shows what it
    constructs, and SDK drift breaks the test loudly.
    """
    metadata: dict[str, str] = {
        "daimon_tenant": str(tenant_id),
        "daimon_name": name,
    }
    if account_id is not None:
        metadata["daimon_account"] = str(account_id)
    if managed:
        metadata["daimon_managed"] = "true"
    return ma_agent(
        id=id_,
        name=name,
        metadata=metadata,
    ).model_dump(mode="json")


@pytest.mark.asyncio
async def test_load_agent_inline_pat_returns_none_when_crypto_unconfigured() -> None:
    """No crypto keys -> no inline PAT could exist; the sessionmaker must never
    be touched (calling _build_runtime_fernet unconditionally would raise)."""
    agent_id = uuid.uuid4()
    settings = MagicMock()
    settings.crypto.keys = ()

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("sessionmaker must not be called when crypto is unconfigured")

    runtime = DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=_boom,  # type: ignore[arg-type]  # deliberately wrong shape; must never be invoked
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )

    result = await write_mod.load_agent_inline_pat(runtime, agent_id=agent_id)
    assert result is None, "no crypto keys configured -> no inline PAT can exist"


@pytest.mark.asyncio
async def test_load_agent_inline_pat_returns_stored_pat(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Round-trips through store_inline_pat -> load_agent_inline_pat, decrypted."""
    await make_tenant(db_session, platform="discord", workspace_id="test-guild-inline-pat-load")

    fernet_key = Fernet.generate_key().decode()
    plaintext = "ghp_inline_pat_load_xxxx9999"

    settings = MagicMock()
    settings.crypto.keys = (MagicMock(get_secret_value=lambda: fernet_key),)
    settings.github.oauth_scopes = ("repo", "read:user")
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=build_stub_anthropic(),
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )

    agent_id = uuid.uuid4()
    await write_mod.store_inline_pat(
        runtime,
        account_id=uuid.uuid4(),
        agent_id=agent_id,
        plaintext_pat=plaintext,
    )

    result = await write_mod.load_agent_inline_pat(runtime, agent_id=agent_id)
    assert result == plaintext, "load_agent_inline_pat must decrypt and return the exact stored PAT"


def _runtime_with_settings(
    anthropic: Any, *, tenant_id: uuid.UUID, public_url: HttpUrl | None
) -> DiscordRuntime:
    """Build a DiscordRuntime carrying just the bits write.py touches."""
    _ = tenant_id  # runtime no longer carries tenant_id; threaded into helpers
    settings = MagicMock()
    settings.mcp.public_url = public_url
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=MagicMock(),
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
    )


async def test_create_blank_agent_is_not_marked_managed(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """A panel-created blank agent must NOT be stamped daimon_managed=true.

    SC-2: the caller (panel.py NewAgentModal) passes the guild account; this
    test verifies create_blank_agent forwards whatever account_id is given and
    never flips managed=True (which would make it sweep-eligible, archiving it
    on the next defaults apply).
    """
    captured: dict[str, Any] = {}

    # Use a distinct guild account to verify the stamp is forwarded as-is.
    guild_account = uuid.UUID("00000000-0000-0000-0000-000000002222")
    assert guild_account != account_id, (
        "test setup: guild account must differ from personal account"
    )

    async def spy_reconcile(
        client: Any,
        spec: AgentSpec,
        *,
        tenant_id: uuid.UUID,
        dry_run: bool,
        account_id: uuid.UUID | None = None,
        public_url: str | None = None,
        managed: bool = True,
    ) -> Any:
        captured["managed"] = managed
        captured["account_id"] = account_id
        return MagicMock()

    monkeypatch.setattr(write_mod, "reconcile_agent", spy_reconcile)

    # Stub find_agents_by_daimon_tag to return empty (no collision).
    async def _no_collision(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(write_mod, "find_agents_by_daimon_tag", _no_collision)

    runtime = _runtime_with_settings(build_stub_anthropic(), tenant_id=tenant_id, public_url=None)

    await create_blank_agent(
        runtime,
        tenant_id=tenant_id,
        name="data scientist",
        system="be helpful",
        model="claude-sonnet-4-6",
        account_id=guild_account,  # SC-2: caller (panel.py) supplies the guild account
    )

    assert captured["managed"] is False, (
        "create_blank_agent makes a guild-owned agent — managed=True would mark it "
        "sweep-eligible, so the next deploy's defaults apply archives it"
    )
    assert captured["account_id"] == guild_account, (
        "SC-2: create_blank_agent must forward the guild account stamp it receives; "
        "passing the personal account would be a regression"
    )
    assert captured["account_id"] != account_id, (
        "SC-2: the personal account must not be the stamp — regression guard"
    )


async def test_create_blank_agent_rejects_duplicate_tenant_name(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Plan-01 create-path guard: if an agent with the same name already exists
    under the guild account, create_blank_agent raises DaimonError (SC-2 + collision
    decision option (a))."""

    guild_account = uuid.UUID("00000000-0000-0000-0000-000000003333")

    # Simulate a pre-existing agent owned by the guild account with the same name.
    existing_meta = {
        "daimon_tenant": str(tenant_id),
        "daimon_name": "existing-agent",
        "daimon_account": str(guild_account),
    }
    existing_agent = ma_agent(
        id="ag_existing",
        name="existing-agent",
        metadata=existing_meta,
    )

    async def _collision_found(*args: Any, **kwargs: Any) -> list[Any]:
        return [existing_agent]

    monkeypatch.setattr(write_mod, "find_agents_by_daimon_tag", _collision_found)

    runtime = _runtime_with_settings(build_stub_anthropic(), tenant_id=tenant_id, public_url=None)

    with pytest.raises(AgentNameCollision, match="existing-agent") as caught:
        await create_blank_agent(
            runtime,
            tenant_id=tenant_id,
            name="existing-agent",
            system="be helpful",
            model="claude-sonnet-4-6",
            account_id=guild_account,
        )

    assert render_error(caught.value, request_id="private-rid") == (
        "This workspace already has an agent with that name. Pick a different name."
        "\n\n-# Ref TE-RID"
    )


async def test_create_blank_agent_rejects_name_held_by_other_owner(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """create_blank_agent rejects a name that exists under a DIFFERENT owner.

    Any non-archived same-name agent in the tenant blocks creation regardless of
    who owns it. Zero MA write calls must fire (collision is detected before reconcile).
    """

    other_account = uuid.UUID("00000000-0000-0000-0000-0000000000ff")

    # Agent stamped with a different account — NOT the caller's account_id.
    existing_agent = ma_agent(
        id="ag_other_owner",
        name="taken-name",
        tenant_id=tenant_id,
        metadata={"daimon_account": str(other_account)},  # different owner
    )

    async def _collision_found(*args: Any, **kwargs: Any) -> list[Any]:
        return [existing_agent]

    reconcile_calls: list[Any] = []

    async def _spy_reconcile(*args: Any, **kwargs: Any) -> Any:
        reconcile_calls.append(args)
        return MagicMock()

    monkeypatch.setattr(write_mod, "find_agents_by_daimon_tag", _collision_found)
    monkeypatch.setattr(write_mod, "reconcile_agent", _spy_reconcile)

    runtime = _runtime_with_settings(build_stub_anthropic(), tenant_id=tenant_id, public_url=None)

    with pytest.raises(AgentNameCollision, match="taken-name") as caught:
        await create_blank_agent(
            runtime,
            tenant_id=tenant_id,
            name="taken-name",
            system=None,
            model="claude-sonnet-4-6",
            account_id=account_id,  # caller's own account; other_account owns the collision
        )

    assert render_error(caught.value, request_id="private-rid") == (
        "This workspace already has an agent with that name. Pick a different name."
        "\n\n-# Ref TE-RID"
    )
    assert reconcile_calls == [], (
        "create must raise before reconcile when another owner holds the name"
    )


@pytest.mark.asyncio
async def test_create_blank_agent_queues_the_new_agents_face(
    monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    async def reconcile(*_args: Any, **_kwargs: Any) -> Any:
        outcome = MagicMock()
        outcome.anthropic_id = "ag_new"
        return outcome

    async def no_collision(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    queued: list[tuple[uuid.UUID, str]] = []
    monkeypatch.setattr(write_mod, "reconcile_agent", reconcile)
    monkeypatch.setattr(write_mod, "find_agents_by_daimon_tag", no_collision)
    monkeypatch.setattr(
        write_mod,
        "queue_agent_face",
        lambda _factory, *, tenant_id, agent_name, **_: queued.append((tenant_id, agent_name)),
    )
    runtime = _runtime_with_settings(build_stub_anthropic(), tenant_id=tenant_id, public_url=None)

    await create_blank_agent(
        runtime,
        tenant_id=tenant_id,
        name="Atlas Birch",
        system=None,
        model="claude-sonnet-4-6",
        account_id=account_id,
    )

    assert queued == [(tenant_id, "Atlas Birch")]
