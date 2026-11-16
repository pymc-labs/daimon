"""Secret-link callbacks: a bearer token, stored only as a hash, over real Postgres and HTTP."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from aiohttp.test_utils import TestClient, TestServer
from daimon.adapters.sidecar.main import create_app
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.continuity.watch_imports import (
    ImportedCallbackWatch,
    ImportedTimer,
    WatchExport,
)
from daimon.core.stores import tenant_ledger, watches
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.stores.held_timers import (
    EMPTY_NOTE_PROMPT,
    cancel_missing_timers,
    import_held_timer,
    settle_held_timers,
    unpause_timers,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.testing.factories import make_tenant
from daimon.testing.watch_fixtures import install_poll_ports
from pydantic import PostgresDsn, SecretStr, ValidationError
from sqlalchemy import text

pytestmark = pytest.mark.fresh_schema

OLD_TOKEN = "old-link-token-" + "cd" * 16


async def _tenant(db):
    await install_poll_ports(db)
    await watches.set_watch_control(db, scope="global", enabled=True)
    tenant = await make_tenant(db)
    await tenant_ledger.insert_entry(
        db,
        tenant_id=tenant.id,
        delta_usd=Decimal(100),
        reason="trial",
        idempotency_key=f"link-test:{tenant.id}",
    )
    return tenant


@pytest.fixture
async def link(db_session_factory, monkeypatch):
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    now = datetime.now(UTC)
    async with db_session_factory.begin() as db:
        tenant = await _tenant(db)
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
        watch, token = await watches.register_callback_watch(
            db,
            binding,
            instructions="Summarize the posted event.",
            lifetime=timedelta(days=1),
            now=now,
        )
    return watch, token


async def post(client, watch_id, token, body=b"hello", **kw):
    return await client.post(f"/callbacks/watches/{watch_id}/{token}", data=body, **kw)


async def count(factory, table):
    async with factory() as db:
        return await db.scalar(text(f"SELECT count(*) FROM {table}"))


async def test_good_token_wakes_once_per_event_and_stores_only_the_hash(db_session_factory, link):
    watch, token = link
    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        response = await post(client, watch.id, token, b"payload one")
        assert response.status == 202 and (await response.json()) == {"accepted": True}
        assert (await post(client, watch.id, token, b"payload two")).status == 202
    async with db_session_factory() as db:
        evidence = await watches.list_evidence(db, watch.id)
        assert len(evidence) == 2
        wake = await get_continuation(db, idempotency_key=evidence[0].wake_key)
        assert wake.reason == "watch_fired" and wake.thread_id == "4242"
        seed = await watches.watch_seed(db, wake, now=datetime.now(UTC))
        assert seed is not None and seed.startswith(watch.instructions)
        assert 'trust="untrusted"' in seed
        digests = list(await db.scalars(text("SELECT digest FROM watch_capabilities")))
        assert digests == [hashlib.sha256(token.encode()).hexdigest()]
        dump = json.dumps(
            [list(row) for row in await db.execute(text("SELECT w::text FROM watches w")) if True]
        )
        assert token not in dump and token not in str(evidence)


@pytest.mark.parametrize("which", ["wrong", "short", "bad_chars", "other_watch", "unknown"])
async def test_bad_links_are_one_uniform_403(db_session_factory, link, which):
    watch, token = link
    target = watch.id
    candidate = {
        "wrong": "A" * len(token),
        "short": token[:8],
        "bad_chars": token[:-1] + "!",
        "other_watch": token,
        "unknown": token,
    }[which]
    if which in ("other_watch", "unknown"):
        target = uuid.uuid4()
    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        assert (await post(client, target, candidate)).status == 403
    assert await count(db_session_factory, "watch_inbox") == 0
    assert await count(db_session_factory, "task_continuations") == 0


async def test_non_uuid_watch_is_404_and_limits_hold(db_session_factory, link):
    watch, token = link
    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        assert (await post(client, "not-a-uuid", token)).status == 404
        assert (await post(client, watch.id, token, b"x" * 4097)).status == 413
        assert (await post(client, watch.id, token, b"\xff\xfe")).status == 400
        assert (await post(client, watch.id, token, b"")).status == 400
        assert (await post(client, watch.id, token, b"x" * 4096)).status == 202
    assert await count(db_session_factory, "watch_inbox") == 1


async def test_in_process_rate_limit_and_gate(db_session_factory, link, monkeypatch):
    watch, token = link
    async with TestClient(
        TestServer(create_app(db_session_factory, max_requests_per_minute=2))
    ) as client:
        assert (await post(client, watch.id, token)).status == 202
        assert (await post(client, watch.id, token)).status == 202
        assert (await post(client, watch.id, token)).status == 429
    monkeypatch.delenv("DAIMON_WATCHES__ENABLED")
    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        assert (await post(client, watch.id, token)).status == 503


async def test_stopped_or_expired_watches_refuse(db_session_factory, link):
    watch, token = link
    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        async with db_session_factory.begin() as db:
            await db.execute(
                text("UPDATE watches SET status='paused', paused_at=now() WHERE id=:i"),
                {"i": watch.id},
            )
        assert (await post(client, watch.id, token)).status == 403
        async with db_session_factory.begin() as db:
            await db.execute(
                text("UPDATE watches SET status='active', paused_at=NULL WHERE id=:i"),
                {"i": watch.id},
            )
            await db.execute(
                text("UPDATE watch_capabilities SET revoked_at=now() WHERE watch_id=:i"),
                {"i": watch.id},
            )
        assert (await post(client, watch.id, token)).status == 403
    assert await count(db_session_factory, "watch_inbox") == 0


async def test_only_an_admin_makes_a_page_watch_permanent(db_session_factory, link):
    watch, _ = link
    assert watch.expires_at is not None
    async with db_session_factory.begin() as db:
        binding = ContinuationRequest(
            tenant_id=watch.tenant_id,
            platform="discord",
            parent_channel_id="4241",
            thread_id="4242",
            requester_account_id=watch.owner_account_id,
            requester_external_user_id="123",
            target_ma_agent_id="agent-bound",
            target_name="analyst",
            requested_work=None,
            reason="watch_fired",
            idempotency_key=uuid.uuid4(),
        )
        with pytest.raises(ValueError, match="permanent"):
            await watches.register_page_watch(
                db,
                binding,
                instructions="x",
                page_id=uuid.uuid4(),
                expires_at=None,
                now=datetime.now(UTC),
                source="page_event",
                permanent_approved_by=watch.owner_account_id,
            )


def _manifest_watch(**kw):
    base = {
        "legacy_id": "11111111-1111-1111-1111-111111111111",
        "source": "callback",
        "instructions": "Report each event.",
        "token_sha256": hashlib.sha256(OLD_TOKEN.encode()).hexdigest(),
        "expires_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
        "owner_discord_id": "555",
        "channel_id": "900",
        "thread_id": None,
        "place_type": "channel",
    }
    return {**base, **kw}


GUILD = "777000111"


def _export(**kw):
    return WatchExport.model_validate({"version": 1, "guild_id": GUILD, "watches": [], **kw})


def test_manifest_accepts_callbacks_and_timers_and_rejects_unknown_fields():
    manifest = WatchExport.model_validate(
        {
            "version": 1,
            "guild_id": GUILD,
            "watches": [_manifest_watch(expires_at=None)],
            "timers": [
                {
                    "source_id": "t1",
                    "owner_discord_id": "555",
                    "channel_id": "900",
                    "thread_id": None,
                    "place_type": "direct-message",
                    "fire_at": "2030-01-01T00:00:00Z",
                    "note": "remind me",
                }
            ],
        }
    )
    assert isinstance(manifest.watches[0], ImportedCallbackWatch)
    assert manifest.watches[0].expires_at is None and len(manifest.timers) == 1
    assert _export().timers == []
    with pytest.raises(ValidationError):
        _export(watches=[_manifest_watch(extra=1)])
    with pytest.raises(ValidationError):
        _export(watches=[_manifest_watch(token_sha256="AB" * 32)])
    with pytest.raises(ValidationError, match="guild_id"):
        WatchExport.model_validate({"version": 1, "watches": []})


@pytest.fixture
async def moved(db_session_factory, monkeypatch):
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    async with db_session_factory.begin() as db:
        tenant = await _tenant(db)
    return tenant


async def _import(factory, tenant, **kw):
    definition = ImportedCallbackWatch.model_validate(_manifest_watch(**kw))
    async with factory.begin() as db:
        return await watches.import_callback_watch(
            db,
            definition,
            tenant_id=tenant.id,
            agent_id="agent-new",
            agent_name="analyst",
            now=datetime.now(UTC),
        )


async def test_import_keeps_the_old_token_and_mints_a_new_one_held_until_confirmed(
    db_session_factory, moved
):
    item = await _import(db_session_factory, moved)
    assert item.outcome == "imported" and item.token is not None and item.watch is not None
    watch = item.watch
    assert watch.status == "paused" and watch.delivery == "dm" and watch.session_id is None
    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        # Held: a live link answers a retryable 503 until an operator confirms the
        # destination; an unknown one learns nothing.
        for token in (item.token, OLD_TOKEN):
            held = await post(client, watch.id, token)
            assert held.status == 503 and held.headers["Retry-After"] == "300"
        assert (await post(client, watch.id, "z" * 40)).status == 403
        assert await count(db_session_factory, "watch_inbox") == 0
        async with db_session_factory.begin() as db:
            await watches.confirm_import(
                db,
                watch.id,
                tenant_id=moved.id,
                owner_account_id=watch.owner_account_id,
                agent_id="agent-new",
                parent_channel_id="900",
                thread_id="900",
                now=datetime.now(UTC),
            )
        assert (await post(client, watch.id, item.token)).status == 202
        assert (await post(client, watch.id, OLD_TOKEN)).status == 202
        assert (await post(client, watch.id, "z" * 40)).status == 403
    async with db_session_factory() as db:
        rows = list(
            await db.execute(
                text("SELECT digest, key_id FROM watch_capabilities ORDER BY generation")
            )
        )
        assert [r[0] for r in rows] == [
            hashlib.sha256(OLD_TOKEN.encode()).hexdigest(),
            hashlib.sha256(item.token.encode()).hexdigest(),
        ]
        wake = await get_continuation(
            db, idempotency_key=(await watches.list_evidence(db, watch.id))[0].wake_key
        )
        assert wake.reason == "watch_report"


async def test_reimport_adds_nothing_and_issues_no_second_link(db_session_factory, moved):
    first = await _import(db_session_factory, moved)
    again = await _import(db_session_factory, moved)
    assert again.outcome == "existing" and again.token is None
    assert again.watch is not None and first.watch is not None and again.watch.id == first.watch.id
    assert await count(db_session_factory, "watch_capabilities") == 2


async def test_import_caps_permanent_and_skips_expired(db_session_factory, moved):
    permanent = await _import(db_session_factory, moved, expires_at=None)
    assert permanent.capped and permanent.watch is not None
    assert permanent.watch.expires_at is not None
    assert permanent.watch.expires_at <= datetime.now(UTC) + timedelta(days=30, minutes=1)
    long = await _import(
        db_session_factory,
        moved,
        legacy_id="22222222-2222-2222-2222-222222222222",
        expires_at=(datetime.now(UTC) + timedelta(days=90)).isoformat(),
    )
    assert long.capped
    gone = await _import(
        db_session_factory,
        moved,
        legacy_id="33333333-3333-3333-3333-333333333333",
        expires_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    assert gone.outcome == "expired" and gone.watch is None and gone.token is None


def _timer(source_id, *, minutes=60, place="thread", owner="555", note="ping", **kw):
    return ImportedTimer(
        source_id=source_id,
        owner_discord_id=owner,
        channel_id="900",
        thread_id="901" if place == "thread" else None,
        place_type=place,
        fire_at=datetime.now(UTC) + timedelta(minutes=minutes),
        note=note,
        **kw,
    )


async def _pending_timers(factory):
    async with factory() as db:
        return list(
            await db.execute(
                text(
                    "SELECT parent_channel_id, thread_id, requested_work FROM task_continuations "
                    "WHERE reason='timer' ORDER BY requested_work"
                )
            )
        )


async def test_held_timers_do_not_fire_until_confirmed_and_settle_once(db_session_factory, moved):
    now = datetime.now(UTC)
    first = _timer("a", note="later")
    async with db_session_factory.begin() as db:
        results = [
            (await import_held_timer(db, t, tenant_id=moved.id, now=now)).outcome
            for t in (
                first,
                _timer("b", minutes=0, note="due"),
                _timer("c", place="direct-message", note="private"),
                _timer("d", place="channel", note="shared"),
                first,
            )
        ]
    assert results == ["held", "past", "held", "held", "existing"], results
    assert await _pending_timers(db_session_factory) == []
    counts = await settle_held_timers(
        db_session_factory, tenant_id=moved.id, agent_id="agent-new", agent_name="analyst", now=now
    )
    assert dict(counts) == {"scheduled": 3}, "a DM or channel timer is scheduled like any other"
    pending = await _pending_timers(db_session_factory)
    assert [p[2] for p in pending] == ["later", "private", "shared"], pending
    assert pending[2][0] == "900" and pending[2][1].startswith("ch:"), (
        "a channel timer fires in its channel under a private scope"
    )
    assert pending[0][:2] == ("900", "901"), "a thread timer wakes the thread it names"
    assert pending[1][0].startswith("dm:") and pending[1][0] == pending[1][1], (
        "a DM timer carries a private scope, never a channel id"
    )
    again = await settle_held_timers(
        db_session_factory, tenant_id=moved.id, agent_id="agent-new", agent_name="analyst", now=now
    )
    assert dict(again) == {}
    async with db_session_factory() as db:
        notes = list(await db.scalars(text("SELECT note FROM held_timers")))
    assert notes == ["", "", "", ""], "settled rows keep no note"


async def test_held_timers_stop_at_the_pending_cap(db_session_factory, moved):
    now = datetime.now(UTC)
    async with db_session_factory.begin() as db:
        for i in range(27):
            await import_held_timer(
                db, _timer(f"t{i:02}", minutes=10 + i, note=f"n{i:02}"), tenant_id=moved.id, now=now
            )
    counts = await settle_held_timers(
        db_session_factory, tenant_id=moved.id, agent_id="agent-new", agent_name="analyst", now=now
    )
    assert dict(counts) == {"scheduled": 25, "capped": 2}
    assert len(await _pending_timers(db_session_factory)) == 25


async def test_paused_import_stays_paused_when_confirmed(db_session_factory, moved):
    item = await _import(db_session_factory, moved, paused=True)
    assert item.watch is not None and item.token is not None
    watch = item.watch
    async with db_session_factory.begin() as db:
        confirmed = await watches.confirm_import(
            db,
            watch.id,
            tenant_id=moved.id,
            owner_account_id=watch.owner_account_id,
            agent_id="agent-new",
            parent_channel_id="900",
            thread_id="900",
            now=datetime.now(UTC),
        )
    assert confirmed.status == "paused"
    assert confirmed.source_state["confirmed"] is True
    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        assert (await post(client, watch.id, item.token)).status == 403
    assert await count(db_session_factory, "watch_inbox") == 0


async def test_paused_timers_are_never_scheduled(db_session_factory, moved):
    flat = _timer("paused-1", paused=True)
    now = datetime.now(UTC)
    async with db_session_factory.begin() as db:
        held = await import_held_timer(db, flat, tenant_id=moved.id, now=now)
        assert held.outcome == "held"
    counts = await settle_held_timers(
        db_session_factory, tenant_id=moved.id, agent_id="agent-new", agent_name="analyst", now=now
    )
    assert not counts
    assert len(await _pending_timers(db_session_factory)) == 0
    async with db_session_factory() as db:
        assert await db.scalar(text("SELECT settled_at IS NULL AND paused FROM held_timers"))


async def test_retried_post_is_accepted_again_without_a_second_wake(db_session_factory, link):
    watch, token = link
    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        first = await post(client, watch.id, token, b"same event")
        retry = await post(client, watch.id, token, b"same event")
        other = await post(client, watch.id, token, b"another event")
    assert [first.status, retry.status, other.status] == [202, 202, 202]
    assert await count(db_session_factory, "watch_inbox") == 2, "a retry must not add a row"
    assert await count(db_session_factory, "task_continuations") == 1, "events share one wake"


async def test_a_body_is_one_event_for_exactly_five_minutes(db_session_factory, link):
    watch, token = link

    async def age(minutes: int) -> None:
        async with db_session_factory.begin() as db:
            await db.execute(
                text("UPDATE watch_inbox SET accepted_at = now() - make_interval(mins => :m)"),
                {"m": minutes},
            )

    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        assert (await post(client, watch.id, token, b"x")).status == 202
        await age(4)
        assert (await post(client, watch.id, token, b"x")).status == 202
        assert await count(db_session_factory, "watch_inbox") == 1, "a retry inside five minutes"
        await age(6)
        assert (await post(client, watch.id, token, b"x")).status == 202
        assert await count(db_session_factory, "watch_inbox") == 2, "a new event after five"


async def test_racing_retries_of_one_body_add_one_event(db_session_factory, link):
    watch, token = link
    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        sent = await asyncio.gather(*(post(client, watch.id, token, b"twin") for _ in range(4)))
    assert [r.status for r in sent] == [202] * 4
    assert await count(db_session_factory, "watch_inbox") == 1


async def test_nul_byte_body_is_400_and_stores_nothing(db_session_factory, link):
    watch, token = link
    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        assert (await post(client, watch.id, token, b"a\x00b")).status == 400
    assert await count(db_session_factory, "watch_inbox") == 0


async def test_one_watch_cannot_use_the_shared_rate_budget(db_session_factory, link):
    watch, token = link
    other = uuid.uuid4()
    async with TestClient(
        TestServer(create_app(db_session_factory, max_watch_requests_per_minute=2))
    ) as client:
        assert (await post(client, watch.id, token, b"1")).status == 202
        assert (await post(client, watch.id, token, b"2")).status == 202
        assert (await post(client, watch.id, token, b"3")).status == 429
        assert (await post(client, other, token)).status == 403, "other watches keep their own"


async def test_a_wrong_token_is_refused_while_the_global_lock_is_held(db_session_factory, link):
    """Guesses must not queue behind the port's global lock: the digest is checked first."""
    watch, token = link
    async with db_session_factory() as holder:
        await holder.execute(text("SELECT pg_advisory_xact_lock(813749201)"))
        async with TestClient(TestServer(create_app(db_session_factory))) as client:
            refused = await asyncio.wait_for(post(client, watch.id, "A" * len(token)), timeout=5)
            assert refused.status == 403
        await holder.rollback()


