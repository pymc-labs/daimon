"""Tests for `DaimonBot`'s continuation-dispatch entry points.

`dispatch_continuations_in_thread` is the entry a caller OUTSIDE a turn uses
(a private-form submission, say); it takes the same per-thread `_processing`
guard a mention takes, then delegates to `_dispatch_continuations`, which the
turn tail calls directly because it already owns the guard.

`_run_continuation_turn` is exercised with `bind_session` /
`run_prepared_turn` patched at the names `bot.py` imports (the precedent in
`test_continuity_orchestration.py`) so the assertion is about the turn
controls the follow-up runs with, not the session-preparation pipeline.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime, build_turn_deps
from daimon.core.config import McpSettings, ThreadNamingSettings
from daimon.core.continuity.continuation import ContinuationDecision, ContinuationRequest
from daimon.core.continuity.wakes import enqueue_wake, poll_wakes_once
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.stores import tenant_ledger
from daimon.core.stores.accounts import get_account, set_role
from daimon.core.stores.domain import ContinuationReason, Role, TaskContinuationRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.task_continuations import (
    get_continuation,
    list_pending_continuations,
    record_continuation,
)
from daimon.core.turn.prepare import ContinuityOutcome, PreparedTurn
from daimon.core.turn.run import RunOutcome
from daimon.core.turn.state import TurnState
from daimon.testing import ma_agent, ma_environment
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import make_bot

_THREAD_ID = 4242


class _AsyncIter:
    def __init__(self, items: list[object]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> object:
        try:
            return next(self._items)
        except StopIteration as err:
            raise StopAsyncIteration from err


def _make_guild(
    *,
    member: object = None,
    fetch_error: Exception | None = None,
    cached_member: object = None,
) -> MagicMock:
    """A guild whose `fetch_member` returns `member` for the requester.

    With no `member`, `fetch_member` raises `fetch_error` -- by default
    Discord's "unknown member" (the requester left). `cached_member` is what
    the member cache (`get_member`) would say, which may be stale.
    """
    guild = MagicMock(spec=discord.Guild)
    guild.owner_id = 1
    guild.get_member = MagicMock(return_value=cached_member)
    if member is not None:
        guild.fetch_member = AsyncMock(return_value=member)
    else:
        guild.fetch_member = AsyncMock(
            side_effect=fetch_error
            or discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Member")
        )
    return guild


def _make_member(*, admin: bool) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = 555
    member.guild_permissions.administrator = admin
    member.guild_permissions.manage_guild = False
    return member


def _make_thread(*, thread_id: int = _THREAD_ID, guild: MagicMock | None = None) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.guild = guild if guild is not None else _make_guild()
    thread.parent_id = thread_id - 1
    thread.history = MagicMock(return_value=_AsyncIter([]))
    message_ref = MagicMock()
    message_ref.id = 42
    message_ref.edit = AsyncMock()
    message_ref.delete = AsyncMock()
    thread.send = AsyncMock(return_value=message_ref)
    return thread


def _make_runtime(
    sessionmaker: async_sessionmaker[AsyncSession], *, agent: object = None
) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp = McpSettings()
    settings.billing.markup = Decimal("1.0")
    settings.billing.signup_credit = Decimal("0")
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = 100
    discord_settings.per_caller_thread_sessions = True
    settings.discord = discord_settings
    settings.thread_naming = ThreadNamingSettings(enabled=False)
    anthropic = AsyncMock()
    anthropic.beta.agents.retrieve = AsyncMock(return_value=ma_agent())
    if agent is not None:
        anthropic.beta.agents.retrieve.return_value = agent
    anthropic.beta.environments.retrieve = AsyncMock(return_value=ma_environment())
    anthropic.beta.agents.list = MagicMock(return_value=_AsyncIter([]))
    resolver_cache = new_resolver_cache()
    deployment_default = DeploymentDefault()
    return DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
        turn_deps=build_turn_deps(
            settings,
            anthropic,
            sessionmaker,
            deployment_default=deployment_default,
            resolver_cache=resolver_cache,
            billing_config=None,
        ),
    )


async def _seed_pending_row(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_id: str,
    requested_work: str | None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed one pending continuation for `_THREAD_ID`; return (tenant_id, key)."""
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=workspace_id)
        account = await make_account(session, tenant=tenant)
        idempotency_key = uuid.uuid4()
        await record_continuation(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id=str(_THREAD_ID - 1),
            thread_id=str(_THREAD_ID),
            requester_account_id=account.id,
            requester_external_user_id="555",
            target_ma_agent_id="ag_target",
            target_name="target-agent",
            reason="task_handoff",
            idempotency_key=idempotency_key,
            requested_work=requested_work,
        )
    return tenant.id, idempotency_key


