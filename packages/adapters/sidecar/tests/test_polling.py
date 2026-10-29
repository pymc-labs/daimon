"""Real transaction/lease tests for deterministic watch polling."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.sidecar.main import poll_once
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.continuity.poll_tools import PollTool, require_poll_tool, validate_poll
from daimon.core.stores import watches
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.testing.factories import make_tenant
from daimon.testing.watch_fixtures import install_poll_ports
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.fresh_schema


@pytest.fixture
async def poll_watch(db_session_factory, monkeypatch):
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    now = datetime.now(UTC)
    async with db_session_factory.begin() as db:
        await install_poll_ports(db)
        await watches.set_watch_control(db, scope="global", enabled=True)
        tenant = await make_tenant(db)
        principal = await get_or_create_platform_principal(
            db, tenant_id=tenant.id, platform="discord", external_id="123"
        )
        await create_thread_session(
            db,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="4242",
            channel_id="4241",
            account_id=principal.account_id,
            ma_session_id="session-bound",
            ma_agent_id="agent-bound",
        )
        binding = ContinuationRequest(
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="4241",
            thread_id="4242",
            requester_account_id=principal.account_id,
            requester_external_user_id="123",
            target_ma_agent_id="agent-bound",
            target_name="analyst",
            requested_work=None,
            reason="watch_fired",
            idempotency_key=uuid.uuid4(),
        )
        watch = await watches.register_poll_watch(
            db,
            binding,
            instructions="Report changed rows.",
            tool="read_thread",
            arguments={"thread_id": "4343"},
            projection=".rows",
            rule="change",
            interval=60,
            expires_at=now + timedelta(days=1),
            now=now,
        )
    return watch, binding


async def claim(factory, watch_id):
    async with factory.begin() as db:
        await db.execute(
            text("UPDATE watches SET next_check_at=now()-interval '1 second' WHERE id=:id"),
            {"id": watch_id},
        )
        items = await watches.claim_poll_watches(db)
        assert len(items) == 1
        return items[0]["lease"]


async def result(factory, watch, lease, value, payload=None):
    async with factory.begin() as db:
        return await watches.finish_poll(db, watch, lease, value=value, payload=payload)


async def test_baseline_evidence_and_wake_commit_atomically(db_session_factory, poll_watch):
    watch, _ = poll_watch
    lease = await claim(db_session_factory, watch.id)
    assert await result(db_session_factory, watch, lease, [], None) is None
    lease = await claim(db_session_factory, watch.id)
    with pytest.raises(RuntimeError):
        async with db_session_factory.begin() as db:
            await watches.finish_poll(db, watch, lease, value=[1], payload="[1]")
            raise RuntimeError("simulated process crash before commit")
    async with db_session_factory() as db:
        current = await watches.get_watch(db, watch.id)
        assert current.baseline == {"value": []}
        assert await watches.list_evidence(db, watch.id) == []
    wake = await result(db_session_factory, watch, lease, [1], "[1]")
    async with db_session_factory() as db:
        current = await watches.get_watch(db, watch.id)
        row = await get_continuation(db, idempotency_key=wake)
        assert current.baseline == {"value": [1]}
        assert row.reason == "watch_fired"
        assert row.requester_account_id == watch.owner_account_id
        assert row.thread_id == watch.thread_id
        assert await watches.watch_seed(db, row, now=datetime.now(UTC))
    with pytest.raises(DBAPIError):
        await result(db_session_factory, watch, lease, [1], "[1]")


async def test_leases_are_separate_exclusive_and_expire(db_session_factory, poll_watch):
    watch, _ = poll_watch
    first = await claim(db_session_factory, watch.id)
    async with db_session_factory.begin() as db:
        assert await watches.claim_poll_watches(db) == []
        await db.execute(
            text("UPDATE watches SET lease_expires_at=now()-interval '1 second' WHERE id=:id"),
            {"id": watch.id},
        )
    second = await claim(db_session_factory, watch.id)
    assert first != second
    with pytest.raises(DBAPIError):
        await result(db_session_factory, watch, first, 1, "1")
    assert await result(db_session_factory, watch, second, 1, "1")


async def test_poll_burst_retains_twenty_and_omitted_count(db_session_factory, poll_watch):
    watch, _ = poll_watch
    keys = []
    for n in range(25):
        lease = await claim(db_session_factory, watch.id)
        keys.append(
            await result(
                db_session_factory,
                watch,
                lease,
                n,
                json.dumps({"owner": "evil", "agent": "evil", "instructions": "write", "n": n}),
            )
        )
    assert len(set(keys)) == 1
    async with db_session_factory() as db:
        firing = await watches.get_firing(db, keys[0])
        wake = await get_continuation(db, idempotency_key=keys[0])
        assert len(firing.events) == 20 and firing.omitted == 5
        assert wake.thread_id == watch.thread_id and wake.target_ma_agent_id == watch.agent_id
        assert wake.requester_account_id == watch.owner_account_id
        seed = await watches.watch_seed(db, wake, now=datetime.now(UTC))
        assert seed.startswith(watch.instructions) and 'trust="untrusted"' in seed


async def test_gate_off_prevents_poll_reads_claims_and_sql(
    db_session_factory, poll_watch, monkeypatch
):
    watch, _ = poll_watch
    lease = await claim(db_session_factory, watch.id)
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "false")
    http = AsyncMock()
    assert await poll_once(db_session_factory, http, origin="http://mcp:8765") == 0
    http.post.assert_not_called()
    with pytest.raises(ValueError, match="disabled"):
        await result(db_session_factory, watch, lease, 1, "1")
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    async with db_session_factory.begin() as db:
        await watches.set_watch_control(db, scope="global", enabled=False)
        assert await watches.claim_poll_watches(db) == []
    with pytest.raises(DBAPIError):
        await result(db_session_factory, watch, lease, 1, "1")


async def test_poll_error_backoff_and_access_revocation_cancel_queued_wakes(
    db_session_factory, poll_watch
):
    watch, _ = poll_watch
    lease = await claim(db_session_factory, watch.id)
    wake = await result(db_session_factory, watch, lease, 1, "1")
    lease = await claim(db_session_factory, watch.id)
    now = datetime.now(UTC)
    async with db_session_factory.begin() as db:
        await watches.poll_failed(db, watch.id, lease, revoked=False, now=now)
        current = await watches.get_watch(db, watch.id)
        assert current.consecutive_errors == 1
        assert current.backoff_until == now + timedelta(seconds=120)
        assert await watches.claim_poll_watches(db) == []
        await db.execute(
            text("UPDATE watches SET backoff_until=NULL WHERE id=:id"), {"id": watch.id}
        )
    lease = await claim(db_session_factory, watch.id)
    async with db_session_factory.begin() as db:
        await watches.poll_failed(db, watch.id, lease, revoked=True, now=now)
        current = await watches.get_watch(db, watch.id)
        row = await get_continuation(db, idempotency_key=wake)
        assert current.status == "paused" and row.status == "cancelled"
        assert await watches.claim_poll_watches(db) == []


async def test_expired_watch_stops_polling_and_cancels_wake(db_session_factory, poll_watch):
    watch, _ = poll_watch
    lease = await claim(db_session_factory, watch.id)
    wake = await result(db_session_factory, watch, lease, 1, "1")
    async with db_session_factory.begin() as db:
        await db.execute(
            text("UPDATE watches SET expires_at=now()-interval '1 second' WHERE id=:id"),
            {"id": watch.id},
        )
        assert await watches.claim_poll_watches(db) == []
        assert (await watches.get_watch(db, watch.id)).status == "expired"
        assert (await get_continuation(db, idempotency_key=wake)).status == "cancelled"


@pytest.mark.parametrize(
    "name", ["send_message", "create_watch", "http_get", "artifact_db_query", "shell"]
)
def test_unreviewed_and_nonread_tools_cannot_enroll(name):
    with pytest.raises(ValueError):
        require_poll_tool(name)


@pytest.mark.parametrize("effect,no_watch", [("write", False), ("read", True)])
def test_registry_respects_read_and_no_watch(monkeypatch, effect, no_watch):
    import daimon.core.continuity.poll_tools as registry

    monkeypatch.setattr(registry, "POLL_TOOLS", {"unsafe": PollTool(effect, no_watch, frozenset())})
    with pytest.raises(ValueError):
        require_poll_tool("unsafe")


@pytest.mark.parametrize(
    "arguments",
    [
        {"thread_id": "https://outside"},
        {"thread_id": "4242", "url": "https://outside"},
        {"thread_id": "4242", "limit": 100},
    ],
)
def test_poll_arguments_have_no_url_or_unbounded_read(arguments):
    with pytest.raises(ValueError):
        validate_poll("read_thread", arguments, projection=".", rule="change", interval=60)


@pytest.mark.parametrize("cap", ["fires", "watch_daily", "owner_daily"])
async def test_poll_firing_caps_keep_baseline_and_evidence_unchanged(
    db_session_factory, poll_watch, cap
):
    watch, binding = poll_watch
    token = await claim(db_session_factory, watch.id)
    key = await result(db_session_factory, watch, token, 1, "1")
    token = await claim(db_session_factory, watch.id)
    async with db_session_factory.begin() as db:
        await db.execute(
            text("UPDATE task_continuations SET status='delivered' WHERE idempotency_key=:key"),
            {"key": key},
        )
        if cap == "fires":
            await db.execute(text("UPDATE watches SET max_fires=1 WHERE id=:id"), {"id": watch.id})
        elif cap == "watch_daily":
            await db.execute(
                text("UPDATE watches SET max_turns_per_day=1 WHERE id=:id"), {"id": watch.id}
            )
        else:
            other = await watches.register_poll_watch(
                db,
                binding,
                instructions="Another owner watch.",
                tool="read_thread",
                arguments={"thread_id": "4343"},
                projection=".",
                rule="change",
                interval=60,
                expires_at=watch.expires_at,
                now=datetime.now(UTC),
            )
            await watches.pause_watch(db, other.id)
            # Existing owner firings on another watch still count toward the owner cap.
            await db.execute(
                text("""WITH generated AS (
              SELECT gen_random_uuid() k FROM generate_series(1,99)
            ), queued AS (
              INSERT INTO task_continuations(tenant_id,platform,parent_channel_id,thread_id,
                requester_account_id,requester_external_user_id,target_ma_agent_id,target_name,
                reason,idempotency_key,status)
              SELECT w.tenant_id,w.platform,w.parent_channel_id,w.thread_id,w.owner_account_id,
                w.owner_external_id,w.agent_id,w.agent_name,'watch_fired',g.k,'delivered'
              FROM watches w CROSS JOIN generated g WHERE w.id=:id RETURNING idempotency_key
            ) INSERT INTO watch_firings(wake_key,watch_id,revision,generation)
              SELECT idempotency_key,:id,1,0 FROM queued"""),
                {"id": other.id},
            )
    with pytest.raises(DBAPIError) as refused:
        await result(db_session_factory, watch, token, 2, "2")
    expected = {
        "fires": "poll binding refused",
        "watch_daily": "watch daily cap",
        "owner_daily": "owner daily cap",
    }[cap]
    assert expected in str(refused.value.orig)
    async with db_session_factory() as db:
        assert (await watches.get_watch(db, watch.id)).baseline == {"value": 1}
        assert len(await watches.list_evidence(db, watch.id)) == 1


async def test_parallel_sidecars_only_one_claims_each_poll(db_session_factory, poll_watch):
    import asyncio

    watch, _ = poll_watch
    from sqlalchemy.ext.asyncio import async_sessionmaker

    independent = async_sessionmaker(db_session_factory.kw["bind"].engine, expire_on_commit=False)

    async def read():
        async with independent.begin() as db:
            return await watches.claim_poll_watches(db)

    first, second = await asyncio.gather(read(), read())
    assert sorted([len(first), len(second)]) == [0, 1]
    assert (first or second)[0]["watch_id"] == str(watch.id)


async def test_max_fires_stops_new_polls_but_final_queued_wake_still_runs(
    db_session_factory, poll_watch
):
    watch, _ = poll_watch
    async with db_session_factory.begin() as db:
        await db.execute(text("UPDATE watches SET max_fires=1 WHERE id=:id"), {"id": watch.id})
    token = await claim(db_session_factory, watch.id)
    key = await result(db_session_factory, watch, token, 1, "1")
    async with db_session_factory.begin() as db:
        assert await watches.claim_poll_watches(db) == []
        row = await watches.get_watch(db, watch.id)
        assert row.next_check_at is None
        queued = await get_continuation(db, idempotency_key=key)
        assert await watches.watch_seed(db, queued, now=datetime.now(UTC))


async def test_baseline_commit_rejects_revoked_owner_binding(db_session_factory, poll_watch):
    watch, _ = poll_watch
    token = await claim(db_session_factory, watch.id)
    async with db_session_factory.begin() as db:
        await db.execute(
            text("DELETE FROM platform_principals WHERE account_id=:owner"),
            {"owner": watch.owner_account_id},
        )
    with pytest.raises(DBAPIError):
        await result(db_session_factory, watch, token, 1)
    async with db_session_factory() as db:
        assert (await watches.get_watch(db, watch.id)).baseline == {}


async def test_poll_transport_requires_private_secret_and_origin(db_session_factory, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    http = MagicMock()
    http.post.return_value.__aenter__.return_value = MagicMock()
    claim = AsyncMock(return_value=[{"watch_id": "watch", "lease": "opaque-lease"}])
    monkeypatch.setattr("daimon.adapters.sidecar.main.claim_poll_watches", claim)
    assert await poll_once(db_session_factory, http, origin="http://watch-executor:8767") == 0
    claim.assert_not_called()
    with pytest.raises(ValueError, match="private OS service"):
        await poll_once(
            db_session_factory, http, origin="http://mcp:8765", executor_secret="s" * 32
        )
    assert (
        await poll_once(
            db_session_factory, http, origin="http://watch-executor:8767", executor_secret="s" * 32
        )
        == 1
    )
    http.post.assert_called_once_with(
        "http://watch-executor:8767/internal/watches/watch/poll",
        headers={"X-Watch-Lease": "opaque-lease", "X-Watch-Executor-Key": "s" * 32},
        allow_redirects=False,
    )


async def test_invalid_poll_failure_never_takes_global_firing_lock(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    session = AsyncMock()
    watch_id = uuid.uuid4()
    monkeypatch.setattr(
        watches, "get_watch", AsyncMock(return_value=SimpleNamespace(lease_owner="real"))
    )
    await watches.poll_failed(session, watch_id, "junk", revoked=True, now=datetime.now(UTC))
    session.execute.assert_not_called()


async def test_old_lease_release_cannot_clear_new_poll_claim(db_session_factory, poll_watch):
    watch, _ = poll_watch
    token = await claim(db_session_factory, watch.id)
    replacement = str(uuid.uuid4())
    async with db_session_factory.begin() as db:
        await db.execute(
            text("UPDATE watches SET lease_owner=:replacement WHERE id=:id"),
            {"replacement": replacement, "id": watch.id},
        )
        await watches.release_poll_lease(db, watch.id, token)
    async with db_session_factory() as db:
        assert (await watches.get_watch(db, watch.id)).lease_owner == replacement


async def fail_once(factory, watch, *, errors_before, failing_for, now):
    """One more poll error after `errors_before` errors that began `failing_for` ago."""
    async with factory.begin() as db:
        await db.execute(
            text(
                "UPDATE watches SET consecutive_errors=:errors, backoff_until=NULL,"
                " source_state=source_state || jsonb_build_object('failing_since', CAST(:since AS text))"
                " WHERE id=:id"
            ),
            {"errors": errors_before, "since": (now - failing_for).isoformat(), "id": watch.id},
        )
    lease = await claim(factory, watch.id)
    async with factory.begin() as db:
        await watches.poll_failed(db, watch.id, lease, revoked=False, now=now)
        return await watches.get_watch(db, watch.id)


@pytest.mark.parametrize(
    ("errors", "failing_for", "stopped"),
    [
        (4, timedelta(days=3), False),
        (5, timedelta(hours=23, minutes=59), False),
        (5, timedelta(hours=24), True),
        (9, timedelta(days=2), True),
    ],
)
async def test_a_poll_stops_after_five_errors_spanning_a_day(
    db_session_factory, poll_watch, errors, failing_for, stopped
):
    watch, _ = poll_watch
    now = datetime.now(UTC)
    current = await fail_once(
        db_session_factory, watch, errors_before=errors - 1, failing_for=failing_for, now=now
    )
    assert current.consecutive_errors == errors
    if stopped:
        assert current.status == "paused" and current.source_state["reason"] == "failed"
        assert current.lease_owner is None
    else:
        assert current.status == "active" and current.source_state.get("reason") is None
        assert current.backoff_until is not None and current.backoff_until > now


async def test_first_error_starts_the_failing_clock_and_success_restarts_it(
    db_session_factory, poll_watch
):
    watch, _ = poll_watch
    now = datetime.now(UTC)
    lease = await claim(db_session_factory, watch.id)
    async with db_session_factory.begin() as db:
        await watches.poll_failed(db, watch.id, lease, revoked=False, now=now)
        assert (await watches.get_watch(db, watch.id)).source_state["failing_since"] == (
            now.isoformat()
        )
        await db.execute(
            text("UPDATE watches SET backoff_until=NULL WHERE id=:id"), {"id": watch.id}
        )
    # A success clears the count; the next error restarts the clock instead of
    # inheriting the old start.
    lease = await claim(db_session_factory, watch.id)
    await result(db_session_factory, watch, lease, [], None)
    later = now + timedelta(days=2)
    lease = await claim(db_session_factory, watch.id)
    async with db_session_factory.begin() as db:
        await watches.poll_failed(db, watch.id, lease, revoked=False, now=later)
        current = await watches.get_watch(db, watch.id)
    assert current.consecutive_errors == 1 and current.status == "active"
    assert current.source_state["failing_since"] == later.isoformat()


async def test_a_stopped_watch_cancels_wakes_notifies_once_and_resumes_clean(
    db_session_factory, poll_watch
):
    from daimon.core.continuity.watch_notices import poll_watch_notices

    watch, _ = poll_watch
    lease = await claim(db_session_factory, watch.id)
    await result(db_session_factory, watch, lease, [], None)
    lease = await claim(db_session_factory, watch.id)
    wake = await result(db_session_factory, watch, lease, [1], "[1]")
    now = datetime.now(UTC)
    current = await fail_once(
        db_session_factory, watch, errors_before=4, failing_for=timedelta(days=1), now=now
    )
    assert current.status == "paused" and current.source_state["reason"] == "failed"
    async with db_session_factory() as db:
        assert (await get_continuation(db, idempotency_key=wake)).status == "cancelled"
        assert await watches.claim_poll_watches(db) == []

    deliver = AsyncMock(return_value=True)
    for _ in range(3):
        await poll_watch_notices(db_session_factory, deliver=deliver)
    deliver.assert_awaited_once()
    recipient, _, message = deliver.await_args.args
    assert recipient.owner_external_id == watch.owner_external_id
    assert watch.instructions in message and "stopped" in message and "resume" in message

    async with db_session_factory.begin() as db:
        await watches.resume_watch(db, watch.id, now=now)
        current = await watches.get_watch(db, watch.id)
    assert current.status == "active" and current.consecutive_errors == 0
    assert current.source_state.get("failing_since") is None
    # After a resume one error is only a backoff again, and no second notice goes out.
    current = await fail_once(
        db_session_factory, watch, errors_before=0, failing_for=timedelta(0), now=now
    )
    assert current.status == "active" and current.consecutive_errors == 1
    await poll_watch_notices(db_session_factory, deliver=deliver)
    deliver.assert_awaited_once()


async def test_daily_cap_notice_goes_out_once_per_rolling_day(db_session_factory, poll_watch):
    from daimon.core.continuity.watch_notices import poll_watch_notices

    watch, _ = poll_watch
    deliver = AsyncMock(return_value=True)

    async def capped(now, *, daily_cap="watch"):
        lease = await claim(db_session_factory, watch.id)
        async with db_session_factory.begin() as db:
            await watches.poll_failed(
                db, watch.id, lease, revoked=False, now=now, capped=True, daily_cap=daily_cap
            )
            return await watches.get_watch(db, watch.id)

    now = datetime.now(UTC)
    # Rate and quota limits stay silent.
    await capped(now, daily_cap=None)
    await poll_watch_notices(db_session_factory, deliver=deliver)
    deliver.assert_not_awaited()

    current = await capped(now)
    assert current.status == "active" and current.consecutive_errors == 0
    for _ in range(2):
        await poll_watch_notices(db_session_factory, deliver=deliver)
    deliver.assert_awaited_once()
    _, _, message = deliver.await_args.args
    assert watch.instructions in message and "daily limit" in message

    # Later ticks in the same window, including after a restart re-claims the poll,
    # send nothing more.
    await capped(now + timedelta(hours=23))
    await poll_watch_notices(db_session_factory, deliver=deliver)
    deliver.assert_awaited_once()

    # Once the window rolls, a new cap gets one new notice.
    current = await capped(now + timedelta(hours=24))
    assert current.status == "active"
    for _ in range(2):
        await poll_watch_notices(db_session_factory, deliver=deliver)
    assert deliver.await_count == 2


async def test_undelivered_daily_cap_notice_retries_without_repeating(
    db_session_factory, poll_watch
):
    from daimon.core.continuity.watch_notices import poll_watch_notices

    watch, _ = poll_watch
    now = datetime.now(UTC)
    lease = await claim(db_session_factory, watch.id)
    async with db_session_factory.begin() as db:
        await watches.poll_failed(
            db, watch.id, lease, revoked=False, now=now, capped=True, daily_cap="watch"
        )
    failing = AsyncMock(return_value=False)
    await poll_watch_notices(db_session_factory, deliver=failing)
    failing.assert_awaited_once()
    async with db_session_factory.begin() as db:
        await db.execute(
            text(
                "UPDATE watches SET source_state=source_state || "
                "'{\"notice_lease_until\": null}'::jsonb WHERE id=:id"
            ),
            {"id": watch.id},
        )
    deliver = AsyncMock(return_value=True)
    for _ in range(2):
        await poll_watch_notices(db_session_factory, deliver=deliver)
    deliver.assert_awaited_once()


async def test_capped_polls_end_a_run_of_errors(db_session_factory, poll_watch):
    watch, _ = poll_watch
    start = datetime.now(UTC)
    current = await fail_once(
        db_session_factory, watch, errors_before=3, failing_for=timedelta(0), now=start
    )
    assert current.consecutive_errors == 4
    async with db_session_factory.begin() as db:
        await db.execute(
            text("UPDATE watches SET backoff_until=NULL WHERE id=:id"), {"id": watch.id}
        )
    # The source recovers, but a day of capped polls re-detects the same held-back change.
    for hours in range(1, 25):
        lease = await claim(db_session_factory, watch.id)
        async with db_session_factory.begin() as db:
            await watches.poll_failed(
                db,
                watch.id,
                lease,
                revoked=False,
                now=start + timedelta(hours=hours),
                capped=True,
                daily_cap="watch",
            )
    async with db_session_factory() as db:
        current = await watches.get_watch(db, watch.id)
    assert current.consecutive_errors == 0
    assert current.source_state.get("failing_since") is None
    # One transient error after that is a fresh backoff, not a stop.
    later = start + timedelta(hours=25)
    lease = await claim(db_session_factory, watch.id)
    async with db_session_factory.begin() as db:
        await watches.poll_failed(db, watch.id, lease, revoked=False, now=later)
        current = await watches.get_watch(db, watch.id)
    assert current.status == "active" and current.consecutive_errors == 1
    assert current.source_state["failing_since"] == later.isoformat()


async def test_owner_cap_notice_goes_out_once_per_owner_per_rolling_day(
    db_session_factory, poll_watch
):
    from daimon.core.continuity.watch_notices import poll_watch_notices

    watch, binding = poll_watch
    async with db_session_factory.begin() as db:
        other = await watches.register_poll_watch(
            db,
            binding,
            instructions="Another owner watch.",
            tool="read_thread",
            arguments={"thread_id": "4343"},
            projection=".",
            rule="change",
            interval=60,
            expires_at=watch.expires_at,
            now=datetime.now(UTC),
        )

    async def capped(target, now, kind):
        async with db_session_factory.begin() as db:
            await db.execute(
                text(
                    "UPDATE watches SET next_check_at = CASE WHEN id=:id"
                    " THEN now()-interval '1 second' ELSE now()+interval '1 day' END"
                    " WHERE id IN (:id, :other_id)"
                ),
                {"id": target, "other_id": other.id if target == watch.id else watch.id},
            )
            (item,) = await watches.claim_poll_watches(db)
            assert item["watch_id"] == str(target)
        async with db_session_factory.begin() as db:
            await watches.poll_failed(
                db, target, item["lease"], revoked=False, now=now, capped=True, daily_cap=kind
            )

    deliver = AsyncMock(return_value=True)

    async def drain():
        for _ in range(2):
            await poll_watch_notices(db_session_factory, deliver=deliver)

    now = datetime.now(UTC)
    await capped(watch.id, now, "owner")
    await capped(other.id, now + timedelta(hours=1), "owner")
    await drain()
    deliver.assert_awaited_once()
    recipient, _, message = deliver.await_args.args
    assert recipient.owner_external_id == watch.owner_external_id
    assert "across all of them" in message

    # A watch's own cap is a different limit, so it still gets its own notice.
    await capped(other.id, now + timedelta(hours=2), "watch")
    await drain()
    assert deliver.await_count == 2
    _, _, message = deliver.await_args.args
    assert "Another owner watch." in message and "across all of them" not in message

    # Once the owner's window rolls, the next owner cap gets one new notice.
    await capped(other.id, now + timedelta(hours=24, minutes=1), "owner")
    await drain()
    assert deliver.await_count == 3