async def test_callback_payloads_are_labelled_as_callbacks(db_session_factory, link):
    watch, token = link
    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        assert (await post(client, watch.id, token, b"hello")).status == 202
    async with db_session_factory() as db:
        evidence = await watches.list_evidence(db, watch.id)
        wake = await get_continuation(db, idempotency_key=evidence[0].wake_key)
        seed = await watches.watch_seed(db, wake, now=datetime.now(UTC))
    assert seed is not None and 'source="callback"' in seed, seed


async def test_expiry_counts_from_confirmation_not_import(db_session_factory, moved):
    start = datetime.now(UTC)
    item = await _import(
        db_session_factory, moved, expires_at=(start + timedelta(days=3)).isoformat()
    )
    assert item.watch is not None
    confirm_at = start + timedelta(days=2)
    async with db_session_factory.begin() as db:
        confirmed = await watches.confirm_import(
            db,
            item.watch.id,
            tenant_id=moved.id,
            owner_account_id=item.watch.owner_account_id,
            agent_id="agent-new",
            parent_channel_id="900",
            thread_id="900",
            now=confirm_at,
        )
        links = list(await db.scalars(text("SELECT expires_at FROM watch_capabilities")))
    expected = confirm_at + timedelta(days=3)
    assert confirmed.expires_at is not None
    assert abs(confirmed.expires_at - expected) < timedelta(minutes=1), confirmed.expires_at
    assert all(abs(e - expected) < timedelta(minutes=1) for e in links), links


