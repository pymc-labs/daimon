"""I1 prevents output feedback cycles; I2 proves the owner's card inside each tool."""

# ruff: noqa: F811

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import httpx
import pytest
from daimon.adapters.mcp.tools import _session_gate
from daimon.adapters.mcp.tools.discord._read import _history_page
from daimon.adapters.mcp.tools.watches import (
    create_watch_impl,
    manage_watch_impl,
    rotate_watch_impl,
)
from daimon.adapters.mcp.watch_executor import create_poll_app
from daimon.adapters.mcp.watch_polling import run_poll
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.continuity.poll_tools import require_distinct_source, validate_poll
from daimon.core.stores import watches
from daimon.core.stores.domain import Role
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.core.turn_origin import turn_origin
from fastmcp.exceptions import ToolError
from mux.testing.drivers.anthropic.ma_models import ma_session, ma_session_agent
from sqlalchemy import text

from .test_watch_polling import attended_owner, lease, runtime_watch  # noqa: F401

SECRET = "s" * 40


@pytest.mark.parametrize("identifier", ["0", "04242", "+4242", " 4242", "١٢٣", "1" * 21])
def test_i1_source_has_exactly_one_boundary_spelling(identifier):
    with pytest.raises(ValueError, match="canonical"):
        validate_poll(
            "read_thread", {"thread_id": identifier}, projection=".", rule="change", interval=60
        )


def history(messages):
    async def iterate(**kwargs):
        for message in messages[: kwargs["limit"]]:
            yield message

    return SimpleNamespace(history=iterate)


def message(identity, *, bot=False, webhook_id=None, application_id=None):
    return SimpleNamespace(
        id=identity,
        author=SimpleNamespace(id=123, name="author", bot=bot),
        channel=SimpleNamespace(id=4343),
        content=str(identity),
        created_at=datetime.now(UTC),
        attachments=[],
        type=discord.MessageType.default,
        webhook_id=webhook_id,
        application_id=application_id,
    )


async def test_i1_bot_reports_cannot_evict_humans_or_change_poll_metadata():
    human = message(1)
    before = await _history_page(history([human]), limit=1, before=None, poll_humans_only=True)
    after = await _history_page(
        history([message(i, bot=True) for i in range(2, 102)] + [human]),
        limit=1,
        before=None,
        poll_humans_only=True,
    )
    assert before == after
    assert [row.role for row in after[0]] == ["user"]
    assert after[1:] == (None, None)


async def test_i1_scan_overflow_refuses_a_partial_human_window():
    with pytest.raises(ValueError, match="bounded history scan"):
        await _history_page(
            history([message(i, bot=True) for i in range(201)] + [message(999)]),
            limit=1,
            before=None,
            poll_humans_only=True,
        )


@pytest.mark.parametrize("automated", [{"webhook_id": 777}, {"application_id": 888}])
async def test_automated_posts_without_bot_flag_do_not_displace_humans(automated):
    human = message(1)
    before = await _history_page(history([human]), limit=1, before=None, poll_humans_only=True)
    after = await _history_page(
        history([message(i, **automated) for i in range(2, 102)] + [human]),
        limit=1,
        before=None,
        poll_humans_only=True,
    )
    assert before == after


