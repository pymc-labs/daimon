"""Tests for `SlackApp`'s continuation-dispatch entry points.

`dispatch_continuations_in_thread` is the entry a caller OUTSIDE a turn uses
(a private-form submission, say); it takes the same per-thread `_processing`
guard a mention takes, then delegates to `_dispatch_continuations`, which the
turn tail calls directly because it already owns the guard.

`_run_continuation_turn` is exercised with `bind_session` /
`run_prepared_turn` patched at the names `app.py` imports (the precedent in
`test_continuity_copy.py`) so the assertion is about the turn controls the
follow-up runs with, not the session-preparation pipeline. Slack itself is
faked at the transport (`fake_slack_web_client`), never method-mocked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.runtime import SlackRuntime, build_turn_deps
from daimon.core.continuity.continuation import ContinuationRequest, record_continuation
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import tenant_ledger
from daimon.core.stores.domain import ContinuationReason, TaskContinuationRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.task_continuations import get_continuation, list_pending_continuations
from daimon.core.turn.prepare import ContinuityOutcome, PreparedTurn
from daimon.core.turn.run import RunOutcome
from daimon.core.turn.state import TurnState
from daimon.testing import build_fake_anthropic, ma_agent, ma_environment, resolved_agent_env_router
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_AGENT_ID = "agent_continuation_entry"
_ENV_ID = "env_continuation_entry"
_CHANNEL = "C_CONT_ENTRY"
_THREAD_ID = "9300000001.000001"


def _make_app(sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id_str: str) -> SlackApp:
    settings = MagicMock()
    settings.crypto.keys = ()
    settings.slack.max_concurrent_turns_per_tenant = 3
    settings.slack.bot_display_name = "daimon"
    settings.mcp.public_url = None
    settings.mcp.app_root_url = None
    settings.defaults_root = MagicMock()
    settings.billing.markup = Decimal("1.0")

    anthropic_client = build_fake_anthropic(
        resolved_agent_env_router(
            ma_agent(id=_AGENT_ID, name="uat-agent", tenant_id=tenant_id_str),
            ma_environment(id=_ENV_ID, name="test-env", tenant_id=tenant_id_str),
        ).dispatch
    )
    deployment_default = DeploymentDefault(agent_name="uat-agent", environment_name="test-env")
    resolver_cache = new_resolver_cache()
    turn_deps = build_turn_deps(
        settings,
        anthropic_client,
        sessionmaker,
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
        billing_config=None,
    )
    runtime = SlackRuntime(
        settings=settings,
        anthropic=anthropic_client,
        sessionmaker=sessionmaker,
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=resolver_cache,
        turn_deps=turn_deps,
        deployment_default=deployment_default,
    )
    return SlackApp(runtime=runtime)


async def _seed_pending_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_id: str,
    requested_work: str | None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed one pending continuation; return (tenant_id, account_id, key)."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id=workspace_id)
    requester = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="slack", external_id="U_REQUESTER"
    )
    await db_session.commit()

    idempotency_key = uuid.uuid4()
    await record_continuation(
        db_session_factory,
        ContinuationRequest(
            tenant_id=tenant.id,
            platform="slack",
            parent_channel_id=_CHANNEL,
            thread_id=_THREAD_ID,
            requester_account_id=requester.account_id,
            requester_external_user_id="U_REQUESTER",
            target_ma_agent_id=_AGENT_ID,
            target_name="uat-agent",
            requested_work=requested_work,
            reason="task_handoff",
            idempotency_key=idempotency_key,
        ),
    )
    return tenant.id, requester.account_id, idempotency_key


async def test_dispatch_continuations_in_thread_skips_a_thread_already_processing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A thread with a turn in flight is left alone, guard and row untouched.

    The running turn reaches `_dispatch_continuations` at its own tail, so the
    row is not dropped -- and the guard this call did not take must not be
    released by it either.
    """
    tenant_id, account_id, key = await _seed_pending_row(
        db_session,
        db_session_factory,
        workspace_id="T_CONT_ENTRY_BUSY",
        requested_work="pick up the report",
    )
    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))
    app._processing.add(_THREAD_ID)  # pyright: ignore[reportPrivateUsage]

    await app.dispatch_continuations_in_thread(
        web_client=fake_slack_web_client.client,
        tenant_id=tenant_id,
        channel=_CHANNEL,
        thread_id=_THREAD_ID,
        account_id=account_id,
        team_id="T_CONT_ENTRY",
    )

    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=key)
        pending = await list_pending_continuations(
            session, tenant_id=tenant_id, platform="slack", thread_id=_THREAD_ID
        )
    assert row is not None, "the seeded continuation should still exist"
    assert row.status == "pending", (
        "a thread already processing must not have its continuation claimed"
    )
    assert len(pending) == 1, (
        f"the row must stay pending for the running turn's tail, got {pending}"
    )
    assert _THREAD_ID in app._processing, (  # pyright: ignore[reportPrivateUsage]
        "the guard belongs to the turn that took it; a skipped call must not release it"
    )