async def test_renew_gives_thirty_days_from_now(db_session_factory, moved):
    item = await _import(db_session_factory, moved)
    assert item.watch is not None
    now = datetime.now(UTC) + timedelta(days=10)
    async with db_session_factory.begin() as db:
        renewed = await watches.renew_callback_watch(db, item.watch.id, tenant_id=moved.id, now=now)
        links = list(await db.scalars(text("SELECT expires_at FROM watch_capabilities")))
        with pytest.raises(ValueError, match="not found"):
            await watches.renew_callback_watch(db, item.watch.id, tenant_id=uuid.uuid4(), now=now)
    assert renewed.expires_at == now + timedelta(days=30), renewed.expires_at
    assert set(links) == {renewed.expires_at}, links


async def test_rotate_revokes_every_link_and_returns_one_new_token(db_session_factory, moved):
    item = await _import(db_session_factory, moved)
    assert item.watch is not None and item.token is not None
    async with db_session_factory.begin() as db:
        await watches.confirm_import(
            db,
            item.watch.id,
            tenant_id=moved.id,
            owner_account_id=item.watch.owner_account_id,
            agent_id="agent-new",
            parent_channel_id="900",
            thread_id="900",
            now=datetime.now(UTC),
        )
        fresh = await watches.rotate_callback_link(
            db, item.watch.id, tenant_id=moved.id, now=datetime.now(UTC)
        )
    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        assert (await post(client, item.watch.id, item.token)).status == 403
        assert (await post(client, item.watch.id, OLD_TOKEN)).status == 403
        assert (await post(client, item.watch.id, fresh)).status == 202
    async with db_session_factory() as db:
        live = await db.scalar(
            text("SELECT count(*) FROM watch_capabilities WHERE revoked_at IS NULL")
        )
    assert live == 1, "exactly one live link remains"