@pytest.mark.fresh_schema
async def test_i1_two_watches_cannot_wake_each_other_with_reports(runtime_watch):
    runtime, first, _ = runtime_watch
    async with runtime.session_factory.begin() as db:
        await create_thread_session(
            db,
            tenant_id=first.tenant_id,
            platform="discord",
            thread_id="4343",
            channel_id="4241",
            account_id=first.owner_account_id,
            ma_session_id="second-session",
            ma_agent_id=first.agent_id,
        )
        second = await watches.register_poll_watch(
            db,
            ContinuationRequest(
                tenant_id=first.tenant_id,
                platform="discord",
                thread_id="4343",
                parent_channel_id="4241",
                requester_account_id=first.owner_account_id,
                requester_external_user_id=first.owner_external_id,
                target_ma_agent_id=first.agent_id,
                target_name=first.agent_name,
                requested_work=None,
                reason="watch_fired",
                idempotency_key=uuid.uuid4(),
            ),
            instructions="report",
            tool="read_thread",
            arguments={"thread_id": "4242"},
            projection=".",
            rule="change",
            interval=60,
            expires_at=first.expires_at,
            now=datetime.now(UTC),
        )

    async def target_lease(watch):
        # This fixture drives one watch at a time. Keep the other out of the
        # scheduler batch rather than losing its lease in the single-watch helper.
        async with runtime.session_factory.begin() as db:
            await db.execute(
                text("UPDATE watches SET next_check_at=now()+interval '1 hour' WHERE id<>:id"),
                {"id": watch.id},
            )
        return await lease(runtime, watch)

    documents = {key: [{"role": "user", "content": "human baseline"}] for key in ("4242", "4343")}

    async def read(*args, thread_id, **kwargs):
        assert kwargs["poll_humans_only"] is True
        return SimpleNamespace(
            model_dump=lambda **_: {
                "rows": documents[thread_id],
                "next_before": str(uuid.uuid4()),
                "hint": str(uuid.uuid4()),
            }
        )

    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", read):
        assert await run_poll(runtime, first.id, await target_lease(first))
        assert await run_poll(runtime, second.id, await target_lease(second))
        documents["4343"][0] = {"role": "user", "content": "human changed source"}
        assert await run_poll(runtime, first.id, await target_lease(first))
        documents["4242"].append({"role": "assistant", "content": "A reports into B's source"})
        assert await run_poll(runtime, second.id, await target_lease(second))
        documents["4343"].append({"role": "assistant", "content": "B reports into A's source"})
        assert await run_poll(runtime, first.id, await target_lease(first))
    async with runtime.session_factory() as db:
        assert len(await watches.list_evidence(db, first.id)) == 1
        assert await watches.list_evidence(db, second.id) == []


@pytest.mark.fresh_schema
@pytest.mark.parametrize("action", ["create_watch", "resume_watch", "rotate_watch_key"])
@pytest.mark.parametrize("proof", ["ungated", "unsupported"])
async def test_i2_each_enrollment_requires_actual_server_side_card_proof(
    attended_owner, monkeypatch, action, proof
):
    runtime, watch, auth, origin = attended_owner
    await manage_watch_impl(runtime, auth, watch_id=str(watch.id), action="pause")
    if action == "rotate_watch_key":
        async with runtime.session_factory.begin() as db:
            await db.execute(
                text("UPDATE watches SET source='page_event' WHERE id=:id"), {"id": watch.id}
            )
    if proof == "ungated":
        monkeypatch.setattr(
            _session_gate,
            "retrieve_session",
            AsyncMock(return_value=ma_session(agent=ma_session_agent(id=watch.agent_id))),
        )
    else:
        from mux.errors import UnsupportedCapability

        monkeypatch.setattr(
            _session_gate,
            "retrieve_session",
            AsyncMock(
                side_effect=UnsupportedCapability(("tool_confirmation",), "gemini.inline_reuse")
            ),
        )
    deliver = AsyncMock()
    with (
        patch("daimon.adapters.mcp.tools.watches.private_key_delivery", deliver),
        pytest.raises(ToolError, match="card approval"),
    ):
        if action == "create_watch":
            await create_watch_impl(
                runtime,
                auth,
                origin_context_id=origin,
                source="poll",
                instructions="report",
                arguments={"thread_id": "4545"},
            )
        elif action == "resume_watch":
            await manage_watch_impl(
                runtime, auth, watch_id=str(watch.id), action="resume", origin_context_id=origin
            )
        else:
            await rotate_watch_impl(runtime, auth, watch_id=str(watch.id), origin_context_id=origin)
    deliver.assert_not_called()
    async with runtime.session_factory() as db:
        assert (await watches.get_watch(db, watch.id)).status == "paused"
        assert (
            len(
                await watches.list_owned_watches(
                    db, tenant_id=watch.tenant_id, account_id=watch.owner_account_id
                )
            )
            == 1
        )
        assert (
            await db.scalar(
                text("SELECT count(*) FROM watch_capabilities WHERE watch_id=:id"), {"id": watch.id}
            )
            == 0
        )


