"""Executable record of a deliberate gap: a compute wake reads only in a thread.

When a background command ends, the turn its wake starts may read the command's
session (`daimon.core.compute_sessions.turn_may_read`). In a thread that turn runs
in the session's own conversation. A wake whose conversation is a Discord DM
(`dm:<uuid>`) or a plain channel's scope (`ch:<uuid>`) runs through the scope timer
runners instead (`direct_messages.run_dm_timer`, `run_channel_timer`). Each starts
the turn in a fresh scope with no history, so the turn's origin stands in another
conversation than the session, and the session's conversation check refuses every
read before the wake binding is looked at. The wake text therefore offers the read
to the owner only (`stores.compute_tasks.wake_reads_in`), and the agent relays the
end of the output it was given. The same check refuses a command, a file write and a
stop, so the setting that lets a wake turn continue on its machine
(`compute_wake_runs`, `compute_sessions.turn_may_act`) does nothing there.

Closing the gap means carrying verified provenance of the source conversation
through these runners, so the fresh turn can prove which conversation its wake came
from. When that lands these tests fail: replace them with tests of the read, and
let `wake_reads_in` say yes there.

No live turn runs in a `ch:` scope today, so no compute session has one; the plain
channel runner is recorded so the gap stays known when one does.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.core import direct_messages
from daimon.core.compute_sessions import ELSEWHERE, ComputeCaller, ComputeSessions
from daimon.core.compute_tasks import wake_key
from daimon.core.continuity.scope_timers import dispatch_scope_timer
from daimon.core.direct_messages import run_channel_timer, run_dm_timer
from daimon.core.errors import StoreError
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.stores.domain import Role
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.stores.turn_origins import bind_origin_session, create_origin, get_active_origin
from daimon.core.turn.termination import TerminationReason
from daimon.core.turn_origin import current_origin_id
from daimon.testing.compute import FakeComputeProvider
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.turn_fakes import deps_for
from sqlalchemy.ext.asyncio import async_sessionmaker

from .test_compute_sessions import _wake_runs, settings
from .test_dm_timer_run import OWNER, admission_for

_RUNNERS = {
    "dm": lambda deps, row, seed: run_dm_timer(deps, row, prompt=seed),
    "channel": lambda deps, row, seed: run_channel_timer(deps, row, prompt=seed, role=Role.USER),
}
# The conversation the machine was launched in: (channel, scope).
_SOURCES = {"dm": ("dm-channel", "dm:"), "channel": ("900", "ch:")}


@pytest.mark.parametrize("place", sorted(_RUNNERS))
async def test_a_wake_in_a_dm_or_plain_channel_reads_nothing_today(
    db_engine, db_clean, monkeypatch, place
):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    channel, prefix = _SOURCES[place]
    scope = f"{prefix}{uuid.uuid4()}"
    async with sm.begin() as db:
        tenant = await make_tenant(db, workspace_id="700000202")
        account = await make_account(db, tenant=tenant)
        await set_dm_enabled(db, tenant_id=tenant.id, enabled=True)
        now = datetime.now(UTC)
        present = await create_origin(
            db,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="discord",
            parent_channel_id=channel,
            thread_id=scope,
            responder_ma_agent_id="ag_target",
            responder_name="target-agent",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.USER,
            expires_at=now + timedelta(minutes=5),
            now=now,
            sender_foreign=False,
            shared_channel=False,
        )
        await bind_origin_session(
            db, origin_id=present.id, chat_session_id="owner", human_invocation=True
        )
        present = await get_active_origin(
            db,
            origin_id=present.id,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="discord",
            now=now,
        )
    assert present is not None
    fake = FakeComputeProvider()
    service = ComputeSessions(sm, settings(), fake)
    owner = ComputeCaller(tenant.id, account.id, present, "owner", OWNER)
    session = await service.launch(
        owner,
        profile="mmm",
        region="us-central1",
        ttl_hours=24,
        approval_key="approved",
        source_metadata={},
    )
    # Even where a server admin lets a wake turn continue, these places stay refused.
    await _wake_runs(sm, tenant.id)
    task = await service.start_task(
        owner, session.id, command="python fit.py", timeout_minutes=60, request_key="use"
    )
    fake.tasks[task.id.hex].status, fake.tasks[task.id.hex].exit_code = "exit", 0
    await service.poll_tasks()
    async with sm() as db:
        row = await get_continuation(db, idempotency_key=wake_key(task.id))
    assert row is not None and (row.parent_channel_id, row.thread_id) == (channel, scope)
    # The agent is told what is true here: the owner can read, it cannot.
    assert "You can read" not in (row.requested_work or "")
    assert "They can read the full result" in (row.requested_work or "")
    assert "You can run" not in (row.requested_work or "")

    seen: dict[str, object] = {}

    async def turn(_deps, _prepared, **_kwargs):
        """The wake's turn, as the real runner starts it: try the read the agent would."""
        origin_id = current_origin_id.get()
        assert origin_id is not None
        async with sm.begin() as db:
            await bind_origin_session(
                db, origin_id=origin_id, chat_session_id="wake", human_invocation=False
            )
            origin = await get_active_origin(
                db,
                origin_id=origin_id,
                tenant_id=tenant.id,
                account_id=account.id,
                platform="discord",
                now=datetime.now(UTC),
            )
            running = await get_continuation(db, idempotency_key=row.idempotency_key)
        assert origin is not None and running is not None
        seen["bound"] = origin.id.hex == running.lease_owner
        seen["place"] = (origin.parent_channel_id, origin.thread_id)
        try:
            await service.task_result(
                ComputeCaller(tenant.id, account.id, origin, "wake", OWNER), task.id
            )
            seen["read"] = "allowed"
        except StoreError as refusal:
            seen["read"] = str(refusal)
        try:
            await service.run(
                ComputeCaller(tenant.id, account.id, origin, "wake", OWNER),
                session.id,
                command="python next.py",
                wait_seconds=0,
                timeout_seconds=None,
                timeout_minutes=60,
                request_key="wake-run",
            )
            seen["run"] = "allowed"
        except StoreError as refusal:
            seen["run"] = str(refusal)
        return SimpleNamespace(
            state=SimpleNamespace(error=None, termination=TerminationReason.COMPLETED, content=[]),
            ma_session_id="sesn_wake",
            mapping_id=None,
            outputs=(),
        )

    admission = admission_for(tenant, account.id, "ag_target")
    monkeypatch.setattr(direct_messages, "admit", AsyncMock(return_value=admission))
    monkeypatch.setattr(
        direct_messages,
        "bind_session",
        AsyncMock(return_value=SimpleNamespace(admission=admission, backend=None)),
    )
    monkeypatch.setattr(direct_messages, "run_prepared_turn", turn)
    deps = deps_for(None, sm)

    await dispatch_scope_timer(
        sm, row, run_timer=lambda row, seed: _RUNNERS[place](deps, row, seed)
    )

    async with sm() as db:
        settled = await get_continuation(db, idempotency_key=row.idempotency_key)
    assert settled is not None and settled.status == "delivered", "the wake's turn ran"
    # The turn is the wake's own (bound), but it stands in a fresh scope...
    assert seen["bound"] is True
    assert seen["place"] != (channel, scope)
    # ...so the session's conversation check refuses the read. Closing the gap fails here.
    assert seen["read"] == ELSEWHERE
    # The same check refuses the next command, in the words a turn with nobody here gets.
    assert seen["run"] == "The machine owner must be here in an active conversation."
