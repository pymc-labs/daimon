"""I1 prevents output feedback cycles; I2 proves the owner's card inside each tool."""

# ruff: noqa: F811

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest
from daimon.adapters.mcp.tools import _session_gate
from daimon.adapters.mcp.tools.discord._read import _history_page
from daimon.adapters.mcp.tools.watches import (
    create_watch_impl,
    manage_watch_impl,
    rotate_watch_impl,
)
from daimon.adapters.mcp.watch_polling import run_poll
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.continuity.poll_tools import validate_poll
from daimon.core.stores import watches
from daimon.core.stores.thread_sessions import create_thread_session
from fastmcp.exceptions import ToolError
from mux.testing.drivers.anthropic.ma_models import ma_session, ma_session_agent
from sqlalchemy import text

from .test_watch_polling import attended_owner, lease, runtime_watch  # noqa: F401


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
async def test_i2_initial_page_key_uses_create_card_without_unapproved_rotation(
    attended_owner, monkeypatch
):
    runtime, _, auth, origin = attended_owner
    proof = AsyncMock(return_value=None)
    monkeypatch.setattr(_session_gate, "session_card_gap", proof)
    with patch("daimon.adapters.mcp.tools.watches.private_key_delivery", AsyncMock()) as deliver:
        created = await create_watch_impl(
            runtime,
            auth,
            origin_context_id=origin,
            source="page_event",
            page_id=str(uuid.uuid4()),
            instructions="report",
        )
    assert created.source == "page_event" and created.key_id is not None
    assert [call.kwargs["tool_name"] for call in proof.await_args_list] == ["create_watch"]
    deliver.assert_awaited_once()


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