@pytest.mark.fresh_schema
async def test_create_page_event_watch_is_refused(attended_owner, monkeypatch):
    runtime, _, auth, origin = attended_owner
    proof = AsyncMock(return_value=None)
    monkeypatch.setattr(_session_gate, "session_card_gap", proof)
    with (
        patch(
            "daimon.adapters.mcp.tools.watches.watches.register_page_watch", AsyncMock()
        ) as register,
        pytest.raises(ToolError, match="until a page-event producer exists"),
    ):
        await create_watch_impl(
            runtime,
            auth,
            origin_context_id=origin,
            source="page_event",
            instructions="report",
        )
    register.assert_not_awaited()


@pytest.mark.fresh_schema
async def test_i1_legacy_bot_baseline_is_reestablished_without_firing(runtime_watch):
    runtime, watch, _ = runtime_watch
    async with runtime.session_factory.begin() as db:
        await db.execute(
            text("UPDATE watches SET baseline=CAST(:baseline AS jsonb) WHERE id=:id"),
            {
                "id": watch.id,
                "baseline": '{"value":[{"role":"user","content":"human"},{"role":"assistant","content":"old report"}]}',
            },
        )
    rows = [{"role": "user", "content": "human"}]
    source = AsyncMock(return_value=SimpleNamespace(model_dump=lambda **_: {"rows": rows}))
    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", source):
        assert await run_poll(runtime, watch.id, await lease(runtime, watch))
        rows.append({"role": "assistant", "content": "new bot report"})
        assert await run_poll(runtime, watch.id, await lease(runtime, watch))
        async with runtime.session_factory() as db:
            assert await watches.list_evidence(db, watch.id) == []
            current = await watches.get_watch(db, watch.id)
            assert current.baseline["value"] == {
                "document": "human_rows_v1",
                "projection": [{"role": "user", "content": "human"}],
            }
        rows.append({"role": "user", "content": "new human evidence"})
        assert await run_poll(runtime, watch.id, await lease(runtime, watch))
    async with runtime.session_factory() as db:
        assert len(await watches.list_evidence(db, watch.id)) == 1


@pytest.mark.fresh_schema
@pytest.mark.parametrize("action", ["create_watch", "resume_watch", "rotate_watch_key"])
async def test_i2_unsupported_bound_profile_refuses_even_a_claimed_gated_snapshot(
    attended_owner, monkeypatch, action
):
    runtime, watch, auth, origin = attended_owner
    proof = AsyncMock(return_value=None)
    monkeypatch.setattr(_session_gate, "session_card_gap", proof)
    monkeypatch.setattr(
        "daimon.adapters.mcp.tools.watches.mux_state.get_binding",
        AsyncMock(return_value=SimpleNamespace(profile="gemini.inline_reuse")),
    )
    with pytest.raises(ToolError, match="card approval"):
        if action == "create_watch":
            await create_watch_impl(
                runtime,
                auth,
                origin_context_id=origin,
                source="poll",
                instructions="report",
                arguments={"thread_id": "4545"},
            )
        elif action == "resume_watch":
            await manage_watch_impl(
                runtime, auth, watch_id=str(watch.id), action="resume", origin_context_id=origin
            )
        else:
            await rotate_watch_impl(runtime, auth, watch_id=str(watch.id), origin_context_id=origin)
    proof.assert_not_called()


