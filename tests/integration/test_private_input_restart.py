"""Scenario: the value lands even when the process dies before it can dispatch.

A private-input form commits three facts in one transaction — the request is
consumed, the value is written, and the turn it owes is queued as a
`task_continuations` row — and only then spawns the follow-up turn
(`credential_modals._dispatch_origin_thread`). That ordering is the whole
crash-safety design: everything durable happens first, and the dispatch is a
best-effort kick that any later completed turn in the thread can redo.

These tests hold the adapter to it against a real Postgres:

- the submission is driven through the real `EnvCredentialModal.on_submit`
  with the spawn replaced by a process death. The value must be stored and
  the continuation must be sitting there `pending`, owed to nobody.
- recovery is then driven twice through the real
  `dispatch_pending_continuations` — the next completed turn, and a retry
  behind it — and must run the follow-up exactly once. `claim_continuation`
  is the gate; a third claim afterwards must still find nothing to take.
- a retried submission of an already-consumed request writes nothing and
  queues no second turn, so a person hammering a dead button cannot buy a
  second billed turn.

Everything platform-facing is a boundary mock (the Discord interaction, the
thread, `thread.history`); MA is faked at the transport with a real
`AsyncAnthropic` over `MARouter`. Discord alone is enough here: the queue,
the claim and the settle all live in `daimon.core`, and the Slack adapter's
own submission path is covered by its unit tests.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.discord import credential_modals as credential_modals_mod
from daimon.adapters.discord.continuation_dispatch import dispatch_pending_continuations
from daimon.adapters.discord.credential_modals import EnvCredentialModal
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.continuity.continuation import ContinuationDecision, claim_continuation
from daimon.core.credential_requests import mint_request_token
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_files import list_agent_files
from daimon.core.stores.credential_requests import create_credential_request
from daimon.core.stores.domain import CredentialRequestRow, TaskContinuationRow
from daimon.core.stores.task_continuations import get_continuation, list_pending_continuations
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_GUILD_ID = 770001
_USER_ID = "100000000000000077"
_MA_AGENT_ID = "agent_01RestartSafety"
_THREAD_ID = "880000"
_PARENT_CHANNEL_ID = "879999"
_POSTED_MESSAGE_ID = "881111"
_SECRET_VALUE = "sk-restart-safety-do-not-leak"
_REQUESTED_WORK = "finish the revenue chart now that the key is set"


def _router(tenant_id: uuid.UUID) -> MARouter:
    """Serve the one agent every lookup in this flow re-resolves.

    `find_agent_by_derived_uuid` (the submit-time target check) lists agents;
    `get_setup_agent` (the dispatch-time destination check) retrieves the
    exact id. Both must see the same live, tenant-owned agent.
    """
    agent = ma_agent(id=_MA_AGENT_ID, name="tester", metadata={"daimon_tenant": str(tenant_id)})
    router = MARouter()
    router.add_agent_list(agent)
    router.add_agent(agent)
    return router


def _runtime(sessionmaker: async_sessionmaker[AsyncSession], router: MARouter) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp.public_url = None
    settings.mcp.jwt_secret = None
    settings.crypto.keys = ()
    settings.github.oauth_scopes = ("repo", "read:user")
    return DiscordRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(  # pyright: ignore[reportArgumentType]  # this flow never runs a turn; the follow-up itself is injected
            fernet=build_multifernet((Fernet.generate_key().decode(),))
        ),
    )


async def _seed_env_request(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    target: str,
    requested_work: str | None = _REQUESTED_WORK,
) -> CredentialRequestRow:
    """A live `kind="env"` request carrying a card, an origin thread and a frozen target.

    The frozen `target_ma_agent_id` / `target_name` are what
    `build_input_continuation` needs to address a continuation at all; without
    them the form records no follow-up, which is a different (legacy-card)
    scenario.
    """
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(_GUILD_ID))
    async with sessionmaker() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=str(_GUILD_ID))
        account = await make_account(session, tenant=tenant)
        return await create_credential_request(
            session,
            token=mint_request_token(),
            kind="env",
            tenant_id=tenant_id,
            agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=_MA_AGENT_ID),
            account_id=account.id,
            target=target,
            replaces_updated_at=None,
            mcp_server_url=None,
            requester_platform_user_id=_USER_ID,
            channel_id=_THREAD_ID,
            platform="discord",
            parent_channel_id=_PARENT_CHANNEL_ID,
            origin_thread_id=_THREAD_ID,
            posted_message_id=_POSTED_MESSAGE_ID,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id=_MA_AGENT_ID,
            target_name="tester",
            requested_work=requested_work,
        )


def _interaction() -> MagicMock:
    """A modal submit from the original requester, on the request's own card."""
    interaction = MagicMock()
    interaction.guild_id = _GUILD_ID
    interaction.user.id = int(_USER_ID)
    interaction.type = discord.InteractionType.modal_submit
    interaction.message = None
    interaction.channel_id = int(_THREAD_ID)
    interaction.channel = MagicMock(spec=discord.Thread)
    interaction.channel.parent_id = int(_PARENT_CHANNEL_ID)
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.client.get_partial_messageable.return_value.get_partial_message.return_value.edit = AsyncMock()
    return interaction


def _origin_thread() -> MagicMock:
    """The thread the continuation is queued against, as the dispatcher sees it."""
    thread = MagicMock(spec=discord.Thread)
    thread.id = int(_THREAD_ID)
    thread.send = AsyncMock()
    return thread