async def test_owner_over_the_live_cap_is_skipped_not_fatal(db_session_factory, moved):
    for _ in range(25):
        assert (await _import(db_session_factory, moved, legacy_id=str(uuid.uuid4()))).watch
    over = await _import(db_session_factory, moved, legacy_id=str(uuid.uuid4()))
    assert over.outcome == "owner_cap" and over.watch is None and over.token is None
    assert await count(db_session_factory, "watches") == 25


async def test_oversize_instructions_are_clipped_and_counted(db_session_factory, moved):
    item = await _import(db_session_factory, moved, instructions="  " + "x" * 5000)
    assert item.truncated and item.watch is not None
    assert len(item.watch.instructions) == 2000, len(item.watch.instructions)


async def test_rerun_updates_an_unconfirmed_watch_and_leaves_a_confirmed_one(
    db_session_factory, moved
):
    first = await _import(db_session_factory, moved)
    assert first.watch is not None
    changed = await _import(db_session_factory, moved, instructions="New wording.", paused=True)
    assert changed.outcome == "updated" and changed.token is None
    assert changed.watch is not None and changed.watch.instructions == "New wording."
    assert changed.watch.source_state["keep_paused"] is True
    async with db_session_factory.begin() as db:
        await watches.confirm_import(
            db,
            first.watch.id,
            tenant_id=moved.id,
            owner_account_id=first.watch.owner_account_id,
            agent_id="agent-new",
            parent_channel_id="900",
            thread_id="900",
            now=datetime.now(UTC),
        )
    final = await _import(db_session_factory, moved, instructions="Third wording.")
    assert final.outcome == "existing" and final.watch is not None
    assert final.watch.instructions == "New wording."


