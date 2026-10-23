"""A copy's own turn on Gemini from the code-default agent template (J2).

No operator runtime file: the deployment's Gemini key alone serves the turn
through `runtime_defaults` ("daimon-agent", tenant-scoped, keeps Daimon's MCP).
Driven through the real capture, preparation, publication, bind and turn of the
faithful template fixture (origin channel, live MCP catalogue).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from daimon.core.provider_keys import DEFAULT_KEY_ENV
from daimon.core.turn.prepare import bind_session_impl
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.runtime_manifest import read_preparation_counterpart
from daimon.core.turn.runtimes import scope_channel_runtimes
from daimon.core.turn.state import extract_final_response
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.drivers.gemini.flash import FLASH_PRIMARY
from mux.profiles import INLINE_REUSE
from mux.profiles.gemini import ARGON_MODEL

from ..test_agent_backend import owned as owned
from .test_declared_drop_preparation import consent_request as consent_request
from .test_gemini_full_source import PARAMS
from .test_gemini_full_source import source_mcp as source_mcp
from .test_gemini_template_mcp import template as template


@pytest.mark.parametrize(
    ("consent_request", "model"),
    [(PARAMS, FLASH_PRIMARY), ({**PARAMS, "backend_model": ARGON_MODEL}, ARGON_MODEL)],
    indirect=["consent_request"],
)
async def test_a_copys_gemini_turn_runs_from_the_code_default_template(
    template, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    request, quote, _native, calls, _posts = template
    assert "mcp" not in quote.declared_drops, "the copy keeps Daimon's tools on Gemini"
    # Only the code default: no operator file, the deployment's own key.
    monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    monkeypatch.setenv(DEFAULT_KEY_ENV[INLINE_REUSE.provider], "offline-code-default")

    scoped, _lookup = scope_channel_runtimes(request)
    selected = await read_preparation_counterpart(scoped)
    assert selected.verified_counterpart == selected.counterpart
    (connection,) = selected.target_spec.mcp_servers
    assert connection.name == "daimon-mcp"

    prepared = await bind_session_impl(
        request.deps,
        request.admission,
        tenant_id=request.tenant_id,
        platform=request.platform,
        external_user_id=request.external_user_id,
        thread_id=request.thread_id,
        session_account_id=request.session_account_id,
        reuse_existing=True,
    )
    assert prepared.runtime.journal.deployment.agent.model.id == model

    async def no_recovery() -> None:
        pytest.fail("the code-default turn changed its admitted source/session")

    outcome = await run_prepared_turn(
        request.deps,
        prepared,
        tenant_id=request.tenant_id,
        platform=request.platform,
        thread_id=request.thread_id,
        external_user_id=request.external_user_id,
        user_message="hi copy, which model are you on?",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=no_recovery,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        operation_key=f"code-default:{model}",
    )
    assert outcome.state.error is None
    assert extract_final_response(outcome.state.content) == "answer"
    sent = [
        json.loads(c.content) for c in calls if c.method == "POST" and "interactions" in c.url.path
    ]
    assert sent and sent[-1]["agent_config"]["model"] == model
    assert any(t.get("type") == "mcp_server" for t in sent[-1].get("tools", []))
