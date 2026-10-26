"""The CI staging switches select real agent and fleet paths.

Only the `pytest-staging-config` job runs this module: it sets the four switches
from its matrix and names the configuration in `DAIMON_CI_CONFIGURATION`, so a
leg whose switches drift from its name fails here instead of passing silently.
Each refusal on the off leg is paired with a control that turns the switch on
and shows the same request admitted, so the refusal is the switch's doing.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from daimon.core._models import AgentBackendRevisionRecord
from daimon.core.config import load_settings, load_turn_settings
from daimon.core.fleet_configuration import is_default_fleet
from daimon.core.neutral_resources import WorkerSpecRefused
from daimon.core.turn.admission import admit_impl
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.openai_io import OpenAITurnIO
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.prepare import bind_session_impl
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.runtime_manifest import (
    read_agent_fleet_grant,
    read_frozen_root,
    read_preparation_counterpart,
)
from daimon.core.turn_hold import RequesterOrg
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.actions import UserMessage
from mux.contracts.config import BackendConfig
from mux.contracts.events import TextPart
from sqlalchemy import delete

from ..turn_path_support import TurnIOSpy
from .native_counterpart_support import application as application
from .native_counterpart_support import clean_sdk_environment as clean_sdk_environment
from .native_counterpart_support import db_preflight as db_preflight
from .native_counterpart_support import owned as owned
from .native_counterpart_support import owned_database_preflight as owned_database_preflight
from .native_counterpart_support import provider as provider
from .test_agent_native_runtime import explicit_worker_request as explicit_worker_request
from .test_backend_selection import initial_backend as initial_backend
from .test_backend_selection import selector_host as selector_host
from .test_fleet_default import _spawn_three_default_workers
from .test_frozen_root import freeze
from .test_frozen_root import root_turn as root_turn
from .test_gemini_codec import interaction
from .test_provider_dispatch import ACCOUNT, PROFILE, TENANT

if "DAIMON_CI_CONFIGURATION" not in os.environ:
    pytest.skip("the staging configuration matrix runs this module", allow_module_level=True)


def expected_enabled() -> bool:
    label = os.environ["DAIMON_CI_CONFIGURATION"]
    assert label in {"enabled", "legacy-off"}, "CI must name the configuration under test"
    return label == "enabled"


def selected_turn():
    """The turn switches the leg loaded, checked against the leg's independent label."""
    enabled = expected_enabled()
    turn = load_turn_settings(_env_file=None)
    assert turn.path == ("mux" if enabled else "legacy")
    assert turn.channel_backends is enabled
    assert turn.agent_backends is enabled
    return turn


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_ci_selected_agent_backend_proof_path(explicit_worker_request):
    """The agent switch decides whether the native counterpart is read and run.

    Agent backends ride on the mux path with channel backends, so the off leg's
    control first turns those on (still refused), then the agent switch (admitted).
    """
    request, _, owned, transport = explicit_worker_request
    turn = selected_turn()
    request = replace(
        request,
        deps=replace(
            request.deps,
            turn_path=turn.path,
            channel_backends=turn.channel_backends,
            agent_backends=turn.agent_backends,
        ),
    )
    owned.wire.requests.clear()
    if not turn.agent_backends:
        for path, channel_backends in ((turn.path, turn.channel_backends), ("mux", True)):
            request = replace(
                request,
                deps=replace(request.deps, turn_path=path, channel_backends=channel_backends),
            )
            with pytest.raises(AdmissionDenied, match="backend_unsupported"):
                await read_preparation_counterpart(request)
        assert not owned.wire.requests, "disabled agent backend must refuse before provider I/O"
        assert transport.requests == [], "disabled agent backend must not execute a provider turn"
        # Control: the same request with the agent switch also on is admitted.
        request = replace(request, deps=replace(request.deps, agent_backends=True))
    selected = await read_preparation_counterpart(request)
    assert selected.counterpart.agent.ref.id == owned.wire.native_id
    assert owned.wire.requests and all(
        wire_request.method == "GET" for wire_request in owned.wire.requests
    ), "an enabled agent backend must read its native counterpart"
    if not turn.agent_backends:
        return
    prepared = await bind_session_impl(
        request.deps,
        request.admission,
        tenant_id=request.tenant_id,
        platform=request.platform,
        external_user_id=request.external_user_id,
        thread_id=request.thread_id,
        session_account_id=request.session_account_id,
        reuse_existing=True,
        deadline=request.deadline,
        now=request.now,
        worker=request.worker,
    )
    assert prepared.backend is not None
    transport.responses.append(interaction("ci-worker-turn", status="completed"))
    await prepared.backend.events.send(
        request.scope,
        prepared.session_ref,
        (UserMessage(content=(TextPart(text="run the restricted task"),)),),
        key="ci-worker-input",
    )
    assert len(transport.requests) == 1
    assert transport.requests[0]["agent"] == selected.counterpart.agent.ref.id