async def test_dispatch_continuations_in_thread_skips_a_thread_already_processing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A thread with a turn in flight is left alone, guard and row untouched.

    The running turn reaches `_dispatch_continuations` at its own tail, so the
    row is not dropped -- and the guard this call did not take must not be
    released by it either.
    """
    tenant_id, key = await _seed_pending_row(
        db_session_factory, workspace_id="710000001", requested_work="pick up the report"
    )
    bot = make_bot(_make_runtime(db_session_factory))
    thread = _make_thread()
    bot._processing.add(thread.id)  # pyright: ignore[reportPrivateUsage]

    await bot.dispatch_continuations_in_thread(
        tenant_id=tenant_id, thread=thread, guild_id="710000001"
    )

    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=key)
        pending = await list_pending_continuations(
            session, tenant_id=tenant_id, platform="discord", thread_id=str(_THREAD_ID)
        )
    assert row is not None, "the seeded continuation should still exist"
    assert row.status == "pending", (
        "a thread already processing must not have its continuation claimed"
    )
    assert len(pending) == 1, (
        f"the row must stay pending for the running turn's tail, got {pending}"
    )
    assert thread.id in bot._processing, (  # pyright: ignore[reportPrivateUsage]
        "the guard belongs to the turn that took it; a skipped call must not release it"
    )


async def test_dispatch_continuations_in_thread_releases_the_guard_after_dispatch(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The guard is taken for the dispatch and released once it is done.

    A save-only continuation (`requested_work=None`) settles `skip_save_only`
    without running a turn, which is enough to prove the dispatch really ran
    inside the guard rather than being skipped by it.
    """
    tenant_id, key = await _seed_pending_row(
        db_session_factory, workspace_id="710000002", requested_work=None
    )
    bot = make_bot(_make_runtime(db_session_factory))
    thread = _make_thread()

    await bot.dispatch_continuations_in_thread(
        tenant_id=tenant_id, thread=thread, guild_id="710000002"
    )

    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None, "the seeded continuation should still exist"
    assert row.status == "skipped", "the dispatch must have run and settled the row"
    assert row.skip_reason == "skip_save_only", f"unexpected skip reason {row.skip_reason}"
    assert thread.id not in bot._processing, (  # pyright: ignore[reportPrivateUsage]
        "the guard must be released once the dispatch finishes"
    )


