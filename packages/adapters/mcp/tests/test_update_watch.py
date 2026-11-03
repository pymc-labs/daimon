"""Editing a watch in place: only its owner, only as the card showed it, never firing on
the edit itself; and permanent watches and raised daily limits need an admin."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import watches as tools
from daimon.adapters.mcp.tools.watches import (
    ADMIN_ONLY,
    WatchEditRequest,
    create_watch_impl,
    preview_watch_edit_impl,
    update_watch_impl,
)
from daimon.adapters.mcp.watch_polling import run_poll
from daimon.core.authz import Place
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.membership import Membership
from daimon.core.stores import watches
from daimon.core.stores.domain import Role
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.watch_fixtures import install_poll_ports
from fastmcp.exceptions import ToolError
from mux.testing.drivers.anthropic.ma_models import ma_session, ma_session_agent
from sqlalchemy import text

pytestmark = pytest.mark.fresh_schema

GATED = ("create_watch", "resume_watch", "rotate_watch_key", "update_watch")


def _session(gated: tuple[str, ...]):
    return ma_session(
        agent=ma_session_agent(
            id="agent-bound",
            tools=[
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "daimon-mcp",
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                    "configs": [
                        {
                            "name": name,
                            "enabled": True,
                            "permission_policy": {"type": "always_ask"},
                        }
                        for name in gated
                    ],
                }
            ],
        )
    )


@pytest.fixture
async def owner(db_session_factory, monkeypatch):
    """A non-admin owner's thread-delivered read_thread watch, in an attended, gated turn."""
    from daimon.core.turn_origin import turn_origin

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
            instructions="Report changes.",
            tool="read_thread",
            arguments={"thread_id": "4343"},
            projection=".rows",
            rule="change",
            interval=60,
            expires_at=now + timedelta(days=1),
            now=now,
        )
    runtime = SimpleNamespace(
        session_factory=db_session_factory,
        client=object(),
        membership_resolver=SimpleNamespace(
            resolve=AsyncMock(return_value=Membership(is_member=True))
        ),
        settings=SimpleNamespace(
            mcp=SimpleNamespace(public_url="https://os.example/mcp"), direct_message_policies={}
        ),
    )
    monkeypatch.setattr(
        "daimon.adapters.mcp.watch_polling.retrieve_agent",
        AsyncMock(
            return_value=ma_agent(
                id="agent-bound",
                name="analyst",
                mcp_servers=[
                    {"type": "url", "name": "daimon-mcp", "url": "https://os.example/mcp"}
                ],
                tools=[
                    {
                        "type": "mcp_toolset",
                        "mcp_server_name": "daimon-mcp",
                        "configs": [],
                        "default_config": {
                            "enabled": True,
                            "permission_policy": {"type": "always_allow"},
                        },
                    }
                ],
            )
        ),
    )
    monkeypatch.setattr(
        "daimon.adapters.mcp.watch_polling.poll_read_place",
        AsyncMock(
            side_effect=lambda runtime, auth, resource_id, **kwargs: Place(
                channel_id=resource_id, parent_channel_id="4241"
            )
        ),
    )
    monkeypatch.setattr(
        "daimon.adapters.mcp.tools._session_gate.retrieve_session",
        AsyncMock(return_value=_session(GATED)),
    )
    monkeypatch.setattr(tools, "fresh_admin", AsyncMock(return_value=False))
    auth = AuthIdentity(
        account_id=watch.owner_account_id,
        tenant_id=watch.tenant_id,
        platform="discord",
        platform_user_id="123",
        external_id=tenant.external_id,
        role=Role.USER,
        chat_session_id="attended-session",
    )
    async with turn_origin(
        db_session_factory,
        tenant_id=watch.tenant_id,
        account_id=watch.owner_account_id,
        platform="discord",
        parent_channel_id=watch.parent_channel_id,
        thread_id=watch.thread_id,
        responder_ma_agent_id=watch.agent_id,
        responder_name=watch.agent_name,
        role=Role.USER,
        chat_session_id=auth.chat_session_id,
        attended=True,
        sender_foreign=False,
        shared_channel=False,
    ) as origin:
        yield runtime, watch, auth, str(origin.id)