@pytest.mark.fresh_schema
async def test_i1_resolved_place_identity_refuses_a_raw_distinct_source(runtime_watch):
    from daimon.core.authz import Place

    runtime, watch, _ = runtime_watch
    with (
        patch(
            "daimon.adapters.mcp.watch_polling.poll_read_place",
            AsyncMock(
                return_value=Place(
                    channel_id=watch.thread_id, parent_channel_id=watch.parent_channel_id
                )
            ),
        ),
        patch("daimon.adapters.mcp.watch_polling._read_thread_impl", AsyncMock()) as source,
    ):
        assert not await run_poll(runtime, watch.id, await lease(runtime, watch))
        source.assert_not_called()
    async with runtime.session_factory() as db:
        assert await watches.list_evidence(db, watch.id) == []


@pytest.mark.fresh_schema
@pytest.mark.parametrize("unsupported_slot", ["shared", "private"])
async def test_enrollment_resolves_both_slot_revisions_like_admission(
    attended_owner, monkeypatch, unsupported_slot
):
    from daimon.adapters.mcp.tools.watches import require_attended_owner
    from mux.contracts.config import BackendConfig, resolve_default

    runtime, _, auth, origin = attended_owner
    default = resolve_default(BackendConfig())
    proof = AsyncMock(return_value=None)
    monkeypatch.setattr(_session_gate, "session_card_gap", proof)
    resolved_revisions = []

    async def binding(db, slot, **kwargs):
        bad = (slot.account_id is None) == (unsupported_slot == "shared")
        return SimpleNamespace(
            thread=slot.thread,
            legacy_account_id=slot.account_id,
            config_revision=1 if bad else 0,
            provider="gemini" if bad else default.backend,
            profile="gemini.inline_reuse" if bad else default.profile,
        )

    async def resolve(db, channel, agent, *, enabled, retained_revision):
        assert enabled and agent is None
        resolved_revisions.append(retained_revision)
        return (
            SimpleNamespace(
                channel=channel, local=1, backend="gemini", profile="gemini.inline_reuse"
            ),
            False,
            "channel",
        )

    monkeypatch.setattr("daimon.adapters.mcp.tools.watches.mux_state.get_binding", binding)
    monkeypatch.setattr(
        "daimon.adapters.mcp.tools.watches.channel_backend.resolve_turn_backend", resolve
    )
    with pytest.raises(ToolError, match="card approval"):
        await require_attended_owner(runtime, auth, origin, tool_name="create_watch")
    assert 1 in resolved_revisions
    proof.assert_not_called()


