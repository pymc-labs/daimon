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
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import yarl
from daimon.adapters.slack import continuation_dispatch
from daimon.adapters.slack.continuation_dispatch import dispatch_pending_continuations
from daimon.core.continuity.continuation import ContinuationRequest, record_continuation
from daimon.core.continuity.wakes import (
    WAKE_CLAIM_LEASE,
    WAKE_RUN_LEASE,
    abandon_interrupted_wakes,
)
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


async def _seed_request(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_id: str,
    thread_id: str,
) -> ContinuationRequest:
    tenant = await make_tenant(db_session, platform="slack", workspace_id=workspace_id)
    requester = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="slack", external_id="U_REQUESTER"
    )
    await db_session.commit()
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
    return request


async def _read(
    db_session_factory: async_sessionmaker[AsyncSession], key: uuid.UUID
) -> TaskContinuationRow:
    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None
    return row


async def test_a_handoff_survives_any_number_of_busy_binds_as_on_main(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Baseline parity: five busy binds, then a free session, delivers the handoff.

    On main each busy bind settled the row and re-queued the request under a
    new key for the next turn tail, without limit, so the sixth tail delivered
    it. Here the same row is released with its claim refunded and
    `available_at` left NULL: delivered on the sixth tail, exactly once.
    """
    thread_id = "9200000003.000001"
    request = await _seed_request(
        db_session, db_session_factory, workspace_id="T_CONT_DISPATCH_BUSY", thread_id=thread_id
    )
    anthropic = build_fake_anthropic(_fake_target_agent_handler(str(request.tenant_id)))
    now = datetime.now(UTC)

    async def _no_newer_message(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(continuation_dispatch, "_latest_human_message_at", _no_newer_message)
    tails = 0
    delivered: list[str] = []

    async def _busy_five_times(row: TaskContinuationRow, seed: str) -> None:
        nonlocal tails
        tails += 1
        if tails <= 5:
            raise SessionBusyError(pending_reasons=("agent_identity",), retry_after=now)
        delivered.append(seed)

    for tail in range(6):
        at = now + timedelta(minutes=tail)
        await dispatch_pending_continuations(
            db_session_factory,
            anthropic,
            fake_slack_web_client.client,
            tenant_id=request.tenant_id,
            channel="C_CONT_DISPATCH",
            thread_id=thread_id,
            active_turn=False,
            run_follow_up=_busy_five_times,
            now=lambda at=at: at,
        )
        row = await _read(db_session_factory, request.idempotency_key)
        if tail < 5:
            assert row.status == "pending" and row.attempts == 0, "a busy wait is refunded"
            assert row.available_at is None, "a handoff still waits for the next turn tail"

    assert delivered == ["please pick up the migration"]
    row = await _read(db_session_factory, request.idempotency_key)
    assert row.status == "delivered" and row.attempts == 1
    pending = await list_pending_continuations(
        db_session, tenant_id=request.tenant_id, platform="slack", thread_id=thread_id
    )
    assert pending == [], "no second row is ever queued"


async def test_process_death_before_the_fence_is_retried_after_lease_expiry(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim whose holder died before starting the turn runs once, after its lease."""
    thread_id = "9200000004.000002"
    request = await _seed_request(
        db_session, db_session_factory, workspace_id="T_CONT_CRASH_PRE", thread_id=thread_id
    )
    anthropic = build_fake_anthropic(_fake_target_agent_handler(str(request.tenant_id)))
    now = datetime.now(UTC)

    async def _no_newer_message(*_args: object, **_kwargs: object) -> None:
        return None

    async def _die_reading_history(*_args: object, **_kwargs: object) -> None:
        raise _SimulatedProcessDeath

    def _dispatch(
        run_follow_up: Callable[[TaskContinuationRow, str], Awaitable[None]], at: datetime
    ) -> Any:
        return dispatch_pending_continuations(
            db_session_factory,
            anthropic,
            fake_slack_web_client.client,
            tenant_id=request.tenant_id,
            channel="C_CONT_DISPATCH",
            thread_id=thread_id,
            active_turn=False,
            run_follow_up=run_follow_up,
            now=lambda: at,
        )

    calls, run_follow_up = _recorder()
    monkeypatch.setattr(continuation_dispatch, "_latest_human_message_at", _die_reading_history)
    with pytest.raises(_SimulatedProcessDeath):
        await _dispatch(run_follow_up, now)
    stranded = await _read(db_session_factory, request.idempotency_key)
    assert stranded.status == "claimed" and stranded.started_at is None

    monkeypatch.setattr(continuation_dispatch, "_latest_human_message_at", _no_newer_message)
    await _dispatch(run_follow_up, now + timedelta(seconds=1))
    assert calls == [], "the dead process's lease is still live"

    await _dispatch(run_follow_up, now + WAKE_CLAIM_LEASE + timedelta(seconds=1))
    assert len(calls) == 1, "an expired unstarted claim is retried exactly once"
    delivered = await _read(db_session_factory, request.idempotency_key)
    assert delivered.status == "delivered" and delivered.attempts == 2


@pytest.mark.parametrize(
    "effect_before_crash", [False, True], ids=["before-effect", "after-effect"]
)
async def test_process_death_after_the_fence_settles_interrupted_and_never_reruns(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    effect_before_crash: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A started claim is never run again, whichever side of its effect the process died on."""
    thread_id = "9200000004.000001"
    request = await _seed_request(
        db_session,
        db_session_factory,
        workspace_id=f"T_CONT_CRASH_{effect_before_crash}",
        thread_id=thread_id,
    )
    anthropic = build_fake_anthropic(_fake_target_agent_handler(str(request.tenant_id)))
    visible_effects: list[str] = []
    now = datetime.now(UTC)

    async def _no_newer_message(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(continuation_dispatch, "_latest_human_message_at", _no_newer_message)

    async def _die_during_follow_up(row: TaskContinuationRow, seed: str) -> None:
        if effect_before_crash:
            visible_effects.append(seed)
        raise _SimulatedProcessDeath

    def _dispatch(
        run_follow_up: Callable[[TaskContinuationRow, str], Awaitable[None]], at: datetime
    ) -> Any:
        return dispatch_pending_continuations(
            db_session_factory,
            anthropic,
            fake_slack_web_client.client,
            tenant_id=request.tenant_id,
            channel="C_CONT_DISPATCH",
            thread_id=thread_id,
            active_turn=False,
            run_follow_up=run_follow_up,
            now=lambda: at,
        )

    with pytest.raises(_SimulatedProcessDeath):
        await _dispatch(_die_during_follow_up, now)

    after_lease = now + WAKE_RUN_LEASE + timedelta(seconds=1)
    calls, run_follow_up = _recorder()
    await _dispatch(run_follow_up, after_lease)
    assert calls == [], "a started claim must never be retried"

    await abandon_interrupted_wakes(db_session_factory, platform="slack", now=after_lease)
    settled = await _read(db_session_factory, request.idempotency_key)
    assert settled.status == "skipped" and settled.skip_reason == "interrupted"
    assert len(visible_effects) == int(effect_before_crash), (
        "a later dispatch must not repeat the observable effect"
    )


async def test_a_timer_refused_for_a_changed_responder_posts_why_and_settles_skipped(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core.continuity.continuation import ResponderChanged

    thread_id = "9200000005.000001"
    request = await _seed_request(
        db_session, db_session_factory, workspace_id="T_TIMER_TARGET", thread_id=thread_id
    )
    anthropic = build_fake_anthropic(_fake_target_agent_handler(str(request.tenant_id)))

    async def _no_newer_message(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(continuation_dispatch, "_latest_human_message_at", _no_newer_message)

    async def _rerouted(row: TaskContinuationRow, seed: str) -> None:
        raise ResponderChanged(target_name="receiving-agent", current_name="other-agent")

    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        fake_slack_web_client.client,
        tenant_id=request.tenant_id,
        channel="C_CONT_DISPATCH",
        thread_id=thread_id,
        active_turn=False,
        run_follow_up=_rerouted,
    )

    row = await _read(db_session_factory, request.idempotency_key)
    assert row.status == "skipped" and row.skip_reason == "skip_target_changed"
    posts = fake_slack_web_client.mock.requests.get(
        ("POST", yarl.URL("https://slack.com/api/chat.postMessage")), []
    )
    texts = [
        str((p.kwargs.get("json") or p.kwargs.get("data") or {}).get("text") or "") for p in posts
    ]
    assert any("other-agent answers in this thread now" in text for text in texts), texts


@pytest.mark.parametrize("path", ["responder_changed", "turn_running"])
@pytest.mark.parametrize("target", ["protected", "unknown", "open"])
async def test_skip_copy_reaches_only_a_thread_the_agent_may_post_in(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    target: str,
) -> None:
    """SYS-048: the dispatcher's skip and responder-changed copy goes through the
    may-post state. A protected thread, or one whose protection can't be read,
    gets nothing; the row settles either way. `open` is the control."""
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.continuity.continuation import ResponderChanged
    from daimon.core.stores.access_policy import set_access_policy
    from sqlalchemy.exc import OperationalError

    thread_id = "9200000009.000001"
    request = await _seed_request(
        db_session, db_session_factory, workspace_id=f"T_SKIP_{target}_{path}", thread_id=thread_id
    )
    if target == "protected":
        async with db_session_factory() as s:
            await set_access_policy(
                s,
                tenant_id=request.tenant_id,
                policy=TenantAccessPolicy(protected_channel_ids=("C_CONT_DISPATCH",)),
            )
            await s.commit()
    if target == "unknown":

        async def _policy_read_fails(*_args: object, **_kwargs: object) -> object:
            raise OperationalError("SELECT", {}, Exception("pool gone"))

        monkeypatch.setattr("daimon.core.turn.protection.load_access_policy", _policy_read_fails)
    anthropic = build_fake_anthropic(_fake_target_agent_handler(str(request.tenant_id)))

    async def _no_newer_message(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(continuation_dispatch, "_latest_human_message_at", _no_newer_message)

    async def _rerouted(row: TaskContinuationRow, seed: str) -> None:
        raise ResponderChanged(target_name="receiving-agent", current_name="other-agent")

    await dispatch_pending_continuations(
        db_session_factory,
        anthropic,
        fake_slack_web_client.client,
        tenant_id=request.tenant_id,
        channel="C_CONT_DISPATCH",
        thread_id=thread_id,
        active_turn=path == "turn_running",
        run_follow_up=_rerouted,
    )

    row = await _read(db_session_factory, request.idempotency_key)
    assert row.status == "skipped", "the row settles whether or not anything is posted"
    posts = fake_slack_web_client.mock.requests.get(
        ("POST", yarl.URL("https://slack.com/api/chat.postMessage")), []
    )
    if target == "open":
        assert posts, "an open thread still gets the skip copy"
    else:
        assert posts == [], f"{target} thread must receive nothing"