async def preview(runtime, auth, watch, **changes):
    return await preview_watch_edit_impl(
        runtime, auth, watch_id=str(watch.id), request=WatchEditRequest(**changes)
    )


async def update(runtime, auth, origin, shown, **changes):
    return await update_watch_impl(
        runtime,
        auth,
        watch_id=str(shown.watch_id),
        origin_context_id=origin,
        expected_revision=shown.revision,
        before=shown.before,
        request=WatchEditRequest(**changes),
    )


async def lease(runtime, watch):
    async with runtime.session_factory.begin() as db:
        await db.execute(
            text("UPDATE watches SET next_check_at=now()-interval '1 second' WHERE id=:id"),
            {"id": watch.id},
        )
        return (await watches.claim_poll_watches(db))[0]["lease"]


def rows(content: str):
    return SimpleNamespace(model_dump=lambda **_: {"rows": [{"role": "user", "content": content}]})


async def test_owner_edits_interval_and_projection(owner):
    runtime, watch, auth, origin = owner
    shown = await preview(runtime, auth, watch, interval=120, projection=".rows[].content")
    assert shown.before == {"projection": ".rows", "interval": 60, "delivery": "thread"}
    assert shown.after == {"projection": ".rows[].content", "interval": 120}
    assert not shown.needs_admin
    edited = await update(runtime, auth, origin, shown, interval=120, projection=".rows[].content")
    assert edited.revision == watch.revision + 1
    async with runtime.session_factory() as db:
        current = await watches.get_watch(db, watch.id)
    assert current is not None
    assert current.rule["projection"] == ".rows[].content"
    assert current.source_state["interval"] == 120
    assert current.baseline == {}
    assert (current.tenant_id, current.owner_account_id, current.agent_id) == (
        watch.tenant_id,
        watch.owner_account_id,
        watch.agent_id,
    )


async def test_stale_revision_refuses(owner):
    runtime, watch, auth, origin = owner
    shown = await preview(runtime, auth, watch, interval=120)
    async with runtime.session_factory.begin() as db:
        await db.execute(
            text("UPDATE watches SET revision=revision+1 WHERE id=:id"), {"id": watch.id}
        )
    with pytest.raises(ToolError, match="changed during approval"):
        await update(runtime, auth, origin, shown, interval=120)
    async with runtime.session_factory() as db:
        assert (await watches.get_watch(db, watch.id)).source_state["interval"] == 60  # type: ignore[union-attr]


async def test_a_made_up_before_refuses(owner):
    runtime, watch, auth, origin = owner
    shown = await preview(runtime, auth, watch, interval=120)
    forged = shown.model_copy(update={"before": {**shown.before, "interval": 3600}})
    with pytest.raises(ToolError, match="changed during approval"):
        await update(runtime, auth, origin, forged, interval=120)
    # `before` must name exactly the changed fields and where it reports.
    missing = shown.model_copy(update={"before": {"interval": 60}})
    with pytest.raises(ToolError, match="changed during approval"):
        await update(runtime, auth, origin, missing, interval=120)


async def test_no_card_refuses(owner, monkeypatch):
    runtime, watch, auth, origin = owner
    shown = await preview(runtime, auth, watch, interval=120)
    with pytest.raises(ToolError, match="active reply from the owner"):
        await update(runtime, auth, None, shown, interval=120)
    # A session that does not ask before update_watch cannot prove the card.
    monkeypatch.setattr(
        "daimon.adapters.mcp.tools._session_gate.retrieve_session",
        AsyncMock(return_value=_session(GATED[:3])),
    )
    with pytest.raises(ToolError, match="card approval"):
        await update(runtime, auth, origin, shown, interval=120)
    async with runtime.session_factory() as db:
        assert (await watches.get_watch(db, watch.id)).revision == watch.revision  # type: ignore[union-attr]


async def test_admin_cannot_edit_another_owners_watch(owner, monkeypatch):
    runtime, watch, auth, origin = owner
    monkeypatch.setattr(tools, "fresh_admin", AsyncMock(return_value=True))
    admin = replace(auth, account_id=uuid.uuid4(), role=Role.ADMIN)
    with pytest.raises(ToolError, match="not found"):
        await preview(runtime, admin, watch, interval=120)
    shown = await preview(runtime, auth, watch, interval=120)
    with pytest.raises(ToolError, match="not found"):
        await update(runtime, admin, origin, shown, interval=120)


