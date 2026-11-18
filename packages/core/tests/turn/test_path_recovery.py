"""A private Anthropic thread keeps its session across turn-path changes."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.turn.prepare import bind_session
from daimon.testing.builders import admit_chat, plain_turn_deps
from daimon.testing.ma import resolved_agent_env_router
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from daimon.testing.turn_fakes import run_prepared
from daimon.testing.turn_router import build_turn_router
from mux.contracts.ids import ChannelRef, ThreadRef
from mux.state.lease import Slot
from mux.testing.drivers.anthropic.ma import (
    FakeMAState,
    build_fake_anthropic,
    combine_handlers,
    make_fake_memory_store_handler,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daimon.testing.factories import (  # isort: skip
    make_ledger_entry,
    make_tenant,
    make_tenant_config,
)


async def test_private_anthropic_session_survives_mux_legacy_mux(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await db_session.commit()

    agent = ma_agent(id="ag_recovery", name="daimon", tenant_id=tenant.id)
    environment = ma_environment(id="env_recovery", name="default", tenant_id=tenant.id)
    ma_state = FakeMAState()
    agent_body = agent.model_dump(mode="json")
    ma_state.agents[agent.id] = agent_body
    ma_state.agent_versions[agent.id] = {agent.version: agent_body}
    sessions = FakeSessionsState(ma=ma_state)
    stream_hits: list[str] = []
    sent_bodies: list[dict[str, object]] = []
    router = build_turn_router(
        str(tenant.id),
        agent_id=agent.id,
        env_id=environment.id,
        usage_event_id=None,
        fresh_event_ids=True,
        stream_hits=stream_hits,
        sent_event_bodies=sent_bodies,
        router=resolved_agent_env_router(agent, environment),
    )
    other_requests = combine_handlers(
        make_fake_sessions_handler(sessions),
        make_fake_memory_store_handler(),
        router.dispatch,
    )
    sent_session_ids: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        if "/events" in request.url.path:
            if request.method == "POST" and request.url.path.endswith("/events"):
                sent_session_ids.append(request.url.path.split("/")[3])
            return router.dispatch(request)
        return other_requests(request)

    client = build_fake_anthropic(transport)
    deps = plain_turn_deps(
        client,
        db_session_factory,
        defaults_root=tmp_path,
        markup=Decimal(1),
        turn_path="mux",
        channel_backends=True,
    )
    slot: Slot | None = None
    first_mapping_id: uuid.UUID | None = None
    first_session_id: str | None = None
    mux_journal_count = 0
    try:
        for index, path in enumerate(("mux", "legacy", "mux")):
            path_deps = replace(deps, turn_path=path)
            admission = await admit_chat(
                path_deps,
                tenant,
                platform="slack",
                external_user_id="writer",
                channel_id="channel",
                thread_id="thread",
                now=datetime.now(UTC),
            )
            if slot is None:
                slot = Slot(
                    thread=ThreadRef(
                        channel=ChannelRef(
                            tenant_id=str(tenant.id), platform="slack", channel_id="channel"
                        ),
                        thread_id="thread",
                    ),
                    account_id=str(admission.account_id),
                )
            else:
                assert slot.account_id == str(admission.account_id), (
                    "path changes must preserve the caller's account"
                )
            prepared = await bind_session(
                path_deps,
                admission,
                tenant_id=tenant.id,
                platform="slack",
                external_user_id="writer",
                thread_id="thread",
                session_account_id=admission.account_id,
                reuse_existing=True,
            )
            if first_session_id is None:
                first_session_id = prepared.ma_session_id
                first_mapping_id = prepared.mapping_id
                assert first_mapping_id is not None, "initial preparation must map the session"
            else:
                assert prepared.ma_session_id == first_session_id, (
                    "a path change must continue the existing native session"
                )
                assert prepared.mapping_id == first_mapping_id, (
                    "a path change must reuse the same thread-session row"
                )
            outcome = await run_prepared(
                path_deps,
                prepared,
                tenant_id=tenant.id,
                platform="slack",
                thread_id="thread",
                external_user_id="writer",
                user_message=f"question {index}",
            )
            assert outcome.state.error is None, f"{path} turn should finish: {outcome.state.error}"
            assert outcome.ma_session_id == first_session_id, "driver must use the mapped session"
            binding = await PostgresStateStore(db_session_factory).get_binding(slot)
            assert binding is not None, "the first mux turn must publish a binding"
            assert binding.id == str(first_mapping_id), "binding identity follows the first mapping"
            assert binding.generation == 1, "same-session turns must not advance generation"
            assert binding.native_refs["session"] == first_session_id, (
                "legacy must leave the mux binding on the existing session"
            )
            journal = await PostgresStateStore(db_session_factory).read_events(first_session_id)
            if index == 0:
                mux_journal_count = len(journal)
                assert mux_journal_count > 0, "mux must persist the native turn events"
            elif index == 1:
                assert len(journal) == mux_journal_count, (
                    "legacy must run without appending to the mux journal"
                )
            else:
                assert len(journal) > mux_journal_count, (
                    "returning to mux must resume journal writes for the same session"
                )

        assert len(sessions.sessions) == 1, "path changes must not create another native session"
        assert stream_hits == [first_session_id] * 3, "each driver turn must stream that session"
        assert len(sent_bodies) == 3, "each path must send one user message"
        assert sent_session_ids == stream_hits, "each send must target the resumed session"
        async with db_session_factory() as db:
            live = await get_live_thread_session(
                db,
                tenant_id=tenant.id,
                platform="slack",
                thread_id="thread",
                account_id=uuid.UUID(slot.account_id),
            )
        assert live is not None and live.id == first_mapping_id, (
            "the original mapping must remain live for this account"
        )
    finally:
        await client.close()
