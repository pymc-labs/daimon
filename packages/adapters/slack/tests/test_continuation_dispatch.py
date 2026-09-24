"""Tests for `daimon.adapters.slack.continuation_dispatch.dispatch_pending_continuations`.

Real Postgres via `daimon.core.continuity.continuation` (the same store the
Slack turn-completion path calls) and a real `AsyncWebClient` intercepted by
`aioresponses` (`fake_slack_web_client`). `run_follow_up` is injected as a
plain async recorder -- the module's whole reason to accept it -- so these
tests assert claim/skip/dispatch behavior without running a second real turn.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from daimon.adapters.slack.continuation_dispatch import dispatch_pending_continuations
from daimon.core.continuity.continuation import ContinuationRequest, record_continuation
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.task_continuations import get_continuation, list_pending_continuations
from daimon.core.turn.errors import SessionBusyError
from daimon.testing import build_fake_anthropic, ma_agent
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TARGET_AGENT_ID = "agent_continuation_target"


class _SimulatedProcessDeath(BaseException):
    """Bypass dispatcher error handling to model abrupt process termination."""


def _fake_target_agent_handler(tenant_id_str: str) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/agents/{_TARGET_AGENT_ID}":
            agent = ma_agent(id=_TARGET_AGENT_ID, name="receiving-agent", tenant_id=tenant_id_str)
            return httpx.Response(200, json=agent.model_dump(mode="json"))
        raise AssertionError(f"unhandled {request.method} {request.url.path}")

    return handler


def _recorder() -> tuple[
    list[tuple[TaskContinuationRow, str]], Callable[[TaskContinuationRow, str], Awaitable[None]]
]:
    calls: list[tuple[TaskContinuationRow, str]] = []

    async def _run_follow_up(row: TaskContinuationRow, seed: str) -> None:
        calls.append((row, seed))

    return calls, _run_follow_up


async def test_dispatch_runs_the_follow_up_exactly_once_and_settles_delivered(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_CONT_DISPATCH_DELIVER")
    requester = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="slack", external_id="U_REQUESTER"
    )
    await db_session.commit()

    thread_id = "9200000001.000001"
    request = ContinuationRequest(
        tenant_id=tenant.id,
        platform="slack",
        parent_channel_id="C_CONT_DISPATCH",
        thread_id=thread_id,
        requester_account_id=requester.account_id,
        requester_external_user_id="U_REQUESTER",
        target_ma_agent_id=_TARGET_AGENT_ID,
        target_name="receiving-agent",
        requested_work="please pick up the migration",
        reason="task_handoff",
        idempotency_key=uuid.uuid4(),
    )
    await record_continuation(db_session_factory, request)

    anthropic = build_fake_anthropic(_fake_target_agent_handler(str(tenant.id)))
    calls, run_follow_up = _recorder()

    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        fake_slack_web_client.client,
        tenant_id=tenant.id,
        channel="C_CONT_DISPATCH",
        thread_id=thread_id,
        active_turn=False,
        run_follow_up=run_follow_up,
        now=lambda: datetime.now(UTC),
    )

    assert len(calls) == 1, "the follow-up must run exactly once for one pending continuation"
    dispatched_row, seed = calls[0]
    assert dispatched_row.idempotency_key == request.idempotency_key
    assert seed == "please pick up the migration"

    settled = await get_continuation(db_session, idempotency_key=request.idempotency_key)
    assert settled is not None
    assert settled.status == "delivered"
    assert settled.delivered_at is not None

    # A second dispatch call (e.g. the next turn's completion path, or a race)
    # must not run the follow-up again: the row is no longer pending.
    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        fake_slack_web_client.client,
        tenant_id=tenant.id,
        channel="C_CONT_DISPATCH",
        thread_id=thread_id,
        active_turn=False,
        run_follow_up=run_follow_up,
        now=lambda: datetime.now(UTC),
    )
    assert len(calls) == 1, "a claimed/delivered continuation must never dispatch twice"


async def test_dispatch_skips_silently_when_no_work_was_requested(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A handoff that only switched the responder (no `continuation` text)
    must settle `skipped`/`skip_save_only` without ever calling `run_follow_up`
    or posting anything -- nothing was ever promised."""
    tenant = await make_tenant(
        db_session, platform="slack", workspace_id="T_CONT_DISPATCH_SAVE_ONLY"
    )
    requester = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="slack", external_id="U_REQUESTER_2"
    )
    await db_session.commit()

    thread_id = "9200000002.000001"
    request = ContinuationRequest(
        tenant_id=tenant.id,
        platform="slack",
        parent_channel_id="C_CONT_DISPATCH_2",
        thread_id=thread_id,
        requester_account_id=requester.account_id,
        requester_external_user_id="U_REQUESTER_2",
        target_ma_agent_id=_TARGET_AGENT_ID,
        target_name="receiving-agent",
        requested_work=None,
        reason="task_handoff",
        idempotency_key=uuid.uuid4(),
    )
    await record_continuation(db_session_factory, request)

    anthropic = build_fake_anthropic(_fake_target_agent_handler(str(tenant.id)))
    calls, run_follow_up = _recorder()

    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        fake_slack_web_client.client,
        tenant_id=tenant.id,
        channel="C_CONT_DISPATCH_2",
        thread_id=thread_id,
        active_turn=False,
        run_follow_up=run_follow_up,
        now=lambda: datetime.now(UTC),
    )

    assert calls == [], "no work was requested -- the follow-up must never run"
    settled = await get_continuation(db_session, idempotency_key=request.idempotency_key)
    assert settled is not None
    assert settled.status == "skipped"
    assert settled.skip_reason == "skip_save_only"

    posts = [
        req
        for (_method, url), reqs in fake_slack_web_client.mock.requests.items()
        if str(url) == "https://slack.com/api/chat.postMessage"
        for req in reqs
    ]
    assert posts == [], "a silent skip must not post anything into the thread"