async def _submit(runtime: DiscordRuntime, row: CredentialRequestRow, *, value: str) -> MagicMock:
    modal = EnvCredentialModal(runtime=runtime, request_row=row)
    modal.value_input._value = value  # pyright: ignore[reportPrivateUsage]  # the modal's own TextInput has no public setter
    interaction = _interaction()
    await modal.on_submit(interaction)
    return interaction


async def _may_post_open() -> bool:
    """No access policy: the agent may post in the thread."""
    return True


async def test_a_crash_between_the_commit_and_the_dispatch_leaves_the_turn_recoverable(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _seed_env_request(db_session_factory, target="OPENAI_API_KEY")
    runtime = _runtime(db_session_factory, _router(row.tenant_id))

    # The process dies at the exact moment the form tries to kick the
    # follow-up: everything before this point has already committed.
    monkeypatch.setattr(
        credential_modals_mod,
        "_dispatch_origin_thread",
        AsyncMock(side_effect=RuntimeError("adapter process died before dispatch")),
    )
    with pytest.raises(RuntimeError, match="died before dispatch"):
        await _submit(runtime, row, value=_SECRET_VALUE)

    async with db_session_factory() as session:
        files = await list_agent_files(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert [(file.key, file.content) for file in files] == [("OPENAI_API_KEY", _SECRET_VALUE)], (
        "the value must be durable before anything is dispatched -- a crash after the "
        "commit may lose the turn, never the secret"
    )

    async with db_session_factory() as session:
        queued = await get_continuation(session, idempotency_key=row.idempotency_key)
    assert queued is not None, "the write's transaction must also have queued the turn it owes"
    assert queued.status == "pending", (
        f"nothing dispatched it, so it must still be claimable; got {queued.status}"
    )
    assert queued.reason == "private_input_applied", (
        "the queued row must be a private-input continuation, not a handoff"
    )
    assert queued.requested_work == _REQUESTED_WORK, (
        "the queued turn must carry the work the card promised to resume"
    )

    # Recovery: the next turn to finish in this thread, then a retry behind
    # it. Both go through the real dispatcher; the follow-up turn itself is
    # the injected seam, so nothing here is billed.
    calls: list[tuple[TaskContinuationRow, ContinuationDecision]] = []

    async def _run_follow_up(
        dispatched: TaskContinuationRow, decision: ContinuationDecision
    ) -> None:
        calls.append((dispatched, decision))

    thread = _origin_thread()
    monkeypatch.setattr(
        "daimon.adapters.discord.continuation_dispatch._latest_human_message_at",
        AsyncMock(return_value=None),
    )
    for _attempt in range(2):
        await dispatch_pending_continuations(
            db_session_factory,
            runtime.anthropic,
            tenant_id=row.tenant_id,
            thread=thread,
            run_follow_up=_run_follow_up,
            may_post=_may_post_open,
        )

    assert len(calls) == 1, (
        f"two dispatch passes over one pending row must run the follow-up exactly once, "
        f"ran it {len(calls)} times"
    )
    dispatched_row, decision = calls[0]
    assert dispatched_row.idempotency_key == row.idempotency_key, (
        "the follow-up must run the row the form queued, not some other pending turn"
    )
    assert decision.seed_user_message == _REQUESTED_WORK, (
        "the recovered turn is seeded with the person's own words, not a synthesized prompt"
    )

    async with db_session_factory() as session:
        settled = await get_continuation(session, idempotency_key=row.idempotency_key)
    assert settled is not None and settled.status == "delivered", (
        "a follow-up that returned without raising must settle the row delivered"
    )

    claimed_again = await claim_continuation(
        db_session_factory, idempotency_key=row.idempotency_key, now=datetime.now(UTC)
    )
    assert claimed_again is False, (
        "a settled row must never be claimable again -- the conditional UPDATE is the "
        "whole at-most-once guarantee across restarts"
    )
    assert thread.send.await_count == 0, (
        "a delivered continuation posts no skip copy into the thread"
    )


async def test_retried_submission_of_a_consumed_request_writes_nothing_and_records_no_second_continuation(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_env_request(db_session_factory, target="STRIPE_KEY")
    runtime = _runtime(db_session_factory, _router(row.tenant_id))

    await _submit(runtime, row, value="first-value")
    second = await _submit(runtime, row, value="second-value")

    async with db_session_factory() as session:
        files = await list_agent_files(session, tenant_id=row.tenant_id, agent_id=row.agent_id)
    assert [(file.key, file.content) for file in files] == [("STRIPE_KEY", "first-value")], (
        "the request is single-use: the retry must not overwrite the value that landed"
    )

    async with db_session_factory() as session:
        pending = await list_pending_continuations(
            session, tenant_id=row.tenant_id, platform="discord", thread_id=_THREAD_ID
        )
    assert len(pending) == 1, (
        f"one consumed request owes exactly one turn; the retry must queue none, got {len(pending)}"
    )
    assert pending[0].idempotency_key == row.idempotency_key, (
        "the one queued turn is the one the consuming submission recorded"
    )

    retry_message: Any = second.followup.send.call_args.args[0]
    assert "no longer valid" in retry_message, (
        "the retry must tell the person the request is spent rather than silently no-op"
    )
