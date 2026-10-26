"""Ordinary agent operation and configuration paths, using offline providers."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from daimon.core.config import load_turn_settings
from daimon.core.turn.admission import admit_impl
from daimon.core.turn.openai_io import OpenAITurnIO
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.prepare import bind_session_impl
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn_hold import RequesterOrg
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.config import BackendConfig

from ..turn_path_support import TurnIOSpy
from .native_counterpart_support import application as application
from .native_counterpart_support import clean_sdk_environment as clean_sdk_environment
from .native_counterpart_support import db_preflight as db_preflight
from .native_counterpart_support import owned as owned
from .native_counterpart_support import owned_database_preflight as owned_database_preflight
from .native_counterpart_support import provider as provider
from .test_backend_selection import initial_backend as initial_backend
from .test_backend_selection import selector_host as selector_host
from .test_provider_dispatch import ACCOUNT, PROFILE, TENANT

if "DAIMON_CI_CONFIGURATION" not in os.environ:
    pytest.skip("the staging configuration matrix runs this module", allow_module_level=True)


# Each CI leg's label and the switches it must load: everything on (staging),
# mux with both backend switches off (the first production activation step) and
# everything off (production today).
LEGS = {
    "enabled": ("mux", True, True),
    "mux-only": ("mux", False, False),
    "legacy-off": ("legacy", False, False),
}


def selected_turn():
    """The turn switches the leg loaded, checked against the leg's independent label."""
    label = os.environ["DAIMON_CI_CONFIGURATION"]
    assert label in LEGS, "CI must name the configuration under test"
    path, channel_backends, agent_backends = LEGS[label]
    turn = load_turn_settings(_env_file=None)
    assert turn.path == path
    assert turn.channel_backends is channel_backends
    assert turn.agent_backends is agent_backends
    return turn


async def _run_channel_turn(host, deps, thread_id: str):
    """Admit, bind and run one Slack turn on `deps`, as a platform adapter does."""
    admission = await admit_impl(
        deps,
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
    assert outcome.state.error is None, "the Anthropic turn should complete"
    turn_io_used.assert_ran(turn.path)
    assert _ma_sends(host) == 2, "Managed Agents must run the turn"
    assert host.wire.requests == [], "OpenAI must receive no request"
    # Control: the same saved choice is admitted once mux and channel backends are on.
    enabled = replace(deps, turn_path="mux", channel_backends=True)
    on = await admit_impl(
        enabled,
        tenant_id=TENANT,
        requester_org=RequesterOrg.HOST,
        platform="slack",
        external_user_id="caller",
        channel_id="channel",
        thread_id="control-thread",
        now=datetime.now(UTC),
    )
    assert on.backend_revision == saved