async def test_a_busy_session_settles_skipped_and_requeues_under_a_new_key(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A follow-up that cannot bind because a turn is still running is not lost.

    `claim_continuation` has no inverse, so the claimed row is settled
    `skipped`/`turn_running` and the same request is queued again under a NEW
    idempotency key. At-most-once still holds per key -- the settled row can
    never dispatch again -- while the work the person asked for survives to be
    picked up by the next turn that finishes in this thread.
    """
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_CONT_DISPATCH_BUSY")
    requester = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="slack", external_id="U_REQUESTER"
    )
    await db_session.commit()

    thread_id = "9200000003.000001"
    request = ContinuationRequest(
        tenant_id=tenant.id,
        platform="slack",
        parent_channel_id="C_CONT_DISPATCH",
        thread_id=thread_id,
        requester_account_id=requester.account_id,
        requester_external_user_id="U_REQUESTER",
        target_ma_agent_id=_TARGET_AGENT_ID,
        target_name="receiving-agent",
        requested_work="please pick up the migration",
        reason="task_handoff",
        idempotency_key=uuid.uuid4(),
    )
    await record_continuation(db_session_factory, request)

    anthropic = build_fake_anthropic(_fake_target_agent_handler(str(tenant.id)))

    async def _busy_follow_up(row: TaskContinuationRow, seed: str) -> None:
        raise SessionBusyError(pending_reasons=("agent_identity",), retry_after=datetime.now(UTC))

    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        fake_slack_web_client.client,
        tenant_id=tenant.id,
        channel="C_CONT_DISPATCH",
        thread_id=thread_id,
        active_turn=False,
        run_follow_up=_busy_follow_up,
        now=lambda: datetime.now(UTC),
    )

    settled = await get_continuation(db_session, idempotency_key=request.idempotency_key)
    assert settled is not None
    assert settled.status == "skipped", "the claimed row must not be left claimed"
    assert settled.skip_reason == "turn_running"

    pending = await list_pending_continuations(
        db_session, tenant_id=tenant.id, platform="slack", thread_id=thread_id
    )
    assert len(pending) == 1, f"the request must be re-queued exactly once, got {pending}"
    requeued = pending[0]
    assert requeued.idempotency_key != request.idempotency_key, (
        "the re-queued row must carry a NEW key, so the settled row's at-most-once still holds"
    )
    assert requeued.requested_work == "please pick up the migration", (
        "the person's own words must survive the requeue"
    )
    assert requeued.target_ma_agent_id == _TARGET_AGENT_ID
    assert requeued.requester_account_id == requester.account_id
    assert requeued.reason == "task_handoff"


@pytest.mark.parametrize(
    "effect_before_crash", [False, True], ids=["before-effect", "after-effect"]
)
async def test_process_death_strands_claim_without_automatic_retry(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    effect_before_crash: bool,
) -> None:
    tenant = await make_tenant(
        db_session, platform="slack", workspace_id=f"T_CONT_CRASH_{effect_before_crash}"
    )
    requester = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="slack", external_id="U_CONT_CRASH"
    )
    await db_session.commit()
    thread_id = "9200000004.000001"
    request = ContinuationRequest(
        tenant_id=tenant.id,
        platform="slack",
        parent_channel_id="C_CONT_CRASH",
        thread_id=thread_id,
        requester_account_id=requester.account_id,
        requester_external_user_id="U_CONT_CRASH",
        target_ma_agent_id=_TARGET_AGENT_ID,
        target_name="receiving-agent",
        requested_work="continue",
        reason="task_handoff",
        idempotency_key=uuid.uuid4(),
    )
    await record_continuation(db_session_factory, request)
    anthropic = build_fake_anthropic(_fake_target_agent_handler(str(tenant.id)))
    visible_effects: list[str] = []

    async def _die_during_follow_up(row: TaskContinuationRow, seed: str) -> None:
        if effect_before_crash:
            visible_effects.append(seed)
        raise _SimulatedProcessDeath

    def _dispatch(run_follow_up: Callable[[TaskContinuationRow, str], Awaitable[None]]) -> Any:
        return dispatch_pending_continuations(
            db_session_factory,
            anthropic,
            fake_slack_web_client.client,
            tenant_id=tenant.id,
            channel="C_CONT_CRASH",
            thread_id=thread_id,
            active_turn=False,
            run_follow_up=run_follow_up,
            now=lambda: datetime.now(UTC),
        )

    with pytest.raises(_SimulatedProcessDeath):
        await _dispatch(_die_during_follow_up)

    claimed = await get_continuation(db_session, idempotency_key=request.idempotency_key)
    assert claimed is not None and claimed.status == "claimed", (
        "process death must leave the committed continuation claim unsettled"
    )
    assert len(visible_effects) == int(effect_before_crash), (
        "the injected effect must match the selected crash boundary"
    )

    calls, run_follow_up = _recorder()
    await _dispatch(run_follow_up)
    assert calls == [], "a new dispatcher must not retry an already-claimed continuation"
    assert len(visible_effects) == int(effect_before_crash), (
        "a later dispatch must not repeat the observable effect"
    )
    still_claimed = await get_continuation(db_session, idempotency_key=request.idempotency_key)
    assert still_claimed is not None and still_claimed.status == "claimed", (
        "a later dispatch must leave the stranded claim unchanged"
    )