async def test_a_queued_wake_from_the_old_revision_is_dropped(owner):
    runtime, watch, auth, origin = owner
    source = AsyncMock(return_value=rows("first"))
    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", source):
        assert await run_poll(runtime, watch.id, await lease(runtime, watch))
        source.return_value = rows("second")
        assert await run_poll(runtime, watch.id, await lease(runtime, watch))
    async with runtime.session_factory() as db:
        (evidence,) = await watches.list_evidence(db, watch.id)
        queued = await get_continuation(db, idempotency_key=evidence.wake_key)
        assert queued is not None and queued.status == "pending"
    shown = await preview(runtime, auth, watch, instructions="Report only new rows.")
    await update(runtime, auth, origin, shown, instructions="Report only new rows.")
    async with runtime.session_factory() as db:
        queued = await get_continuation(db, idempotency_key=evidence.wake_key)
        assert queued is not None and queued.status == "cancelled"
        assert await watches.watch_seed(db, queued, now=datetime.now(UTC)) is None


async def test_the_edit_poll_rebaselines_without_firing(owner):
    runtime, watch, auth, origin = owner
    source = AsyncMock(return_value=rows("first"))
    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", source):
        assert await run_poll(runtime, watch.id, await lease(runtime, watch))
        shown = await preview(runtime, auth, watch, interval=120)
        await update(runtime, auth, origin, shown, interval=120)
        # Different from the old baseline, but the first poll after an edit only
        # saves a new starting point.
        source.return_value = rows("second")
        assert await run_poll(runtime, watch.id, await lease(runtime, watch))
        async with runtime.session_factory() as db:
            assert await watches.list_evidence(db, watch.id) == []
        source.return_value = rows("third")
        assert await run_poll(runtime, watch.id, await lease(runtime, watch))
    async with runtime.session_factory() as db:
        assert len(await watches.list_evidence(db, watch.id)) == 1


async def test_a_change_during_approval_refuses_the_commit(owner, monkeypatch):
    runtime, watch, auth, origin = owner
    shown = await preview(runtime, auth, watch, interval=120)
    real = tools.authorize_poll

    async def changed_meanwhile(runtime_, row):
        # Another session pauses the watch while this edit's checks run.
        async with runtime.session_factory.begin() as db:
            await watches.pause_watch(db, watch.id)
        monkeypatch.setattr(tools, "authorize_poll", real)
        return await real(runtime_, row)

    monkeypatch.setattr(tools, "authorize_poll", changed_meanwhile)
    with pytest.raises(ToolError, match="changed during approval"):
        await update(runtime, auth, origin, shown, interval=120)
    async with runtime.session_factory() as db:
        assert (await watches.get_watch(db, watch.id)).source_state["interval"] == 60  # type: ignore[union-attr]


async def test_permanent_needs_an_admin(owner, monkeypatch):
    runtime, watch, auth, origin = owner
    create = {
        "origin_context_id": origin,
        "instructions": "Report changes.",
        "source": "poll",
        "arguments": {"thread_id": "4545"},
        "projection": ".rows",
        "interval": 60,
        "permanent": True,
    }
    with pytest.raises(ToolError) as refused:
        await create_watch_impl(runtime, auth, **create)  # type: ignore[arg-type]
    assert str(refused.value) == ADMIN_ONLY
    shown = await preview(runtime, auth, watch, permanent=True)
    assert shown.needs_admin and shown.after == {"lasts": "permanent"}
    with pytest.raises(ToolError, match="Only an admin"):
        await update(runtime, auth, origin, shown, permanent=True)

    monkeypatch.setattr(tools, "fresh_admin", AsyncMock(return_value=True))
    made = await create_watch_impl(runtime, auth, **create)  # type: ignore[arg-type]
    assert made.permanent and made.expires_at is None
    edited = await update(runtime, auth, origin, shown, permanent=True)
    assert edited.permanent
    async with runtime.session_factory() as db:
        current = await watches.get_watch(db, watch.id)
    assert current is not None and current.expires_at is None
    assert current.permanent_approved_by == auth.account_id