async def test_operator_unpause_resumes_a_confirmed_paused_import(db_session_factory, moved):
    item = await _import(db_session_factory, moved, paused=True)
    assert item.watch is not None and item.token is not None
    async with db_session_factory.begin() as db:
        with pytest.raises(ValueError, match="not found"):
            await watches.unpause_imported_watch(
                db, item.watch.id, tenant_id=moved.id, now=datetime.now(UTC)
            )
        await watches.confirm_import(
            db,
            item.watch.id,
            tenant_id=moved.id,
            owner_account_id=item.watch.owner_account_id,
            agent_id="agent-new",
            parent_channel_id="900",
            thread_id="900",
            now=datetime.now(UTC),
        )
        row = await watches.unpause_imported_watch(
            db, item.watch.id, tenant_id=moved.id, now=datetime.now(UTC)
        )
    assert row.status == "active", row.status
    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        assert (await post(client, item.watch.id, item.token)).status == 202


async def test_timer_rerun_updates_unsettled_rows_and_full_cancels_the_missing(
    db_session_factory, moved
):
    now = datetime.now(UTC)
    async with db_session_factory.begin() as db:
        for sid in ("keep", "drop", "paused"):
            timer = _timer(sid, note=sid, paused=sid == "keep")
            await import_held_timer(db, timer, tenant_id=moved.id, now=now)
        # What a rerun says about `paused` is ignored: only the first import counts.
        moved_fire = _timer("keep", minutes=120, note="changed", paused=False)
        again = await import_held_timer(db, moved_fire, tenant_id=moved.id, now=now)
        assert again.outcome == "updated", again
        cancelled = await cancel_missing_timers(
            db, tenant_id=moved.id, keep={"keep", "paused"}, now=now
        )
        assert cancelled == 1
        assert await unpause_timers(db, tenant_id=moved.id, source_id="keep") == 1
    counts = await settle_held_timers(
        db_session_factory, tenant_id=moved.id, agent_id="agent-new", agent_name="analyst", now=now
    )
    assert dict(counts) == {"scheduled": 2}, counts
    async with db_session_factory() as db:
        outcomes = {
            r[0]: r[1] for r in await db.execute(text("SELECT source_id, outcome FROM held_timers"))
        }
    assert outcomes == {"keep": "scheduled", "drop": "cancelled", "paused": "scheduled"}