async def test_dispatch_continuations_in_thread_releases_the_guard_after_dispatch(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The guard is taken for the dispatch and released once it is done.

    A save-only continuation (`requested_work=None`) settles `skip_save_only`
    without running a turn, which is enough to prove the dispatch really ran
    inside the guard rather than being skipped by it.
    """
    tenant_id, account_id, key = await _seed_pending_row(
        db_session,
        db_session_factory,
        workspace_id="T_CONT_ENTRY_FREE",
        requested_work=None,
    )
    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))

    await app.dispatch_continuations_in_thread(
        web_client=fake_slack_web_client.client,
        tenant_id=tenant_id,
        channel=_CHANNEL,
        thread_id=_THREAD_ID,
        account_id=account_id,
        team_id="T_CONT_ENTRY",
    )

    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None, "the seeded continuation should still exist"
    assert row.status == "skipped", "the dispatch must have run and settled the row"
    assert row.skip_reason == "skip_save_only", f"unexpected skip reason {row.skip_reason}"
    assert _THREAD_ID not in app._processing, (  # pyright: ignore[reportPrivateUsage]
        "the guard must be released once the dispatch finishes"
    )


async def test_dispatch_continuations_in_thread_releases_the_guard_when_dispatch_raises(
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A dispatch that blows up must not strand the thread's guard forever."""
    app = _make_app(db_session_factory, tenant_id_str=str(uuid.uuid4()))

    with (
        patch(
            "daimon.adapters.slack.app.dispatch_pending_continuations",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(RuntimeError),
    ):
        await app.dispatch_continuations_in_thread(
            web_client=fake_slack_web_client.client,
            tenant_id=uuid.uuid4(),
            channel=_CHANNEL,
            thread_id=_THREAD_ID,
            account_id=uuid.uuid4(),
            team_id="T_CONT_ENTRY",
        )

    assert _THREAD_ID not in app._processing, (  # pyright: ignore[reportPrivateUsage]
        "a raising dispatch must still release the guard"
    )


def _make_continuation_row(
    *, tenant_id: uuid.UUID, account_id: uuid.UUID, reason: ContinuationReason
) -> TaskContinuationRow:
    now = datetime.now(UTC)
    return TaskContinuationRow(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        platform="slack",
        thread_id=_THREAD_ID,
        parent_channel_id=_CHANNEL,
        requester_account_id=account_id,
        requester_external_user_id="U_REQUESTER",
        target_ma_agent_id=_AGENT_ID,
        target_name="uat-agent",
        requested_work="finish the migration",
        reason=reason,
        status="claimed",
        skip_reason=None,
        idempotency_key=uuid.uuid4(),
        created_at=now,
        claimed_at=now,
        delivered_at=None,
    )


def _prepared_turn(*, account_id: uuid.UUID) -> PreparedTurn:
    async def _record_noop(*, event: Any) -> None:
        return None

    from daimon.core.turn.admission import Admission

    return PreparedTurn(
        admission=MagicMock(spec=Admission),
        ma_session_id="sess_continuation_entry",
        mapping_id=None,
        watermark=None,
        reused=True,
        session_account_id=account_id,
        _record=_record_noop,
        continuity=ContinuityOutcome(state="continued", transfer_kind="none"),
    )


async def _run_continuation_and_capture_controls(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    *,
    workspace_id: str,
    reason: ContinuationReason,
) -> str:
    """Run one continuation turn and return the `user_message` it ran with."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id=workspace_id)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    requester = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="slack", external_id="U_REQUESTER"
    )
    await db_session.commit()

    app = _make_app(db_session_factory, tenant_id_str=str(tenant.id))
    row = _make_continuation_row(
        tenant_id=tenant.id, account_id=requester.account_id, reason=reason
    )

    with (
        patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock) as resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as resolve_env,
        patch("daimon.adapters.slack.app.bind_session", new_callable=AsyncMock) as bind,
        patch("daimon.adapters.slack.app.run_prepared_turn", new_callable=AsyncMock) as run_turn,
    ):
        resolve_agent.return_value = _AGENT_ID
        resolve_env.return_value = _ENV_ID
        bind.return_value = _prepared_turn(account_id=requester.account_id)
        run_turn.return_value = RunOutcome(
            state=TurnState(),
            ma_session_id="sess_continuation_entry",
            mapping_id=None,
            recovered=False,
        )
        await app._run_continuation_turn(  # pyright: ignore[reportPrivateUsage]
            row,
            "finish the migration",
            web_client=fake_slack_web_client.client,
            tenant_id=tenant.id,
            channel=_CHANNEL,
            thread_id=_THREAD_ID,
        )

    assert run_turn.await_args is not None, "the follow-up turn should have run"
    user_message = run_turn.await_args.kwargs["user_message"]
    assert isinstance(user_message, str), "user_message should be the rendered controls + seed"
    return user_message


async def test_continuation_turn_omits_handoff_notice_for_private_input(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """`private_input_applied` re-runs the SAME agent, so there is no handoff.

    A `task_handoff` row still gets the notice -- the suppression is keyed on
    the row's `reason`, not on the dispatch path.
    """
    private_controls = await _run_continuation_and_capture_controls(
        db_session,
        db_session_factory,
        fake_slack_web_client,
        workspace_id="T_CONT_ENTRY_PRIVATE",
        reason="private_input_applied",
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
        db_session,
        db_session_factory,
        fake_slack_web_client,
        workspace_id="T_CONT_ENTRY_HANDOFF",
        reason="task_handoff",
    )
    assert '"handoff"' in handoff_controls, (
        f"a task handoff must still carry the one-time notice, got {handoff_controls}"
    )


async def test_dispatch_skipped_while_processing_runs_when_the_turn_releases_the_thread(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A form submitted after the running turn's own tail dispatch is not stranded.

    The mention turn owns `_processing`; the submission's dispatch arrives
    once that turn has already passed its tail dispatch, so it is skipped.
    When `_orchestrate` releases the thread, the skipped dispatch must run
    (formal/thread_queue `FormDuringTail`), not wait for the next message.
    """
    tenant_id, account_id, key = await _seed_pending_row(
        db_session,
        db_session_factory,
        workspace_id="T_CONT_ENTRY_TAIL",
        requested_work=None,
    )
    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))
    web_client = fake_slack_web_client.client

    async def _turn_whose_tail_already_ran(*_args: Any, **_kwargs: Any) -> None:
        # The form submission lands here: past the tail, before the release.
        await app.dispatch_continuations_in_thread(
            web_client=web_client,
            tenant_id=tenant_id,
            channel=_CHANNEL,
            thread_id=_THREAD_ID,
            account_id=account_id,
            team_id="T_CONT_ENTRY_TAIL",
        )

    with (
        patch.object(app, "_run_thread_turn", side_effect=_turn_whose_tail_already_ran),
        patch.object(app, "_maybe_post_connect_nudge", new_callable=AsyncMock),
    ):
        await app._orchestrate(  # pyright: ignore[reportPrivateUsage]
            {"ts": _THREAD_ID, "user": "U_REQUESTER", "text": "hi"},
            team_id="T_CONT_ENTRY_TAIL",
            channel=_CHANNEL,
            event_ts=_THREAD_ID,
            web_client=web_client,
            tenant_id=tenant_id,
        )
    for task in list(app._bg_tasks):  # pyright: ignore[reportPrivateUsage]
        await task

    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None, "the seeded continuation should still exist"
    assert row.status == "skipped" and row.skip_reason == "skip_save_only", (
        f"the skipped dispatch must run once the thread is released, got {row.status}"
    )
    assert _THREAD_ID not in app._processing, (  # pyright: ignore[reportPrivateUsage]
        "the re-run dispatch must release the guard it took"
    )


