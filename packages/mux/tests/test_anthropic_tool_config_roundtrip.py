"""Retrieved builtin config fields survive the authorized session override boundary."""

from __future__ import annotations

from pathlib import Path

import pytest
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ResourceRef, Revision, Scope
from mux.contracts.resources import SessionSpec
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.sessions_lifecycle import AnthropicSessions
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from pydantic import JsonValue, ValidationError

FIXTURES = Path(__file__).parent / "fixtures"
SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="auth"
)


def reference(kind: str, identity: str) -> ResourceRef:
    return ResourceRef(
        id=identity,
        kind=kind,
        provider="anthropic",
        account_scope_id="org",
        tenant_id=SCOPE.tenant_id,
        account_id=SCOPE.account_id,
    )


def session_spec(agent: dict[str, JsonValue]) -> SessionSpec:
    extension = ExtensionConfig(
        namespace="anthropic.session_create", version=1, value={"agent": agent}
    )
    return SessionSpec(
        agent=reference("agent", "ag_1"),
        environment=reference("environment", "env_1"),
        agent_revision=Revision(local=0),
        config_revision=0,
        extensions={extension.namespace: extension},
    )


@pytest.mark.parametrize(
    "change",
    [
        {"type": True},
        {"type": None},
        {"type": 123},
        {"type": []},
        {"type": {}},
        {"type": "unapproved"},
        {"type": "read"},
        {"unexpected": "field"},
        {"enabled": "true"},
        {"name": "unapproved"},
        {"permission_policy": {"type": "unapproved"}},
    ],
)
async def test_builtin_config_acceptance_keeps_the_native_fence(
    change: dict[str, JsonValue],
) -> None:
    config: dict[str, JsonValue] = {
        "name": "bash",
        "enabled": True,
        "permission_policy": {"type": "always_allow"},
        **change,
    }
    agent: dict[str, JsonValue] = {
        "type": "agent_with_overrides",
        "id": "ag_1",
        "tools": [{"type": "agent_toolset_20260401", "configs": [config]}],
    }
    transport = ScriptedTransport()
    async with transport.client() as sdk:
        port = AnthropicSessions(
            sdk,
            "org",
            ResourceAuthorization(SCOPE, frozenset({("agent", "ag_1"), ("environment", "env_1")})),
        )
        with pytest.raises(ValidationError):
            await port.create(SCOPE, session_spec(agent), key="create")
    assert not transport.requests