async def test_daily_limit_above_24_needs_an_admin_and_stays_under_the_ceiling(owner, monkeypatch):
    runtime, _, auth, origin = owner
    create = {
        "origin_context_id": origin,
        "instructions": "Report changes.",
        "source": "poll",
        "arguments": {"thread_id": "4545"},
        "projection": ".rows",
        "interval": 60,
    }
    async with runtime.session_factory.begin() as db:
        await watches.set_watch_limits(db, scope="global", per_watch=48, per_owner=None)
    with pytest.raises(ToolError, match="Only an admin"):
        await create_watch_impl(runtime, auth, max_turns_per_day=30, **create)  # type: ignore[arg-type]
    monkeypatch.setattr(tools, "fresh_admin", AsyncMock(return_value=True))
    made = await create_watch_impl(runtime, auth, max_turns_per_day=30, **create)  # type: ignore[arg-type]
    assert made.max_turns_per_day == 30
    with pytest.raises(ToolError, match="1 to 48 times a day"):
        await create_watch_impl(runtime, auth, max_turns_per_day=60, **create)  # type: ignore[arg-type]


async def test_a_lowered_ceiling_caps_the_next_firing(owner):
    from daimon.core.continuity.watch_notices import poll_watch_notices

    runtime, watch, _, _ = owner
    source = AsyncMock(return_value=rows("first"))
    deliver = AsyncMock(return_value=True)
    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", source):
        assert await run_poll(runtime, watch.id, await lease(runtime, watch))
        source.return_value = rows("second")
        assert await run_poll(runtime, watch.id, await lease(runtime, watch))
        async with runtime.session_factory.begin() as db:
            await db.execute(text("UPDATE task_continuations SET status='delivered'"))
            # The watch allows 24 a day; the operator lowers every watch to 1.
            await watches.set_watch_limits(db, scope="global", per_watch=1, per_owner=None)
        source.return_value = rows("third")
        assert not await run_poll(runtime, watch.id, await lease(runtime, watch))
        await poll_watch_notices(runtime.session_factory, deliver=deliver)
    async with runtime.session_factory() as db:
        current = await watches.get_watch(db, watch.id)
        assert current is not None and current.max_turns_per_day == 24
        assert current.source_state.get("daily_cap_kind") == "watch"
        assert len(await watches.list_evidence(db, watch.id)) == 1
    deliver.assert_awaited_once()
    _, _, message = deliver.await_args.args
    assert "daily limit" in message


async def test_a_lowered_owner_ceiling_caps_the_next_firing(owner):
    runtime, watch, _, _ = owner
    source = AsyncMock(return_value=rows("first"))
    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", source):
        assert await run_poll(runtime, watch.id, await lease(runtime, watch))
        source.return_value = rows("second")
        assert await run_poll(runtime, watch.id, await lease(runtime, watch))
        async with runtime.session_factory.begin() as db:
            await db.execute(text("UPDATE task_continuations SET status='delivered'"))
            await watches.set_watch_limits(
                db, scope=f"tenant:{watch.tenant_id}", per_watch=None, per_owner=1
            )
        source.return_value = rows("third")
        assert not await run_poll(runtime, watch.id, await lease(runtime, watch))
    async with runtime.session_factory() as db:
        current = await watches.get_watch(db, watch.id)
    assert current is not None and current.source_state.get("daily_cap_kind") == "owner"


async def test_edit_checks_access_without_holding_the_watch_lock(owner, monkeypatch):
    runtime, watch, auth, origin = owner
    shown = await preview(runtime, auth, watch, interval=120)
    real = tools.authorize_poll
    seen: list[tuple[int, bool]] = []

    async def lock_free(runtime_, row):
        async with runtime.session_factory() as db:
            held = await db.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid()"
                    " AND locktype = 'advisory' AND objid = 813749201"
                )
            )
        seen.append((int(str(row.source_state["interval"])), held == 0))
        return await real(runtime_, row)

    monkeypatch.setattr(tools, "authorize_poll", lock_free)
    edited = await update(runtime, auth, origin, shown, interval=120)
    assert edited.revision == watch.revision + 1
    # Polling and pause/resume wait on this lock, so provider calls run outside it,
    # and they check the watch as it will be saved.
    assert seen and all(free for _, free in seen), seen
    assert (120, True) in seen