async def test_timer_text_is_stripped_clipped_and_may_be_empty(db_session_factory, moved):
    now = datetime.now(UTC)
    async with db_session_factory.begin() as db:
        long = await import_held_timer(
            db, _timer("long", note="n" * 5000), tenant_id=moved.id, now=now
        )
        empty = await import_held_timer(
            db, _timer("empty", note="   "), tenant_id=moved.id, now=now
        )
    assert long.truncated and long.outcome == "held"
    assert empty.outcome == "held" and not empty.truncated
    await settle_held_timers(
        db_session_factory, tenant_id=moved.id, agent_id="agent-new", agent_name="analyst", now=now
    )
    texts = {row[2] for row in await _pending_timers(db_session_factory)}
    assert "n" * 2000 in texts and EMPTY_NOTE_PROMPT in texts, texts


def test_exporter_shapes_are_accepted():
    observer = _manifest_watch(place_type="observer")
    padded = _manifest_watch(instructions="  x  ")
    export = _export(watches=[observer, padded])
    assert export.watches[0].place_type == "channel", "an observer place maps to the channel place"
    assert export.watches[1].instructions == "x"
    with pytest.raises(ValidationError):
        _export(watches=[_manifest_watch(instructions="   ")])
    with pytest.raises(ValidationError):
        _export(
            timers=[
                {
                    "source_id": "t",
                    "owner_discord_id": "5",
                    "channel_id": "9",
                    "thread_id": None,
                    "place_type": "direct-message",
                    "fire_at": "2030-01-01T00:00:00",
                    "note": "",
                }
            ]
        )


def _settings(**extra):
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            **extra,
        }
    )


async def _choose(factory, tenant, value):
    async with factory.begin() as db:
        await set_feature_value(
            db,
            tenant_id=tenant.id,
            target=FeatureTarget("workspace"),
            key="callback_watch_lifetime",
            value=value,
            set_by=None,
            surface="chat",
        )


async def _lifetime(factory, tenant, settings=None):
    async with factory() as db:
        return await watches.callback_lifetime(db, settings, tenant.id)


async def test_callback_lifetime_is_thirty_days_unless_a_server_admin_changes_it(
    db_session_factory, moved
):
    assert await _lifetime(db_session_factory, moved) == timedelta(days=30)
    await _choose(db_session_factory, moved, "0")
    assert await _lifetime(db_session_factory, moved) is None
    await _choose(db_session_factory, moved, "7")
    assert await _lifetime(db_session_factory, moved) == timedelta(days=7)


async def test_a_workspace_may_set_ninety_days_over_the_thirty_day_default(
    db_session_factory, moved
):
    await _choose(db_session_factory, moved, "90")
    assert await _lifetime(db_session_factory, moved) == timedelta(days=90)
    assert await _lifetime(db_session_factory, moved, _settings(features={})) == timedelta(days=90)


async def test_the_deployment_sets_the_lifetime_and_a_workspace_cannot_loosen_it(
    db_session_factory, moved
):
    kept = _settings(features={"callback_watch_lifetime": "14"})
    assert await _lifetime(db_session_factory, moved, kept) == timedelta(days=14)
    await _choose(db_session_factory, moved, "0")
    assert await _lifetime(db_session_factory, moved, kept) == timedelta(days=14)
    unlimited = _settings(features={"callback_watch_lifetime": "0"})
    await _choose(db_session_factory, moved, "7")
    assert await _lifetime(db_session_factory, moved, unlimited) == timedelta(days=7)
    assert await _lifetime(db_session_factory, moved, _settings(features={})) == timedelta(days=7)


async def test_import_keeps_a_long_source_to_the_setting_and_unlimited_keeps_it_all(
    db_session_factory, moved
):
    async def run(legacy_id, expires_at, cap):
        definition = ImportedCallbackWatch.model_validate(
            _manifest_watch(legacy_id=legacy_id, expires_at=expires_at)
        )
        async with db_session_factory.begin() as db:
            return await watches.import_callback_watch(
                db,
                definition,
                tenant_id=moved.id,
                agent_id="agent-new",
                agent_name="analyst",
                now=datetime.now(UTC),
                lifetime_cap=cap,
            )

    ninety = (datetime.now(UTC) + timedelta(days=90)).isoformat()
    week = await run("a" * 8 + "-1111-1111-1111-" + "1" * 12, ninety, timedelta(days=7))
    assert (
        week.capped
        and week.watch is not None
        and week.watch.source_state["lifetime_s"] == 7 * 86400
    )
    free = await run("b" * 8 + "-1111-1111-1111-" + "1" * 12, ninety, None)
    assert not free.capped and free.watch is not None
    assert 89 * 86400 < free.watch.source_state["lifetime_s"] <= 90 * 86400
    never = await run("c" * 8 + "-1111-1111-1111-" + "1" * 12, None, None)
    assert not never.capped and never.watch is not None
    assert never.watch.source_state["lifetime_s"] is None
    async with db_session_factory.begin() as db:
        confirmed = await watches.confirm_import(
            db,
            never.watch.id,
            tenant_id=moved.id,
            owner_account_id=never.watch.owner_account_id,
            agent_id="agent-new",
            parent_channel_id="900",
            thread_id="900",
            now=datetime.now(UTC),
        )
        links = list(
            await db.scalars(
                text("SELECT expires_at FROM watch_capabilities WHERE watch_id=:w"),
                {"w": never.watch.id},
            )
        )
    assert confirmed.status == "active" and confirmed.expires_at is None
    assert links == [None, None], links
    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        assert (await post(client, never.watch.id, never.token)).status == 202


