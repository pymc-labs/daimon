"""WatchCredentialModal: the requester's own watch key, stored only for them.

Requester-only at submit, encrypted at rest, keyed by the row's tenant,
requester account and exact server URL, and never echoed or logged.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import structlog
from cryptography.fernet import Fernet
from daimon.adapters.discord.credential_modals import WatchCredentialModal
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.credential_requests import mint_request_token
from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.credential_requests import (
    create_credential_request,
    peek_credential_request,
)
from daimon.core.stores.domain import CredentialRequestRow
from daimon.core.stores.watch_credentials import load_watch_credential
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.turn_fakes import deps_for
from mux.testing.drivers.anthropic.ma import build_stub_anthropic
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_GUILD_ID = 111
_REQUESTER = 100000000000000001
_KEY = "watch-owner-key-sentinel-do-not-leak-" + "k" * 24
_URL = "https://tools.example.com/mcp"


def _runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> DiscordRuntime:
    return DiscordRuntime(
        settings=MagicMock(),
        anthropic=build_stub_anthropic(),
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=replace(
            deps_for(None, None),
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
            turn_path=None,
        ),
    )


async def _seed(sessionmaker: async_sessionmaker[AsyncSession]) -> CredentialRequestRow:
    async with sessionmaker() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(_GUILD_ID))
        account = await make_account(session, tenant=tenant)
        return await create_credential_request(
            session,
            token=mint_request_token(),
            kind="watch_credential",
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent-bound"),
            account_id=account.id,
            target="tools.example.com",
            mcp_server_url=_URL,
            requester_platform_user_id=str(_REQUESTER),
            channel_id="333",
            platform="discord",
            parent_channel_id="222",
            origin_thread_id="333",
            posted_message_id="444",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id="agent-bound",
            target_name="analyst",
            requested_work=None,
        )


def _interaction(user_id: int = _REQUESTER) -> MagicMock:
    interaction = MagicMock()
    interaction.guild_id = _GUILD_ID
    interaction.user.id = user_id
    interaction.type = discord.InteractionType.modal_submit
    interaction.message = None
    interaction.channel_id = 333
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.parent_id = 222
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    card = interaction.client.get_partial_messageable.return_value.get_partial_message
    card.return_value.edit = AsyncMock()
    return interaction


def _every_reply(interaction: MagicMock) -> str:
    card = interaction.client.get_partial_messageable.return_value.get_partial_message
    calls: list[Any] = [*interaction.followup.send.call_args_list]
    calls += card.return_value.edit.call_args_list
    texts = [
        item.content
        for call in calls
        if isinstance(call.kwargs.get("view"), discord.ui.LayoutView)
        for item in call.kwargs["view"].walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    ]
    return repr([(call.args, call.kwargs) for call in calls]) + "\n".join(texts)


async def test_the_requesters_key_is_stored_encrypted_for_them_only(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory)
    runtime = _runtime(db_session_factory)
    modal = WatchCredentialModal(runtime=runtime, request_row=row)
    modal.key_input._value = _KEY  # pyright: ignore[reportPrivateUsage]
    interaction = _interaction()

    with structlog.testing.capture_logs() as logs:
        await modal.on_submit(interaction)

    assert runtime.turn_deps.fernet is not None
    async with db_session_factory() as session:
        sealed = await load_watch_credential(
            session, tenant_id=row.tenant_id, owner_account_id=row.account_id, server_url=_URL
        )
        assert sealed is not None and decrypt_token(runtime.turn_deps.fernet, sealed) == _KEY
        dumped = await session.scalar(
            text("SELECT string_agg(row_to_json(c)::text, '') FROM watch_credentials c")
        )
        spent = await peek_credential_request(session, token=row.token)
    assert _KEY not in str(dumped), "only ciphertext is stored"
    assert spent is not None and spent.used_at is not None and spent.outcome == "applied"
    assert _KEY not in repr(logs) and _KEY not in _every_reply(interaction)
    assert "Only your own watches use it" in _every_reply(interaction)


async def test_someone_else_cannot_submit_the_requesters_form(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory)
    modal = WatchCredentialModal(runtime=_runtime(db_session_factory), request_row=row)
    modal.key_input._value = _KEY  # pyright: ignore[reportPrivateUsage]
    interaction = _interaction(user_id=_REQUESTER + 1)

    await modal.on_submit(interaction)

    async with db_session_factory() as session:
        stored = await session.scalar(text("SELECT count(*) FROM watch_credentials"))
        live = await peek_credential_request(session, token=row.token)
    assert stored == 0
    assert live is not None and live.used_at is None, "the requester's form is still usable"
    assert _KEY not in _every_reply(interaction)


async def test_a_spent_form_stores_nothing_twice(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed(db_session_factory)
    runtime = _runtime(db_session_factory)
    for value in (_KEY, _KEY + "-second"):
        modal = WatchCredentialModal(runtime=runtime, request_row=row)
        modal.key_input._value = value  # pyright: ignore[reportPrivateUsage]
        await modal.on_submit(_interaction())
    assert runtime.turn_deps.fernet is not None
    async with db_session_factory() as session:
        sealed = await load_watch_credential(
            session, tenant_id=row.tenant_id, owner_account_id=row.account_id, server_url=_URL
        )
    assert sealed is not None and decrypt_token(runtime.turn_deps.fernet, sealed) == _KEY
    assert derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID)) == row.tenant_id