async def test_no_redispatch_while_draining(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Shutdown drain starts no new work: the row stays pending for the next turn."""
    tenant_id, account_id, key = await _seed_pending_row(
        db_session,
        db_session_factory,
        workspace_id="T_CONT_ENTRY_DRAIN",
        requested_work=None,
    )
    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))
    app._processing.add(_THREAD_ID)  # pyright: ignore[reportPrivateUsage]
    await app.dispatch_continuations_in_thread(
        web_client=fake_slack_web_client.client,
        tenant_id=tenant_id,
        channel=_CHANNEL,
        thread_id=_THREAD_ID,
        account_id=account_id,
        team_id="T_CONT_ENTRY",
    )
    app.draining = True
    app._release_thread(_THREAD_ID)  # pyright: ignore[reportPrivateUsage]
    assert not app._bg_tasks, "a draining adapter must not spawn the dispatch"  # pyright: ignore[reportPrivateUsage]
    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None and row.status == "pending"


async def test_mention_queued_during_a_continuation_dispatch_gets_its_own_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A mention that lands while a form's continuation turn holds the thread is drained.

    The dispatch holds `_processing`, so the mention queues behind it with ⌛
    exactly as it would behind a mention turn. When the dispatch ends, the
    queued mention must get its own turn instead of waiting for the next
    mention in the thread (formal/thread_queue `NoStrandedMention`).
    """
    tenant_id, account_id, key = await _seed_pending_row(
        db_session,
        db_session_factory,
        workspace_id="T_CONT_ENTRY_MENTION",
        requested_work="pick up the report",
    )
    app = _make_app(db_session_factory, tenant_id_str=str(tenant_id))
    web_client = fake_slack_web_client.client
    mention = {
        "ts": "9300000001.000099",
        "thread_ts": _THREAD_ID,
        "user": "U_OTHER",
        "text": "and the chart too?",
    }

    async def _continuation_turn_while_a_mention_arrives(*_args: Any, **_kwargs: Any) -> None:
        await app._orchestrate(  # pyright: ignore[reportPrivateUsage]
            mention,
            team_id="T_CONT_ENTRY_MENTION",
            channel=_CHANNEL,
            event_ts=mention["ts"],
            web_client=web_client,
            tenant_id=tenant_id,
        )
        assert app._pending.get(_THREAD_ID) == [mention], (  # pyright: ignore[reportPrivateUsage]
            "a mention during the dispatch must queue behind it, not run beside it"
        )

    thread_turn = AsyncMock()
    with (
        patch.object(
            app,
            "_run_continuation_turn",
            side_effect=_continuation_turn_while_a_mention_arrives,
        ),
        patch.object(app, "_run_thread_turn", thread_turn),
    ):
        await app.dispatch_continuations_in_thread(
            web_client=web_client,
            tenant_id=tenant_id,
            channel=_CHANNEL,
            thread_id=_THREAD_ID,
            account_id=account_id,
            team_id="T_CONT_ENTRY_MENTION",
        )
        for task in list(app._bg_tasks):  # pyright: ignore[reportPrivateUsage]
            await task

    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None and row.status != "pending", "the continuation itself must run"
    hourglass = [
        req
        for (method, url), reqs in fake_slack_web_client.mock.requests.items()
        if method == "POST" and url.path == "/api/reactions.add"
        for req in reqs
    ]
    assert hourglass, "the queued mention should carry the ⌛ reaction"
    assert thread_turn.await_count == 1, (
        f"the queued mention must get its own turn once the dispatch ends, "
        f"got {thread_turn.await_count} turns"
    )
    call = thread_turn.await_args
    assert call is not None
    assert call.args[0] is mention
    assert call.kwargs["content_override"] == "and the chart too?"
    assert call.kwargs["team_id"] == "T_CONT_ENTRY_MENTION"
    assert _THREAD_ID not in app._pending  # pyright: ignore[reportPrivateUsage]
    assert _THREAD_ID not in app._processing  # pyright: ignore[reportPrivateUsage]
