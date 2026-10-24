"""J10 offline E2E: a GPT or Gemini agent coordinates a fleet, like a Claude one does.

A copy switched to another backend answers through its native counterpart; the fleet
root gate refused any admission carrying a counterpart, so every GPT/Gemini root ran
with fleet off (the computed default) or failed (an explicit grant). Astra review R02
and latent-audit M07. Same harness as J2 (production path, fake provider HTTP edges);
cheap models only (Gemini Flash; the Claude copy is the control).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from daimon.adapters.mcp.tools.agents import (
    _fork_agent_impl,  # pyright: ignore[reportPrivateUsage]
    _request_agent_backend_switch_impl,  # pyright: ignore[reportPrivateUsage]
)

from packages.adapters.mcp.tests.tools.test_j2_gemini_root_e2e import (
    db_nullpool_engine as db_nullpool_engine,
)
from packages.adapters.mcp.tests.tools.test_j2_gemini_root_e2e import db_preflight as db_preflight
from packages.adapters.mcp.tests.tools.test_j2_gemini_root_e2e import fresh_chat as fresh_chat


async def _coordinator_turn(chat, monkeypatch, backend: str | None, *, explicit: bool = False):
    """The copy (switched to `backend`, or left on Claude) answers one message with fleet
    on. Returns (retained fleet_root operation keys, remembered refusal)."""
    from daimon.core.mcp_oauth import discovery
    from daimon.core.stores import turn_card_intents
    from daimon.core.stores.mux_state import PostgresStateStore
    from daimon.core.turn.admission import admit
    from daimon.core.turn.prepare import bind_session_impl
    from daimon.core.turn.run import run_prepared_turn
    from daimon.core.turn.runtime_manifest import root_refusal
    from daimon.core.turn.runtimes import build_channel_runtime
    from daimon.core.turn_hold import RequesterOrg
    from daimon.core.turn_origin import current_origin_id
    from daimon.testing.turn_fakes import RecordingLifecycle, deps_for_pinned_mux
    from sqlalchemy import text

    from packages.core.tests.turn.test_gemini_codec import interaction
    from packages.core.tests.turn.test_gemini_runtime_transport import clients_for

    async def listed(_url: str, _token: str) -> frozenset[str]:
        return frozenset({"hand_off_task", "request_agent_backend_switch"})

    monkeypatch.setattr(discovery, "list_mcp_tools", listed)
    copy = None
    if backend is not None:
        copy = await _fork_agent_impl(
            chat.runtime,
            chat.auth,
            "Daimon",
            "Daimon",
            expected_ma_agent_id="agent_daimon",
            new_copy_if_taken=True,
            origin_context_id=str(chat.origin.id),
        )
    if backend == "gemini":
        preview = await _request_agent_backend_switch_impl(
            chat.runtime,
            chat.auth,
            agent_name=copy.name,
            target_backend="gemini",
            model="gemini-3.8-flash",
            origin_context_id=str(chat.origin.id),
        )
        await _request_agent_backend_switch_impl(
            chat.runtime,
            chat.auth,
            agent_name=copy.name,
            target_backend="gemini",
            model="gemini-3.8-flash",
            origin_context_id=str(chat.origin.id),
            expected_revision=preview.expected_revision,
            preview_digest=preview.preview_digest,
            accepted_drops=preview.declared_drops,
        )
    if explicit:
        # An explicit (stored) fleet grant on the copy, saved through the same backend
        # service the request_fleet_config card confirms: a root refusal then fails
        # the turn instead of quietly turning fleet off.
        from daimon.adapters.mcp.tools.fleet_config import (
            FleetConfigInput,
            configured_fleet_grant,
        )
        from daimon.core.authz import Subject
        from daimon.core.channel_backend import (
            AgentBackendService,
            BackendActor,
            load_backend_agent,
        )

        service = AgentBackendService(chat.runtime.session_factory, enabled=True)
        agent = await load_backend_agent(
            chat.runtime.client,
            chat.runtime.session_factory,
            tenant_id=chat.auth.tenant_id,
            agent_name=copy.name,
        )
        now = datetime.now(UTC)
        value = await configured_fleet_grant(
            FleetConfigInput.model_validate(
                {"max_workers": 3, "budget_usd": "1", "max_worker_seconds": 120}
            ),
            service,
            chat.runtime.client,
            chat.runtime.session_factory,
            agent,
            now=now,
        )
        preview = await service.preview_fleet_grant(agent, value, now=now)
        await service.set_fleet_grant(
            agent,
            value,
            preview.setting_revision,
            BackendActor(chat.auth.tenant_id, chat.auth.account_id, Subject(is_admin=True)),
            expected_preview_digest=preview.digest,
            now=now,
        )
    native: dict = {}
    posts: list = []

    def gemini(call: httpx.Request) -> httpx.Response:
        if call.url.path.startswith("/v1beta/agents"):
            if call.method == "POST":
                native.update(json.loads(call.content))
            return httpx.Response(200, json=native)
        if call.method == "POST":
            posts.append(json.loads(call.content))
            return httpx.Response(200, json=interaction(f"turn-{len(posts)}"))
        return httpx.Response(200, json=interaction(call.url.path.rsplit("/", 1)[-1]))

    clients_for(monkeypatch, gemini)
    sm = chat.runtime.session_factory
    deps = replace(
        deps_for_pinned_mux(chat.runtime.client, sm),
        agent_backends=True,
        fleet_enabled=True,
        deployment_default=chat.runtime.deployment_default,
        state_store=PostgresStateStore(sm),
        turn_runtimes={},
        turn_runtime_factory=build_channel_runtime,
        mcp=chat.runtime.settings.mcp,
    )
    # The copy's turn has its own origin naming it as the responder (production makes
    # one per turn), current for the whole turn, preparation included.
    from daimon.core.stores.domain import Role
    from daimon.core.stores.turn_origins import create_origin

    async with sm.begin() as db:
        origin = await create_origin(
            db,
            tenant_id=chat.auth.tenant_id,
            account_id=chat.auth.account_id,
            platform="discord",
            parent_channel_id="fresh-channel",
            thread_id="thread",
            responder_ma_agent_id=copy.id if copy is not None else "agent_daimon",
            responder_name=copy.name if copy is not None else "Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.ADMIN if chat.auth.is_admin else Role.USER,
            sender_foreign=False,
            shared_channel=False,
            now=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    token = current_origin_id.set(origin.id)
    admission = await admit(
        deps,
        tenant_id=chat.auth.tenant_id,
        requester_org=RequesterOrg.HOST,
        platform="discord",
        external_user_id="123",
        channel_id="fresh-channel",
        thread_id="thread",
        now=datetime.now(UTC),
    )
    assert admission.agent.id == (copy.id if copy is not None else "agent_daimon")
    prepared = await bind_session_impl(
        deps,
        admission,
        tenant_id=chat.auth.tenant_id,
        platform="discord",
        external_user_id="123",
        thread_id="thread",
        session_account_id=chat.auth.account_id,
        reuse_existing=True,
    )
    async with sm.begin() as db:
        intent = await turn_card_intents.create_turn_card_intent(
            db,
            tenant_id=chat.auth.tenant_id,
            platform="discord",
            channel_id="fresh-channel",
            thread_id="thread",
            turn_token=uuid.uuid4(),
        )

    async def no_recovery():
        pytest.fail("the turn changed its admitted source/session")

    try:
        await run_prepared_turn(
            deps,
            prepared,
            tenant_id=chat.auth.tenant_id,
            platform="discord",
            thread_id="thread",
            external_user_id="123",
            user_message="compare X across a fleet",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=no_recovery,
            recovery_lifecycle=lambda _: RecordingLifecycle(),
            operation_key="j10-native-root",
            turn_card_intent_id=intent.id,
        )
    finally:
        current_origin_id.reset(token)
    async with sm() as db:
        keys = list(
            await db.scalars(
                text("SELECT key FROM operation WHERE key LIKE :k"),
                {"k": f"fleet_root:{origin.id}:%"},
            )
        )
    frozen = None
    if keys:
        from daimon.core.mux_backend import resource_scope
        from daimon.core.turn.runtime_manifest import read_frozen_root

        frozen, _ = await read_frozen_root(
            deps,
            resource_scope(
                tenant_id=str(chat.auth.tenant_id),
                account_id=str(chat.auth.account_id),
                authorization_id="fleet-origin:" + str(origin.id),
            ),
            origin_id=origin.id,
            now=datetime.now(UTC),
        )
    return keys, root_refusal(origin.id), prepared, frozen


@pytest.mark.parametrize("grant", ["default", "explicit"])
@pytest.mark.parametrize("backend", ["gemini"])
async def test_a_native_agent_coordinates_a_fleet(fresh_chat, monkeypatch, backend, grant):
    """A Claude coordinator is covered by test_frozen_root and the J4 journey."""
    keys, refusal, prepared, frozen = await _coordinator_turn(
        fresh_chat, monkeypatch, backend, explicit=grant == "explicit"
    )
    if backend == "gemini":
        assert prepared.session_ref.provider == "gemini"
        assert prepared.admission.backend_counterpart is not None
    assert refusal is None, refusal
    assert keys, "no fleet root was retained"
    # The root's own model is the one it runs on, so a worker that names no model runs
    # there too (not on the copy's original Claude model).
    assert (frozen.spec.model.provider, frozen.spec.model.id) == ("gemini", "gemini-3.8-flash")
