"""A copy's own turn on Gemini from the code-default agent template (J2).

No operator runtime file: the deployment's Gemini key alone serves the turn
through `runtime_defaults` ("daimon-agent", tenant-scoped, keeps Daimon's MCP).
Driven through the real capture, preparation, publication, bind and turn of the
faithful template fixture (origin channel, live MCP catalogue).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

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


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize("failure", ["own", "deeper"])
async def test_a_skipped_mcp_representation_is_logged_without_content(
    template, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """J2's Argon preview dropped MCP with no trace. A capture that can't
    represent the source now says so once: the error class and the ids, and the
    capture's own fixed sentence, never text raised from deeper down."""
    from dataclasses import replace

    from daimon.core.config import McpSettings
    from daimon.core.mcp_oauth import discovery
    from daimon.core.turn.runtime_manifest import capture_preparation_source
    from structlog.testing import capture_logs

    request, *_ = template
    if failure == "own":
        # The capture itself refuses: no MCP server to read the catalogue from.
        request = replace(request, deps=replace(request.deps, mcp=McpSettings()))
    else:

        async def listed(_url: str, _token: str) -> frozenset[str]:
            raise ValueError("catalogue body with secret-content")

        monkeypatch.setattr(discovery, "list_mcp_tools", listed)
    with capture_logs() as logs:
        source = await capture_preparation_source(request, capability_preview=True)
    assert source.representations == ()
    (entry,) = [e for e in logs if e["event"] == "source_capture.representation_skipped"]
    assert entry["log_level"] == "warning" and entry["error"] == "ValueError"
    assert entry["agent_id"] == str(source.agent_id)
    assert entry["tenant_id"] == str(source.tenant_id)
    if failure == "own":
        assert entry["reason"] == "MCP catalogue requires the captured caller"
    else:
        assert entry["reason"] is None
        assert "secret-content" not in repr(entry)


@pytest.mark.parametrize("consent_request", [PARAMS], indirect=True)
@pytest.mark.parametrize("retry_delay", ["5s", "900s"])
async def test_a_free_tier_rate_limit_is_waited_out_within_the_turn(
    template, monkeypatch: pytest.MonkeyPatch, retry_delay: str
) -> None:
    """Free-tier Flash (no paid key): Google's 429 with a refilling quota is
    retried inside the turn's deadline and answered; a delay past the deadline
    ends the turn at once with the free-quota words."""
    import httpx
    from daimon.core.turn.notices import provider_quota_copy
    from mux.drivers.gemini import flash

    request, _quote, _native, calls, _posts = template
    monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    monkeypatch.setenv(DEFAULT_KEY_ENV[INLINE_REUSE.provider], "offline-code-default")
    slept: list[float] = []

    async def no_wait(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(flash, "_sleep", no_wait)
    original = httpx.MockTransport.handle_async_request
    limited = {"left": 1}

    async def free_tier(self, call):
        if call.method == "POST" and "interactions" in call.url.path and limited["left"]:
            limited["left"] -= 1
            return httpx.Response(
                429,
                json={
                    "error": {
                        "code": 429,
                        "status": "RESOURCE_EXHAUSTED",
                        "message": "Quota exceeded, limit: 15, model: gemini-3.8-flash",
                        "details": [
                            {
                                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                "violations": [
                                    {
                                        "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                                        "quotaDimensions": {"model": "gemini-3.8-flash"},
                                    }
                                ],
                            },
                            {
                                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                                "retryDelay": retry_delay,
                            },
                        ],
                    }
                },
            )
        return await original(self, call)

    monkeypatch.setattr(httpx.MockTransport, "handle_async_request", free_tier)
    assert flash.CREATE_DEADLINE.get() is None
    scoped, _lookup = scope_channel_runtimes(request)
    await read_preparation_counterpart(scoped)
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

    async def no_recovery() -> None:
        pytest.fail("a rate-limited create changed its admitted source/session")

    outcome = await run_prepared_turn(
        request.deps,
        prepared,
        tenant_id=request.tenant_id,
        platform=request.platform,
        thread_id=request.thread_id,
        external_user_id=request.external_user_id,
        user_message="hi copy",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=no_recovery,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        operation_key=f"free-tier:{retry_delay}",
        # Two minutes left: Google's 5 s fits before it, its 15 min does not.
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )
    assert flash.CREATE_DEADLINE.get() is None, "the turn's deadline ends with the turn"
    # The 429 is answered above, before the fixture's provider records a call.
    assert limited["left"] == 0, "Google rate-limited the first create"
    created = [c for c in calls if c.method == "POST" and "interactions" in c.url.path]
    if retry_delay == "5s":
        assert outcome.state.error is None
        assert extract_final_response(outcome.state.content) == "answer"
        assert slept == [5.0] and len(created) == 1, "one retry, then the answer"
    else:
        assert outcome.state.error is not None
        assert slept == [] and created == [], "no retry past the deadline"
        assert provider_quota_copy(outcome.state.error) == (
            "Google's free quota is used up.",
            "Try again in 15 min.",
        )
