"""Secret-link callbacks: a bearer token, stored only as a hash, over real Postgres and HTTP."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from aiohttp.test_utils import TestClient, TestServer
from daimon.adapters.sidecar.main import create_app
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.continuity.held_timers import import_held_timer, settle_held_timers
from daimon.core.continuity.watch_imports import (
    ImportedCallbackWatch,
    ImportedTimer,
    WatchExport,
)
from daimon.core.stores import tenant_ledger, watches
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.testing.factories import make_tenant
from daimon.testing.watch_fixtures import install_poll_ports
from pydantic import ValidationError
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
            expires_at=now + timedelta(days=1),
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


async def test_callback_watch_is_never_permanent(db_session_factory, link):
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
                source="callback",
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


def test_manifest_accepts_callbacks_and_timers_and_rejects_unknown_fields():
    manifest = WatchExport.model_validate(
        {
            "version": 1,
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
    assert WatchExport.model_validate({"version": 1, "watches": []}).timers == []
    with pytest.raises(ValidationError):
        WatchExport.model_validate({"version": 1, "watches": [_manifest_watch(extra=1)]})
    with pytest.raises(ValidationError):
        WatchExport.model_validate(
            {"version": 1, "watches": [_manifest_watch(token_sha256="AB" * 32)]}
        )


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
        # Held: neither link works until an operator confirms the destination.
        assert (await post(client, watch.id, item.token)).status == 403
        assert (await post(client, watch.id, OLD_TOKEN)).status == 403
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


def _timer(source_id, *, minutes=60, place="channel", owner="555", note="ping"):
    return ImportedTimer(
        source_id=source_id,
        owner_discord_id=owner,
        channel_id="900",
        thread_id=None,
        place_type=place,
        fire_at=datetime.now(UTC) + timedelta(minutes=minutes),
        note=note,
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
    async with db_session_factory.begin() as db:
        results = [
            await import_held_timer(db, t, tenant_id=moved.id, now=now)
            for t in (
                _timer("a", note="later"),
                _timer("b", minutes=0, note="due"),
                _timer("c", place="direct-message", note="private"),
                _timer("a", note="later"),
            )
        ]
    assert results == ["held", "past", "held", "existing"]
    assert await _pending_timers(db_session_factory) == []
    counts = await settle_held_timers(
        db_session_factory, tenant_id=moved.id, agent_id="agent-new", agent_name="analyst", now=now
    )
    assert dict(counts) == {"scheduled": 1, "no_dm": 1}
    assert await _pending_timers(db_session_factory) == [("900", "900", "later")]
    again = await settle_held_timers(
        db_session_factory, tenant_id=moved.id, agent_id="agent-new", agent_name="analyst", now=now
    )
    assert dict(again) == {}
    async with db_session_factory() as db:
        notes = list(await db.scalars(text("SELECT note FROM held_timers")))
    assert notes == ["", "", ""]


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