@pytest.mark.fresh_schema
@pytest.mark.parametrize("action", ["resume", "rotate"])
async def test_enrollment_access_checks_precede_global_lock_and_recheck_state(
    attended_owner, monkeypatch, action
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    runtime, watch, auth, origin = attended_owner
    await manage_watch_impl(runtime, auth, watch_id=str(watch.id), action="pause")
    if action == "rotate":
        async with runtime.session_factory.begin() as db:
            await db.execute(
                text("UPDATE watches SET source='page_event' WHERE id=:id"), {"id": watch.id}
            )
    independent = async_sessionmaker(runtime.session_factory.kw["bind"].engine)
    observed = []

    async def check_access(*args):
        async with independent.begin() as other:
            free = await other.scalar(text("SELECT pg_try_advisory_xact_lock(813749201)"))
            observed.append(free)
        # A stop/control action while network I/O was outstanding invalidates
        # this approval, even when the final status is still paused.
        async with runtime.session_factory.begin() as db:
            await watches.pause_watch(db, watch.id)

    monkeypatch.setattr("daimon.adapters.mcp.tools.watches.authorize_poll", check_access)
    delivery = AsyncMock()
    monkeypatch.setattr("daimon.adapters.mcp.tools.watches.private_key_delivery", delivery)
    with pytest.raises(ToolError, match="changed during approval"):
        if action == "resume":
            await manage_watch_impl(
                runtime, auth, watch_id=str(watch.id), action="resume", origin_context_id=origin
            )
        else:
            await rotate_watch_impl(runtime, auth, watch_id=str(watch.id), origin_context_id=origin)
    assert observed == [True]
    delivery.assert_not_called()
    async with runtime.session_factory() as db:
        assert (await watches.get_watch(db, watch.id)).status == "paused"
        assert await db.scalar(text("SELECT count(*) FROM watch_capabilities")) == 0


@pytest.mark.fresh_schema
@pytest.mark.parametrize("action", ["resume", "rotate"])
@pytest.mark.parametrize("origin_kind", ["unattended", "none", "other_session"])
async def test_unattended_or_missing_origin_cannot_resume_or_rotate(
    runtime_watch,
    action,
    origin_kind,
):
    runtime, watch, auth = runtime_watch
    await manage_watch_impl(runtime, auth, watch_id=str(watch.id), action="pause")
    if action == "rotate":
        async with runtime.session_factory.begin() as db:
            await db.execute(
                text("UPDATE watches SET source='page_event' WHERE id=:id"), {"id": watch.id}
            )
    agent_auth = replace(auth, chat_session_id="routine-session")
    async with turn_origin(
        runtime.session_factory,
        tenant_id=watch.tenant_id,
        account_id=watch.owner_account_id,
        platform="discord",
        parent_channel_id=watch.parent_channel_id,
        thread_id=watch.thread_id,
        responder_ma_agent_id=watch.agent_id,
        responder_name=watch.agent_name,
        role=Role.USER,
        chat_session_id="other" if origin_kind == "other_session" else "routine-session",
        attended=origin_kind == "other_session",
        sender_foreign=False,
        shared_channel=False,
    ) as origin:
        origin_id = None if origin_kind == "none" else str(origin.id)
        deliver = AsyncMock()
        with (
            patch("daimon.adapters.mcp.tools.watches.private_key_delivery", deliver),
            pytest.raises(ToolError),
        ):
            if action == "resume":
                await manage_watch_impl(
                    runtime,
                    agent_auth,
                    watch_id=str(watch.id),
                    action="resume",
                    origin_context_id=origin_id,
                )
            else:
                await rotate_watch_impl(
                    runtime, agent_auth, watch_id=str(watch.id), origin_context_id=origin_id
                )
        deliver.assert_not_called()
    async with runtime.session_factory() as db:
        assert (await watches.get_watch(db, watch.id)).status == "paused"


@pytest.mark.fresh_schema
@pytest.mark.parametrize("spelling", ["04242", "004242", "0000000004242"])
def test_distinct_source_refuses_alternate_spellings_of_reporting_thread(spelling):
    # Discord resolves int("04242") == 4242: the same thread, so the same self-wake loop.
    with pytest.raises(ValueError):
        require_distinct_source({"thread_id": spelling}, "4242")


@pytest.mark.fresh_schema
async def test_register_refuses_leading_zero_self_source(runtime_watch):
    runtime, watch, _ = runtime_watch
    binding = ContinuationRequest(
        tenant_id=watch.tenant_id,
        platform="discord",
        parent_channel_id=watch.parent_channel_id,
        thread_id=watch.thread_id,
        requester_account_id=watch.owner_account_id,
        requester_external_user_id=watch.owner_external_id,
        target_ma_agent_id=watch.agent_id,
        target_name=watch.agent_name,
        requested_work=None,
        reason="watch_fired",
        idempotency_key=uuid.uuid4(),
    )
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        async with runtime.session_factory.begin() as db:
            await watches.register_poll_watch(
                db,
                binding,
                instructions="Report changes.",
                tool="read_thread",
                arguments={"thread_id": "0" + watch.thread_id},
                projection=".rows",
                rule="change",
                interval=60,
                expires_at=now + timedelta(days=1),
                now=now,
            )


@pytest.mark.fresh_schema
async def test_leading_zero_self_source_cannot_read(runtime_watch):
    """Even a stored row must be refused once the source resolves to the reporting thread."""
    runtime, watch, _ = runtime_watch
    async with runtime.session_factory.begin() as db:
        await db.execute(
            text(
                "UPDATE watches SET source_state=jsonb_set(source_state,'{arguments,thread_id}',"
                "to_jsonb(CAST(:t AS text))) WHERE id=:id"
            ),
            {"t": "0" + watch.thread_id, "id": watch.id},
        )
    # Real resolution canonicalises the ID, as discord.py's int(...) does.
    with (
        patch(
            "daimon.adapters.mcp.watch_polling.poll_read_place",
            AsyncMock(
                side_effect=lambda runtime, auth, resource_id, **kw: __import__(
                    "daimon.core.authz", fromlist=["Place"]
                ).Place(channel_id=str(int(resource_id)), parent_channel_id="4241")
            ),
        ),
        patch(
            "daimon.adapters.mcp.watch_polling._read_thread_impl",
            AsyncMock(
                return_value=SimpleNamespace(
                    model_dump=lambda **_: {"rows": [{"role": "user", "content": "1"}]}
                )
            ),
        ) as source,
    ):
        await run_poll(runtime, watch.id, await lease(runtime, watch))
        source.assert_not_called()


@pytest.mark.fresh_schema
async def test_create_watch_requires_server_side_card_proof(attended_owner, monkeypatch):
    """Other card-only tools (add_skill, publish, microsoft_send) call session_card_gap.

    A session that does not hold create_watch on always_ask (e.g. a backend whose
    profile lacks tool_confirmation, where run.py drops confirmed_tools) must be refused.
    """
    runtime, watch, auth, origin = attended_owner
    gap = AsyncMock(return_value="session_not_gated")
    monkeypatch.setattr(
        "daimon.adapters.mcp.tools._session_gate.session_card_gap", gap, raising=True
    )
    with pytest.raises(ToolError):
        await create_watch_impl(
            runtime,
            auth,
            origin_context_id=origin,
            instructions="Report a change.",
            source="poll",
            tool="read_thread",
            arguments={"thread_id": "4545"},
        )


class _NoDb:
    def __call__(self, *a, **k):
        raise AssertionError("database touched before authentication")

    begin = __call__


@pytest.mark.fresh_schema
@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("X-Watch-Executor-Key", "")],
        [("X-Watch-Executor-Key", SECRET[:-1])],
        [("X-Watch-Executor-Key", SECRET + "x")],
        [("X-Watch-Executor-Key", SECRET.upper())],
        [("X-Watch-Executor-Key", SECRET), ("X-Watch-Executor-Key", SECRET)],
        [("x-watch-executor-key", " " + SECRET)],
    ],
)
async def test_executor_auth_precedes_any_db_work(monkeypatch, headers):
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    monkeypatch.setenv("DAIMON_WATCHES__EXECUTOR_SECRET", SECRET)
    app = create_poll_app(SimpleNamespace(session_factory=_NoDb()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://x") as c:
        r = await c.post(
            f"/internal/watches/{uuid.uuid4()}/poll",
            headers=[*headers, ("X-Watch-Lease", str(uuid.uuid4()))],
        )
    assert r.status_code == 403


@pytest.mark.fresh_schema
async def test_executor_refuses_everything_when_secret_unset(monkeypatch):
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    monkeypatch.delenv("DAIMON_WATCHES__EXECUTOR_SECRET", raising=False)
    app = create_poll_app(SimpleNamespace(session_factory=_NoDb()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://x") as c:
        for key in ("", "None", "null"):
            r = await c.post(
                f"/internal/watches/{uuid.uuid4()}/poll",
                headers={"X-Watch-Executor-Key": key, "X-Watch-Lease": str(uuid.uuid4())},
            )
            assert r.status_code == 403


@pytest.mark.fresh_schema
def test_public_mcp_app_has_no_poll_route():
    import inspect

    from daimon.adapters.mcp import server

    assert "internal/watches" not in inspect.getsource(server)


@pytest.mark.fresh_schema
async def test_db_gate_flip_mid_poll_is_not_access_revoked(runtime_watch):
    runtime, watch, _ = runtime_watch
    token = await lease(runtime, watch)
    async with runtime.session_factory.begin() as db:
        await db.execute(
            text("INSERT INTO watch_controls(scope,enabled) VALUES(:s,false)"),
            {"s": f"tenant:{watch.tenant_id}"},
        )
    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", AsyncMock()) as source:
        assert not await run_poll(runtime, watch.id, token)
        source.assert_not_called()
    async with runtime.session_factory() as db:
        row = await watches.get_watch(db, watch.id)
    assert row.source_state.get("reason") != "access_revoked"
    assert row.lease_owner is None


@pytest.mark.fresh_schema
async def test_concurrent_executions_of_one_lease_fire_once(runtime_watch):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    runtime, watch, _ = runtime_watch
    state = {"rows": [{"role": "user", "content": "1"}]}
    source = AsyncMock(
        side_effect=lambda *a, **k: SimpleNamespace(model_dump=lambda **_: dict(state))
    )
    with patch("daimon.adapters.mcp.watch_polling._read_thread_impl", source):
        assert await run_poll(runtime, watch.id, await lease(runtime, watch))
        state["rows"] = [{"role": "user", "content": "2"}]
        token = await lease(runtime, watch)
        independent = async_sessionmaker(
            runtime.session_factory.kw["bind"].engine, expire_on_commit=False
        )
        parallel = SimpleNamespace(**{**vars(runtime), "session_factory": independent})
        results = await asyncio.gather(*(run_poll(parallel, watch.id, token) for _ in range(4)))
    assert sum(results) == 1
    async with runtime.session_factory() as db:
        assert len(await watches.list_evidence(db, watch.id)) == 1
        assert (await watches.get_watch(db, watch.id)).status == "active"


@pytest.mark.fresh_schema
@pytest.mark.parametrize(
    ("tool", "arguments", "projection", "rule"),
    [
        ("send_message", {"channel_id": "1"}, ".", "change"),
        ("artifact_db_query", {}, ".", "change"),
        ("READ_THREAD", {"thread_id": "1"}, ".", "change"),
        ("read_thread", {}, ".", "change"),
        ("read_thread", {"thread_id": "1", "before": "2"}, ".", "change"),
        ("read_thread", {"thread_id": "1", "url": "https://x"}, ".", "change"),
        ("read_thread", {"channel_id": "1"}, ".", "change"),
        ("read_thread", {"thread_id": "https://x"}, ".", "change"),
        ("read_thread", {"thread_id": "١٢٣"}, ".", "change"),
        ("read_thread", {"thread_id": 123}, ".", "change"),
        ("read_thread", {"thread_id": "1", "limit": True}, ".", "change"),
        ("read_thread", {"thread_id": "1", "limit": 500}, ".", "change"),
        ("read_thread", {"thread_id": "1"}, ".rows[] | select(.x)", "change"),
        ("read_thread", {"thread_id": "1"}, "..", "change"),
        ("read_thread", {"thread_id": "1"}, ".", "deleted"),
    ],
)
def test_poll_registry_refuses_smuggling(tool, arguments, projection, rule):
    from daimon.core.continuity.poll_tools import validate_poll

    with pytest.raises(ValueError):
        validate_poll(tool, arguments, projection=projection, rule=rule, interval=300)


def _history(messages):
    async def iterate(**kwargs):
        for m in messages[: kwargs["limit"]]:
            yield m

    return SimpleNamespace(history=iterate)


def _msg(identity, *, bot=False, webhook_id=None):
    return SimpleNamespace(
        id=identity,
        author=SimpleNamespace(id=identity, name="a", bot=bot),
        channel=SimpleNamespace(id=4343),
        content=f"m{identity}",
        created_at=datetime.now(UTC),
        attachments=[],
        type=discord.MessageType.default,
        webhook_id=webhook_id,
    )


async def test_i1_webhook_post_is_excluded_even_without_bot_flag():
    """Daimon answers through webhooks; exclusion must not rest on one inferred flag."""
    rows, _, _ = await _history_page(
        _history([_msg(2, webhook_id=777), _msg(1)]),
        limit=1,
        before=None,
        poll_humans_only=True,
    )
    assert [r.id for r in rows] == ["1"]


@pytest.mark.fresh_schema
async def test_i2_shared_slot_unsupported_profile_refuses(attended_owner, monkeypatch):
    """Admission reads the shared slot first on ever-shared threads; so must I2."""
    runtime, _, auth, origin = attended_owner
    monkeypatch.setattr(_session_gate, "session_card_gap", AsyncMock(return_value=None))

    async def binding(db, slot, **kw):
        return SimpleNamespace(profile="gemini.inline_reuse") if slot.account_id is None else None

    monkeypatch.setattr("daimon.adapters.mcp.tools.watches.mux_state.get_binding", binding)
    with pytest.raises(ToolError):
        await create_watch_impl(
            runtime,
            auth,
            origin_context_id=origin,
            source="poll",
            instructions="report",
            arguments={"thread_id": "4545"},
        )


@pytest.mark.fresh_schema
@pytest.mark.parametrize("approved", ["resume_watch", "rotate_watch_key", "add_skill"])
async def test_i2_proof_for_another_tool_cannot_create(attended_owner, monkeypatch, approved):
    runtime, _, auth, origin = attended_owner

    async def proof(runtime, auth, origin, *, tool_name, **kw):
        return None if tool_name == approved else "session_not_gated"

    monkeypatch.setattr(_session_gate, "session_card_gap", proof)
    with pytest.raises(ToolError):
        await create_watch_impl(
            runtime,
            auth,
            origin_context_id=origin,
            source="poll",
            instructions="report",
            arguments={"thread_id": "4545"},
        )


@pytest.mark.fresh_schema
@pytest.mark.parametrize("action", ["resume", "rotate"])
async def test_i2_proof_lookup_does_not_hold_global_watch_lock(attended_owner, monkeypatch, action):
    """The card proof makes provider calls; the global firing lock must not be held meanwhile."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    runtime, watch, auth, origin = attended_owner
    if action == "rotate":
        async with runtime.session_factory.begin() as db:
            await db.execute(
                text("UPDATE watches SET source='page_event' WHERE id=:id"), {"id": watch.id}
            )
    await manage_watch_impl(runtime, auth, watch_id=str(watch.id), action="pause")
    independent = async_sessionmaker(runtime.session_factory.kw["bind"].engine)
    observed = {}

    async def proof(*a, **k):
        async with independent() as other:
            observed["free"] = await other.scalar(text("SELECT pg_try_advisory_lock(813749201)"))
            if observed["free"]:
                await other.execute(text("SELECT pg_advisory_unlock(813749201)"))
        return None

    monkeypatch.setattr(_session_gate, "session_card_gap", proof)
    monkeypatch.setattr(
        "daimon.adapters.mcp.tools.watches.private_key_delivery", AsyncMock(return_value=None)
    )
    try:
        if action == "resume":
            await manage_watch_impl(
                runtime, auth, watch_id=str(watch.id), action="resume", origin_context_id=origin
            )
        else:
            await rotate_watch_impl(runtime, auth, watch_id=str(watch.id), origin_context_id=origin)
    except ToolError:
        pass
    assert observed.get("free") is True
