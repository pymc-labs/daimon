"""Pure planning and byte-identical in-place session requests."""

from __future__ import annotations

from typing import cast

import httpx
import pytest
from anthropic import AsyncAnthropic, BadRequestError
from anthropic.types.beta.session_update_params import SessionUpdateParams
from daimon.testing.ma_models import ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ResourceRef, Revision, Scope
from mux.contracts.resources import SessionSpec
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.sessions_lifecycle import AnthropicSessions
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="auth"
)
REVISION = Revision(local=7, native="7")


def ref(kind: str, native_id: str) -> ResourceRef:
    return ResourceRef(
        id=native_id,
        kind=kind,
        provider="anthropic",
        account_scope_id="org",
        tenant_id="tenant",
        account_id="account",
    )


def spec(payload: dict[str, object]) -> SessionSpec:
    return SessionSpec.model_validate(
        {
            "agent": ref("agent", "ag_1"),
            "agent_revision": REVISION,
            "config_revision": 0,
            "extensions": {
                "anthropic.session_update": ExtensionConfig.model_validate(
                    {"namespace": "anthropic.session_update", "version": 1, "value": payload}
                )
            },
        }
    )


def port(client: AsyncAnthropic) -> AnthropicSessions:
    return AnthropicSessions(
        client,
        "org",
        ResourceAuthorization(SCOPE, frozenset({("agent", "ag_1"), ("session", "sess_1")})),
    )


VALID_UPDATES: list[dict[str, object]] = [
    {"agent": {"tools": [], "mcp_servers": []}},
    {
        "agent": {
            "tools": [
                {
                    "configs": [],
                    "default_config": {"enabled": False},
                    "type": "agent_toolset_20260401",
                }
            ],
            "mcp_servers": [{"name": "mcp", "type": "url", "url": "https://mcp.example"}],
        }
    },
    {"metadata": {}},
    {"agent": None},
]


@pytest.mark.parametrize("payload", VALID_UPDATES)
async def test_plan_is_pure_and_apply_matches_legacy_bytes(payload: dict[str, object]) -> None:
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply(
                "POST",
                "/v1/sessions/sess_1",
                httpx.Response(200, json=ma_session(id="sess_1").model_dump(mode="json")),
            )
        )
    async with old.client() as legacy, new.client() as client:
        await legacy.beta.sessions.update("sess_1", **cast(SessionUpdateParams, payload))
        sessions = port(client)
        plan = await sessions.plan_update(SCOPE, ref("session", "sess_1"), spec(payload))
        assert not new.requests
        assert plan.action == "in_place"
        receipt = await sessions.apply_update(SCOPE, plan, expected=REVISION, key="apply")
        assert receipt.status == "processed"
        assert receipt.applies == "now"
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests


async def test_mismatched_plan_revision_fails_before_io() -> None:
    transport = ScriptedTransport()
    async with transport.client() as client:
        sessions = port(client)
        plan = await sessions.plan_update(
            SCOPE, ref("session", "sess_1"), spec({"agent": {"tools": []}})
        )
        with pytest.raises(ProviderError, match="conflict"):
            await sessions.apply_update(SCOPE, plan, expected=Revision(local=8), key="apply")
    assert not transport.requests


async def test_busy_retains_native_error_cause() -> None:
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "POST",
            "/v1/sessions/sess_1",
            httpx.Response(
                400,
                json={
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Cannot update agent while session is running",
                    }
                },
            ),
        )
    )
    async with transport.client() as client:
        sessions = port(client)
        plan = await sessions.plan_update(
            SCOPE, ref("session", "sess_1"), spec({"agent": {"tools": []}})
        )
        with pytest.raises(ProviderError) as caught:
            await sessions.apply_update(SCOPE, plan, expected=REVISION, key="apply")
    transport.assert_consumed()
    assert isinstance(caught.value.__cause__, BadRequestError)
    assert "while session is running" in str(caught.value.__cause__)


@pytest.mark.parametrize(
    "payload",
    [
        {"vault_ids": []},
        {"agent": {"id": "other"}},
        {"agent": {"skills": []}},
        {"metadata": {"daimon_tenant": "other"}},
    ],
)
async def test_invalid_update_payload_fails_before_io(payload: dict[str, object]) -> None:
    transport = ScriptedTransport()
    async with transport.client() as client:
        with pytest.raises((ValueError, ScopeViolation)):
            await port(client).plan_update(SCOPE, ref("session", "sess_1"), spec(payload))
    assert not transport.requests


async def test_fresh_state_plan_never_replaces_a_thread() -> None:
    transport = ScriptedTransport()
    async with transport.client() as client:
        sessions = port(client)
        desired = spec({"agent": {"tools": []}}).model_copy(update={"state_mode": "fresh"})
        plan = await sessions.plan_update(SCOPE, ref("session", "sess_1"), desired)
        assert plan.action == "refuse"
        with pytest.raises(UnsupportedCapability):
            await sessions.apply_update(SCOPE, plan, expected=REVISION, key="apply")
    assert not transport.requests


async def test_generic_desired_revision_is_not_silently_claimed_as_reuse() -> None:
    transport = ScriptedTransport()
    async with transport.client() as client:
        sessions = port(client)
        desired = spec({}).model_copy(update={"extensions": {}})
        plan = await sessions.plan_update(SCOPE, ref("session", "sess_1"), desired)
        assert plan.action == "refuse"
        with pytest.raises(UnsupportedCapability):
            await sessions.apply_update(SCOPE, plan, expected=REVISION, key="apply")
    assert not transport.requests


async def test_explicit_empty_patch_reuses_without_a_provider_request() -> None:
    transport = ScriptedTransport()
    async with transport.client() as client:
        sessions = port(client)
        plan = await sessions.plan_update(SCOPE, ref("session", "sess_1"), spec({}))
        assert plan.action == "reuse"
        receipt = await sessions.apply_update(SCOPE, plan, expected=REVISION, key="noop")
        assert receipt.status == "processed"
    assert not transport.requests