async def test_ci_selected_fleet_admission_path(root_turn):
    """The fleet switch alone decides whether the admitted root gets a grant and workers."""
    deps, prepared, _, intent, _, scope, *_ = root_turn
    selected_turn()
    settings = load_settings(_env_file=None)
    assert settings.fleet.enabled is expected_enabled()
    assert deps.fleet_enabled, "the fixture's root is built with fleet on"
    selected = replace(deps, fleet_enabled=settings.fleet.enabled)
    async with deps.sessionmaker.begin() as db:
        await db.execute(delete(AgentBackendRevisionRecord))
    now = datetime.now(UTC)
    on_grant = await read_agent_fleet_grant(deps, prepared.admission, now=now)
    assert on_grant is not None and is_default_fleet(on_grant), "fleet on resolves the default"
    grant = await read_agent_fleet_grant(selected, prepared.admission, now=now)
    if settings.fleet.enabled:
        assert grant == on_grant
        specs = await _spawn_three_default_workers(root_turn)
        assert len(specs) == 3, "enabled fleet must admit three bounded workers"
        return
    assert grant is None, "disabled fleet must stop before grant lookup"
    await freeze(root_turn)
    with pytest.raises(WorkerSpecRefused):
        await read_frozen_root(selected, scope, intent_id=intent.id, now=datetime.now(UTC))
    # Control: the same frozen root is readable with only the fleet switch on.
    parent, _ = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    assert parent.fleet.depth == 0


async def _run_channel_turn(host, deps, thread_id: str):
    """Admit, bind and run one Slack turn on `deps`, as a platform adapter does."""
    admission = await admit_impl(
        deps,
        origin="chat",
        tenant_id=TENANT,
        requester_org=RequesterOrg.HOST,
        platform="slack",
        external_user_id="caller",
        channel_id="channel",
        thread_id=thread_id,
        now=datetime.now(UTC),
    )
    prepared = await bind_session_impl(
        deps,
        admission,
        tenant_id=TENANT,
        platform="slack",
        external_user_id="caller",
        thread_id=thread_id,
        session_account_id=ACCOUNT,
        reuse_existing=True,
    )

    async def reseed_user_message() -> str:
        return "which provider answers?"

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=TENANT,
        platform="slack",
        thread_id=thread_id,
        external_user_id="caller",
        user_message="which provider answers?",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=reseed_user_message,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        render_interval_s=0.01,
        operation_key=f"staging-config-{thread_id}",
    )
    await drain_outcomes()
    return admission, prepared, outcome


def _ma_sends(host) -> int:
    return len([r for r in host.calls if r.method == "POST" and r.url.path.endswith("/events")])


@pytest.mark.parametrize("selector_host", ["postgres"], indirect=True)
@pytest.mark.parametrize("initial_backend", [None], indirect=True)
async def test_ci_selected_channel_backend_execution_path(selector_host, turn_io_used: TurnIOSpy):
    """The default turn runs on the leg's path; a saved channel choice runs on its
    provider only when the leg turns it on."""
    host = selector_host
    turn = selected_turn()
    deps = replace(
        host.deps,
        turn_path=turn.path,
        channel_backends=turn.channel_backends,
        agent_backends=turn.agent_backends,
    )
    admission, _, outcome = await _run_channel_turn(host, deps, "default-thread")
    assert outcome.state.error is None, "the default Anthropic turn should complete"
    turn_io_used.assert_ran(turn.path)
    assert _ma_sends(host) == 1, "Managed Agents must run the default turn"
    assert host.wire.requests == [], "OpenAI must receive no request before a choice is saved"
    turn_io_used.classes.clear()

    saved = await host.change(BackendConfig(backend="openai", profile=PROFILE, model="gpt-6-luna"))
    assert saved.backend == "openai"
    admission, prepared, outcome = await _run_channel_turn(host, deps, "selected-thread")
    if turn.channel_backends:
        assert admission.backend_revision == saved, "the saved channel choice must be admitted"
        assert prepared.session_ref is not None and prepared.session_ref.provider == "openai"
        assert outcome.state.error is None, "the native turn should complete"
        assert turn_io_used.classes == [OpenAITurnIO], "the turn must run on OpenAI's turn I/O"
        assert any(r.method == "POST" for r in host.wire.requests), "OpenAI must run the turn"
        assert _ma_sends(host) == 1, "Managed Agents must not receive the chosen turn"
        return
    assert admission.backend_revision is None, "the off leg must ignore the saved choice"
    assert outcome.state.error is None, "the legacy turn should complete"
    turn_io_used.assert_ran("legacy")
    assert _ma_sends(host) == 2, "Managed Agents must run the turn"
    assert host.wire.requests == [], "OpenAI must receive no request"
    # Control: the same saved choice is admitted once mux and channel backends are on.
    enabled = replace(deps, turn_path="mux", channel_backends=True)
    on = await admit_impl(
        enabled,
        origin="chat",
        tenant_id=TENANT,
        requester_org=RequesterOrg.HOST,
        platform="slack",
        external_user_id="caller",
        channel_id="channel",
        thread_id="control-thread",
        now=datetime.now(UTC),
    )
    assert on.backend_revision == saved