async def test_an_unlimited_callback_watch_and_its_link_keep_working(db_session_factory, moved):
    item = await _import(db_session_factory, moved, expires_at=None)
    assert item.watch is not None
    now = datetime.now(UTC)
    async with db_session_factory.begin() as db:
        renewed = await watches.renew_callback_watch(
            db, item.watch.id, tenant_id=moved.id, now=now, lifetime=None
        )
        links = list(await db.scalars(text("SELECT expires_at FROM watch_capabilities")))
    assert renewed.expires_at is None
    assert links == [None, None], links
    async with db_session_factory.begin() as db:
        token = await watches.rotate_callback_link(db, item.watch.id, tenant_id=moved.id, now=now)
        rotated = await db.scalar(
            text("SELECT expires_at FROM watch_capabilities WHERE revoked_at IS NULL")
        )
        assert rotated is None and token
        short = await watches.renew_callback_watch(
            db, item.watch.id, tenant_id=moved.id, now=now, lifetime=timedelta(days=7)
        )
        links = list(
            await db.scalars(
                text("SELECT expires_at FROM watch_capabilities WHERE revoked_at IS NULL")
            )
        )
    assert short.expires_at == now + timedelta(days=7)
    assert links == [short.expires_at]


async def test_an_unlimited_import_kept_paused_counts_its_paused_days_from_confirmation(
    db_session_factory, moved
):
    definition = ImportedCallbackWatch.model_validate(_manifest_watch(expires_at=None, paused=True))
    start = datetime.now(UTC)
    async with db_session_factory.begin() as db:
        item = await watches.import_callback_watch(
            db,
            definition,
            tenant_id=moved.id,
            agent_id="agent-new",
            agent_name="analyst",
            now=start,
            lifetime_cap=None,
        )
    assert item.watch is not None
    confirm_at = start + timedelta(days=20)
    async with db_session_factory.begin() as db:
        kept = await watches.confirm_import(
            db,
            item.watch.id,
            tenant_id=moved.id,
            owner_account_id=item.watch.owner_account_id,
            agent_id="agent-new",
            parent_channel_id="900",
            thread_id="900",
            now=confirm_at,
        )
    assert kept.status == "paused" and kept.expires_at is None and kept.paused_at == confirm_at
    late = confirm_at + watches.PAUSED_PERMANENT_EXPIRY + timedelta(minutes=1)
    async with db_session_factory.begin() as db:
        with pytest.raises(ValueError, match="cannot resume"):
            await watches.unpause_imported_watch(db, item.watch.id, tenant_id=moved.id, now=late)
    async with db_session_factory.begin() as db:
        renewed = await watches.renew_callback_watch(
            db, item.watch.id, tenant_id=moved.id, now=late, lifetime=None
        )
        assert renewed.paused_at == late, "renew restarts a paused watch's clock"
        row = await watches.unpause_imported_watch(db, item.watch.id, tenant_id=moved.id, now=late)
    assert row.status == "active" and row.expires_at is None


async def test_a_new_callback_watch_may_last_as_long_as_the_setting_allows(
    db_session_factory, link
):
    template, _ = link
    now = datetime.now(UTC)
    async with db_session_factory.begin() as db:
        binding = ContinuationRequest(
            tenant_id=template.tenant_id,
            platform="discord",
            parent_channel_id="4241",
            thread_id="4242",
            requester_account_id=template.owner_account_id,
            requester_external_user_id="123",
            target_ma_agent_id="agent-bound",
            target_name="analyst",
            requested_work=None,
            reason="watch_fired",
            idempotency_key=uuid.uuid4(),
        )
        long, _token = await watches.register_callback_watch(
            db, binding, instructions="Report.", lifetime=timedelta(days=90), now=now
        )
        assert long.expires_at == now + timedelta(days=90)
        # No expiry polls nothing, so these never count toward the permanent-watch limit.
        made = [
            await watches.register_callback_watch(
                db, binding, instructions="Report.", lifetime=None, now=now
            )
            for _ in range(watches.MAX_PERMANENT_PER_OWNER + 1)
        ]
        links = list(
            await db.scalars(
                text("SELECT expires_at FROM watch_capabilities WHERE watch_id=:w"),
                {"w": made[0][0].id},
            )
        )
    assert all(watch.expires_at is None for watch, _ in made)
    assert links == [None]
    watch, token = made[0]
    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        assert (await post(client, watch.id, token)).status == 202