async def test_dispatch_continuations_in_thread_releases_the_guard_when_dispatch_raises(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A dispatch that blows up must not strand the thread's guard forever."""
    bot = make_bot(_make_runtime(db_session_factory))
    thread = _make_thread()
    tenant_id = uuid.uuid4()

    with (
        patch(
            "daimon.adapters.discord.bot.dispatch_pending_continuations",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(RuntimeError),
    ):
        await bot.dispatch_continuations_in_thread(
            tenant_id=tenant_id, thread=thread, guild_id="710000003"
        )

    assert thread.id not in bot._processing, (  # pyright: ignore[reportPrivateUsage]
        "a raising dispatch must still release the guard"
    )


def _make_continuation_row(
    *, tenant_id: uuid.UUID, account_id: uuid.UUID, reason: ContinuationReason
) -> TaskContinuationRow:
    now = datetime.now(UTC)
    return TaskContinuationRow(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        platform="discord",
        thread_id=str(_THREAD_ID),
        parent_channel_id=str(_THREAD_ID - 1),
        requester_account_id=account_id,
        requester_external_user_id="555",
        target_ma_agent_id="ag_target",
        target_name="target-agent",
        requested_work="finish the migration",
        reason=reason,
        status="claimed",
        skip_reason=None,
        idempotency_key=uuid.uuid4(),
        created_at=now,
        claimed_at=now,
        delivered_at=None,
    )


def _make_prepared_turn(*, account_id: uuid.UUID) -> PreparedTurn:
    from daimon.core.turn.admission import Admission

    async def _noop_recorder(*, event: object) -> None:
        return None

    return PreparedTurn(
        admission=Admission(
            account_id=account_id,
            agent=ma_agent(),
            environment=ma_environment(),
            config=ResolvedConfig(
                agent_name="test-agent",
                agent_name_tier="tenant",
                environment_name="test-env",
                environment_name_tier="tenant",
            ),
        ),
        ma_session_id="sess_test",
        mapping_id=None,
        watermark=None,
        reused=True,
        session_account_id=account_id,
        _record=_noop_recorder,
        continuity=ContinuityOutcome(state="continued", transfer_kind="none"),
    )


async def _run_continuation_and_capture_controls(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_id: str,
    reason: ContinuationReason,
) -> str:
    """Run one continuation turn and return the `user_message` it ran with."""
    controls, _ = await _run_continuation(
        db_session_factory, workspace_id=workspace_id, reason=reason
    )
    return controls


async def _run_continuation(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_id: str,
    reason: ContinuationReason,
    guild: MagicMock | None = None,
    prior_role: Role | None = None,
) -> tuple[str, uuid.UUID]:
    """Run one continuation turn; return its `user_message` and the tenant id.

    `prior_role` pre-stamps the requester's account as a previous turn would
    have, so a test can show the continuation re-derives the role rather than
    inheriting it.
    """
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=workspace_id)
        await tenant_ledger.insert_entry(
            session,
            tenant_id=tenant.id,
            delta_usd=Decimal("100.00"),
            reason="trial",
            idempotency_key=f"trial:{tenant.id}",
        )
        account = await make_account(session, tenant=tenant)
        if prior_role is not None:
            principal = await get_or_create_platform_principal(
                session, tenant_id=tenant.id, platform="discord", external_id="555"
            )
            await set_role(session, principal.account_id, prior_role)

    bot = make_bot(_make_runtime(db_session_factory))
    thread = _make_thread(guild=guild)
    row = _make_continuation_row(tenant_id=tenant.id, account_id=account.id, reason=reason)
    decision = ContinuationDecision(action="dispatch", seed_user_message="finish the migration")

    with (
        patch(
            "daimon.core.turn.admission.resolve_config", new_callable=AsyncMock
        ) as resolve_config,
        patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock) as resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as resolve_env,
        patch("daimon.adapters.discord.bot.bind_session", new_callable=AsyncMock) as bind,
        patch("daimon.adapters.discord.bot.run_prepared_turn", new_callable=AsyncMock) as run_turn,
    ):
        resolve_config.return_value = ResolvedConfig(
            agent_name="test-agent",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        resolve_agent.return_value = "ag_test"
        resolve_env.return_value = "env_test"
        bind.return_value = _make_prepared_turn(account_id=account.id)
        run_turn.return_value = RunOutcome(
            state=TurnState(),
            ma_session_id="sess_test",
            mapping_id=None,
            recovered=False,
        )
        await bot._run_continuation_turn(  # pyright: ignore[reportPrivateUsage]
            row, decision, thread=thread, tenant_id=tenant.id, guild_id=workspace_id
        )

    assert run_turn.await_args is not None, "the follow-up turn should have run"
    user_message = run_turn.await_args.kwargs["user_message"]
    assert isinstance(user_message, str), "user_message should be the rendered controls + seed"
    return user_message, tenant.id


async def test_continuation_turn_omits_handoff_notice_for_private_input(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`private_input_applied` re-runs the SAME agent, so there is no handoff.

    A `task_handoff` row still gets the notice -- the suppression is keyed on
    the row's `reason`, not on the dispatch path.
    """
    private_controls = await _run_continuation_and_capture_controls(
        db_session_factory, workspace_id="710000004", reason="private_input_applied"
    )
    assert '"handoff"' not in private_controls, (
        f"a private-input continuation must carry no handoff block, got {private_controls}"
    )
    assert "your first reply must show you have the task" not in private_controls, (
        "the handoff instruction paragraph must be absent with no handoff block"
    )
    assert private_controls.endswith("finish the migration"), (
        "the requester's own words still seed the turn"
    )

    handoff_controls = await _run_continuation_and_capture_controls(
        db_session_factory, workspace_id="710000005", reason="task_handoff"
    )
    assert '"handoff"' in handoff_controls, (
        f"a task handoff must still carry the one-time notice, got {handoff_controls}"
    )


async def _requester_account_role(
    db_session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> Role:
    async with db_session_factory() as session, session.begin():
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform="discord", external_id="555"
        )
        account = await get_account(session, principal.account_id)
    assert account is not None, "admit must have resolved the requester's account"
    return account.role


async def test_continuation_turn_runs_as_admin_when_requester_is_a_guild_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A private form an admin submitted resumes as admin.

    The continuation used to hardcode USER, so an admin's setup run lost its
    admin tools on the turn that applied their answer and stopped there.
    """
    controls, tenant_id = await _run_continuation(
        db_session_factory,
        workspace_id="710000010",
        reason="private_input_applied",
        guild=_make_guild(member=_make_member(admin=True)),
    )
    assert '"current_role": "admin"' in controls, (
        f"an admin requester's continuation must carry the admin role, got {controls}"
    )
    assert await _requester_account_role(db_session_factory, tenant_id) == Role.ADMIN, (
        "the live-role gate the resumed turn's MCP calls read must say admin"
    )


@pytest.mark.parametrize(
    ("workspace_id", "guild"),
    [
        pytest.param(
            "710000020", _make_guild(member=_make_member(admin=False)), id="non-admin-member"
        ),
        pytest.param("710000021", _make_guild(), id="requester-left-the-guild"),
        pytest.param(
            "710000023",
            _make_guild(member=_make_member(admin=False), cached_member=_make_member(admin=True)),
            id="stale-cached-admin-lost-the-role",
        ),
        pytest.param(
            "710000024",
            _make_guild(cached_member=_make_member(admin=True)),
            id="stale-cached-admin-left-the-guild",
        ),
        pytest.param(
            "710000022",
            _make_guild(fetch_error=discord.HTTPException(MagicMock(status=503), "unavailable")),
            id="discord-lookup-failed",
        ),
    ],
)
async def test_continuation_turn_never_runs_as_admin_without_a_live_admin_requester(
    db_session_factory: async_sessionmaker[AsyncSession], workspace_id: str, guild: MagicMock
) -> None:
    """A non-admin's form never produces an admin continuation -- even when
    the requester's account was admin on an earlier turn."""
    controls, tenant_id = await _run_continuation(
        db_session_factory,
        workspace_id=workspace_id,
        reason="private_input_applied",
        guild=guild,
        prior_role=Role.ADMIN,
    )
    assert '"current_role": "user"' in controls, f"continuation must run as user, got {controls}"
    assert await _requester_account_role(db_session_factory, tenant_id) == Role.USER, (
        "a stale admin stamp must not survive into the continuation"
    )


async def test_dispatch_skipped_while_processing_runs_when_the_thread_is_released(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A form submitted after the owner's own dispatch is not stranded.

    An owner holds the thread's `_processing` slot and has already run its
    dispatch (the turn tail) when the credential modal's dispatch arrives, so
    that call is skipped. When the owner releases the thread, the skipped
    dispatch must run (formal/thread_queue `FormDuringTail`), not wait for
    the next message in the thread.
    """
    tenant_id, key = await _seed_pending_row(
        db_session_factory, workspace_id="710000006", requested_work=None
    )
    bot = make_bot(_make_runtime(db_session_factory))
    thread = _make_thread()
    real_dispatch = bot._dispatch_continuations  # pyright: ignore[reportPrivateUsage]
    calls = 0

    async def _owner_tail_then_form(**kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            # The owner's tail dispatch has run; the form lands now.
            await bot.dispatch_continuations_in_thread(
                tenant_id=tenant_id, thread=thread, guild_id="710000006"
            )
            return
        await real_dispatch(**kwargs)  # pyright: ignore[reportArgumentType]

    with patch.object(bot, "_dispatch_continuations", side_effect=_owner_tail_then_form):
        await bot.dispatch_continuations_in_thread(
            tenant_id=tenant_id, thread=thread, guild_id="710000006"
        )
        for task in list(bot._bg_tasks):  # pyright: ignore[reportPrivateUsage]
            await task

    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None
    assert row.status == "skipped" and row.skip_reason == "skip_save_only", (
        f"the skipped dispatch must run once the thread is released, got {row.status}"
    )
    assert thread.id not in bot._processing  # pyright: ignore[reportPrivateUsage]


async def _seed_due_wake(
    db_session_factory: async_sessionmaker[AsyncSession], *, workspace_id: str, funded: bool
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed one wake already due in `_THREAD_ID`; return (tenant_id, key)."""
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id=workspace_id)
        if funded:
            await tenant_ledger.insert_entry(
                session,
                tenant_id=tenant.id,
                delta_usd=Decimal("100.00"),
                reason="trial",
                idempotency_key=f"trial:{tenant.id}",
            )
        account = await make_account(session, tenant=tenant)
    request = ContinuationRequest(
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id=str(_THREAD_ID - 1),
        thread_id=str(_THREAD_ID),
        requester_account_id=account.id,
        requester_external_user_id="555",
        target_ma_agent_id="ag_target",
        target_name="target-agent",
        requested_work="check the build again",
        reason="task_handoff",
        idempotency_key=uuid.uuid4(),
    )
    await enqueue_wake(
        db_session_factory, request, available_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    return tenant.id, request.idempotency_key


def _wake_bot(
    db_session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> tuple[DaimonBot, MagicMock]:
    """A bot whose MA stub knows the wake's target agent, and a live thread for it."""
    target = ma_agent(id="ag_target", name="target-agent", tenant_id=tenant_id)
    bot = make_bot(_make_runtime(db_session_factory, agent=target))
    thread = _make_thread(guild=_make_guild(member=_make_member(admin=False)))
    return bot, thread


def _admission_patches() -> tuple[Any, ...]:
    return (
        patch("daimon.core.turn.admission.resolve_config", new_callable=AsyncMock),
        patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock),
        patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock),
        patch("daimon.adapters.discord.bot.bind_session", new_callable=AsyncMock),
        patch("daimon.adapters.discord.bot.run_prepared_turn", new_callable=AsyncMock),
    )


async def test_a_wake_denied_by_admission_settles_skipped_and_never_runs(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A wake goes through the same balance gate a mention does, and stops there."""
    tenant_id, key = await _seed_due_wake(
        db_session_factory, workspace_id="710000007", funded=False
    )
    bot, thread = _wake_bot(db_session_factory, tenant_id)

    p_config, p_agent, p_env, p_bind, p_run = _admission_patches()
    with (
        p_config as resolve_config,
        p_agent as resolve_agent,
        p_env as resolve_env,
        p_bind as bind,
        p_run as run_turn,
    ):
        resolve_config.return_value = ResolvedConfig(
            agent_name="test-agent",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        resolve_agent.return_value = "ag_target"
        resolve_env.return_value = "env_test"
        await bot.dispatch_continuations_in_thread(
            tenant_id=tenant_id, thread=thread, guild_id="710000007"
        )

    bind.assert_not_awaited()
    run_turn.assert_not_awaited()
    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None
    assert row.status == "skipped"
    assert row.skip_reason == "admission_denied:balance_depleted"


async def test_the_wake_poller_opens_the_thread_and_runs_the_wake_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Poll → open thread → admit/bind/run → delivered; a second poll finds nothing."""
    tenant_id, key = await _seed_due_wake(db_session_factory, workspace_id="710000008", funded=True)
    bot, thread = _wake_bot(db_session_factory, tenant_id)
    bot.get_channel = MagicMock(return_value=thread)  # pyright: ignore[reportAttributeAccessIssue]

    p_config, p_agent, p_env, p_bind, p_run = _admission_patches()
    with (
        p_config as resolve_config,
        p_agent as resolve_agent,
        p_env as resolve_env,
        p_bind as bind,
        p_run as run_turn,
    ):
        resolve_config.return_value = ResolvedConfig(
            agent_name="test-agent",
            agent_name_tier="tenant",
            environment_name="test-env",
            environment_name_tier="tenant",
        )
        resolve_agent.return_value = "ag_target"
        resolve_env.return_value = "env_test"
        bind.return_value = _make_prepared_turn(account_id=uuid.uuid4())
        run_turn.return_value = RunOutcome(
            state=TurnState(), ma_session_id="sess_test", mapping_id=None, recovered=False
        )
        for _ in range(2):
            await poll_wakes_once(
                db_session_factory,
                platform="discord",
                open_thread=bot._open_wake_thread,  # pyright: ignore[reportPrivateUsage]
                now=datetime.now(UTC),
            )
            for task in list(bot._bg_tasks):  # pyright: ignore[reportPrivateUsage]
                await task

    run_turn.assert_awaited_once()
    assert run_turn.await_args is not None
    assert run_turn.await_args.kwargs["user_message"].endswith("check the build again")
    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None and row.status == "delivered" and row.attempts == 1


async def test_the_wake_poller_settles_wakes_for_a_thread_that_is_gone(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, key = await _seed_due_wake(db_session_factory, workspace_id="710000009", funded=True)
    bot, _thread = _wake_bot(db_session_factory, tenant_id)
    bot.get_channel = MagicMock(return_value=None)  # pyright: ignore[reportAttributeAccessIssue]
    bot.fetch_channel = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
        side_effect=discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Channel")
    )

    await poll_wakes_once(
        db_session_factory,
        platform="discord",
        open_thread=bot._open_wake_thread,  # pyright: ignore[reportPrivateUsage]
        now=datetime.now(UTC),
    )

    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None
    assert row.status == "skipped" and row.skip_reason == "thread_unavailable"
