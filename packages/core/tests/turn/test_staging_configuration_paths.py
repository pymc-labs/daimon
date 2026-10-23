"""The CI staging switches select real agent and fleet paths.

Only the `pytest-staging-config` job runs this module: it sets the four switches
from its matrix and names the configuration in `DAIMON_CI_CONFIGURATION`, so a
leg whose switches drift from its name fails here instead of passing silently.
Each refusal on the off leg is paired with a control that turns the switch on
and shows the same request admitted, so the refusal is the switch's doing.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from daimon.core._models import AgentBackendRevisionRecord
from daimon.core.config import load_settings, load_turn_settings
from daimon.core.fleet_configuration import is_default_fleet
from daimon.core.neutral_resources import WorkerSpecRefused
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.prepare import bind_session_impl
from daimon.core.turn.runtime_manifest import (
    read_agent_fleet_grant,
    read_frozen_root,
    read_preparation_counterpart,
)
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from sqlalchemy import delete

from .native_counterpart_support import application as application
from .native_counterpart_support import clean_sdk_environment as clean_sdk_environment
from .native_counterpart_support import db_preflight as db_preflight
from .native_counterpart_support import owned as owned
from .native_counterpart_support import owned_database_preflight as owned_database_preflight
from .native_counterpart_support import provider as provider
from .test_agent_native_runtime import explicit_worker_request as explicit_worker_request
from .test_fleet_default import _spawn_three_default_workers
from .test_frozen_root import freeze
from .test_frozen_root import root_turn as root_turn
from .test_gemini_codec import interaction

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