async def test_the_deployment_sets_the_body_cap_for_the_sidecar_and_the_database(
    db_session_factory, link, monkeypatch
):
    watch, token = link
    monkeypatch.setenv("DAIMON_WATCHES__CALLBACK_MAX_BODY_BYTES", str(256 * 1024))
    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        assert (await post(client, watch.id, token, b"x" * (256 * 1024 + 1))).status == 413
        for letter in (b"a", b"b", b"c"):
            assert (await post(client, watch.id, token, letter * (256 * 1024))).status == 202
    async with db_session_factory() as db:
        evidence = await watches.list_evidence(db, watch.id)
        assert [len(row.payload) for row in evidence] == [256 * 1024] * 3
        wake = await get_continuation(db, idempotency_key=evidence[0].wake_key)
        seed = await watches.watch_seed(db, wake, now=datetime.now(UTC))
        # A wake carries at most 512 KiB of evidence: the newest post stays, older ones are counted.
        assert seed is not None and "c" * 1000 in seed and "a" * 1000 not in seed
        assert '"omitted": 2' in seed


async def test_the_default_body_cap_is_unchanged(db_session_factory, link):
    watch, token = link
    assert watches.load_watches_settings().callback_max_body_bytes == 4096
    async with TestClient(TestServer(create_app(db_session_factory))) as client:
        assert (await post(client, watch.id, token, b"x" * 4097)).status == 413
        assert (await post(client, watch.id, token, b"x" * 4096)).status == 202


async def test_the_database_enforces_its_own_cap_whatever_the_caller_passes(
    db_session_factory, link
):
    watch, token = link

    async def call(size: int, cap: int | None) -> str:
        try:
            async with db_session_factory.begin() as db:
                await db.scalar(
                    text("SELECT accept_watch_callback(:w,:t,:b,:m)"),
                    {"w": watch.id, "t": token, "b": "x" * size, "m": cap},
                )
        except Exception as exc:
            return str(getattr(exc.__cause__, "sqlstate", None) or getattr(exc, "orig", exc))
        return "accepted"

    assert await call(5000, 4096) == "42501"
    assert await call(5000, None) == "42501"
    assert await call(4096, 1) == "accepted"  # Below the floor, the default 4 KiB applies.
    assert await call(524289, 10_000_000) == "42501"  # Above the ceiling, 512 KiB applies.
    assert await call(524288, 10_000_000) == "accepted"


@pytest.mark.parametrize("value", ["4095", "524289", "0", "-1"])
def test_the_body_cap_setting_has_bounds(monkeypatch, value):
    monkeypatch.setenv("DAIMON_WATCHES__CALLBACK_MAX_BODY_BYTES", value)
    with pytest.raises(ValidationError):
        watches.load_watches_settings()


async def test_reimporting_an_unconfirmed_watch_refreshes_its_lifetime(db_session_factory, moved):
    async def run(cap):
        definition = ImportedCallbackWatch.model_validate(_manifest_watch(expires_at=None))
        async with db_session_factory.begin() as db:
            return await watches.import_callback_watch(
                db,
                definition,
                tenant_id=moved.id,
                agent_id="agent-new",
                agent_name="analyst",
                now=datetime.now(UTC),
                lifetime_cap=cap,
            )

    first = await run(timedelta(days=7))
    assert first.capped and first.watch is not None
    assert first.watch.source_state["lifetime_s"] == 7 * 86400
    again = await run(timedelta(days=90))
    assert again.outcome == "updated" and again.capped and again.watch is not None
    assert again.watch.source_state["lifetime_s"] == 90 * 86400
    assert (await run(timedelta(days=90))).outcome == "existing"
    free = await run(None)
    assert free.outcome == "updated" and not free.capped and free.watch is not None
    assert free.watch.source_state["lifetime_s"] is None
    async with db_session_factory.begin() as db:
        await watches.confirm_import(
            db,
            first.watch.id,
            tenant_id=moved.id,
            owner_account_id=first.watch.owner_account_id,
            agent_id="agent-new",
            parent_channel_id="900",
            thread_id="900",
            now=datetime.now(UTC),
        )
    assert (await run(None)).outcome == "existing"


async def test_a_rerun_after_the_source_ended_keeps_its_lifetime_uncapped(
    db_session_factory, moved
):
    first = await _import(db_session_factory, moved)
    assert first.watch is not None and not first.capped
    kept = first.watch.source_state["lifetime_s"]
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    again = await _import(db_session_factory, moved, expires_at=past, instructions="New wording.")
    assert again.outcome == "updated" and not again.capped and again.watch is not None
    assert again.watch.source_state["lifetime_s"] == kept


async def test_an_unlimited_watch_lists_as_permanent(db_session_factory, moved):
    from daimon.adapters.mcp.tools.watches import lasts, summary

    item = await _import(db_session_factory, moved, expires_at=None)
    assert item.watch is not None
    now = datetime.now(UTC)
    async with db_session_factory.begin() as db:
        forever = await watches.renew_callback_watch(
            db, item.watch.id, tenant_id=moved.id, now=now, lifetime=None
        )
    assert lasts(forever.expires_at) == "permanent"
    listed = summary(forever)
    assert listed.permanent and listed.expires_at is None
    async with db_session_factory.begin() as db:
        week = await watches.renew_callback_watch(
            db, item.watch.id, tenant_id=moved.id, now=now, lifetime=timedelta(days=7)
        )
    assert lasts(week.expires_at).startswith("until ")
    shown = summary(week)
    assert not shown.permanent and shown.expires_at == week.expires_at
