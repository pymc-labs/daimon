"""Offline session lifecycle requests, scopes, omissions and SDK error causes."""

from __future__ import annotations

from typing import cast

import httpx
import pytest
from anthropic import NotFoundError
from daimon.testing.ma_models import ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ResourceRef, Revision, Scope
from mux.contracts.resources import ResourceBinding, SessionSpec
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.sessions_lifecycle import AnthropicSessions
from mux.errors import ProviderError, ScopeViolation

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="auth"
)


def ref(kind: str, native_id: str) -> ResourceRef:
    return ResourceRef(
        id=native_id,
        kind=kind,
        provider="anthropic",
        account_scope_id="org",
        tenant_id=SCOPE.tenant_id,
        account_id=SCOPE.account_id,
    )


def spec(**extra: object) -> SessionSpec:
    return SessionSpec.model_validate(
        {
            "agent": ref("agent", "ag_1"),
            "environment": ref("environment", "env_1"),
            "agent_revision": Revision(local=0),
            "config_revision": 0,
            **extra,
        }
    )


@pytest.mark.parametrize("resources", [None, ()])
async def test_create_preserves_omitted_and_explicit_empty_resources(
    resources: tuple[()] | None,
) -> None:
    transport = ScriptedTransport()
    body = ma_session(id="sess_1").model_dump(mode="json")
    body["future_field"] = {"kept": True}
    transport.queue(ScriptedReply("POST", "/v1/sessions", httpx.Response(200, json=body)))
    async with transport.client() as client:
        port = AnthropicSessions(
            client,
            "org",
            ResourceAuthorization(SCOPE, frozenset({("agent", "ag_1"), ("environment", "env_1")})),
        )
        result = await port.create(
            SCOPE, spec(**({"resources": resources} if resources is not None else {})), key="create"
        )
    transport.assert_consumed()
    expected = {"agent": "ag_1", "environment_id": "env_1"}
    if resources is not None:
        expected["resources"] = []
    assert transport.requests[0].json() == expected
    assert result.ref.tenant_id == SCOPE.tenant_id
    assert result.ref.account_id == SCOPE.account_id
    assert cast(dict[str, object], result.native)["future_field"] == {"kept": True}


@pytest.mark.parametrize("foreign_kind", ["agent", "environment", "file"])
async def test_create_refuses_foreign_references_before_io(foreign_kind: str) -> None:
    transport = ScriptedTransport()
    foreign = ref(
        foreign_kind, {"agent": "ag_1", "environment": "env_1", "file": "file_1"}[foreign_kind]
    ).model_copy(update={"tenant_id": "other"})
    values: dict[str, object] = (
        {foreign_kind: foreign}
        if foreign_kind != "file"
        else {"resources": (ResourceBinding(id="mount", kind="artifact", resource=foreign),)}
    )
    async with transport.client() as client:
        port = AnthropicSessions(
            client,
            "org",
            ResourceAuthorization(
                SCOPE, frozenset({("agent", "ag_1"), ("environment", "env_1"), ("file", "file_1")})
            ),
        )
        with pytest.raises(ScopeViolation):
            await port.create(SCOPE, spec(**values), key="create")
    assert not transport.requests


async def test_retrieve_retains_native_not_found_as_provider_error_cause() -> None:
    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions/sess_1",
            httpx.Response(404, json={"error": {"type": "not_found_error", "message": "gone"}}),
        )
    )
    async with transport.client() as client:
        port = AnthropicSessions(
            client, "org", ResourceAuthorization(SCOPE, frozenset({("session", "sess_1")}))
        )
        with pytest.raises(ProviderError) as caught:
            await port.retrieve(SCOPE, ref("session", "sess_1"))
    transport.assert_consumed()
    assert caught.value.category == "not_found"
    assert isinstance(caught.value.__cause__, NotFoundError)


async def test_override_cannot_select_another_agent() -> None:
    transport = ScriptedTransport()
    config = ExtensionConfig(
        namespace="anthropic.session_create",
        version=1,
        value={
            "agent": {
                "type": "agent_with_overrides",
                "id": "foreign",
                "tools": [],
                "mcp_servers": [],
            }
        },
    )
    async with transport.client() as client:
        port = AnthropicSessions(
            client,
            "org",
            ResourceAuthorization(SCOPE, frozenset({("agent", "ag_1"), ("environment", "env_1")})),
        )
        with pytest.raises(ScopeViolation):
            await port.create(SCOPE, spec(extensions={config.namespace: config}), key="create")
    assert not transport.requests


async def test_create_preserves_override_tool_key_order_in_request_bytes() -> None:
    # SDK response models dump configs/default_config before the type tag;
    # schema reserialization would reorder those keys and change the request.
    agent = {
        "type": "agent_with_overrides",
        "id": "ag_1",
        "mcp_servers": [{"name": "server", "type": "url", "url": "https://mcp.example"}],
        "tools": [
            {
                "configs": [
                    {"name": "bash", "enabled": True, "permission_policy": {"type": "always_ask"}}
                ],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
                "type": "agent_toolset_20260401",
            }
        ],
        "skills": [{"skill_id": "skill_1", "type": "custom", "version": None}],
    }
    extension = ExtensionConfig(
        namespace="anthropic.session_create", version=1, value={"agent": agent}
    )
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply(
                "POST",
                "/v1/sessions",
                httpx.Response(200, json=ma_session().model_dump(mode="json")),
            )
        )
    async with old.client() as original, new.client() as client:
        from anthropic.types.beta.session_create_params import Agent

        await original.beta.sessions.create(agent=cast(Agent, agent), environment_id="env_1")
        port = AnthropicSessions(
            client,
            "org",
            ResourceAuthorization(SCOPE, frozenset({("agent", "ag_1"), ("environment", "env_1")})),
        )
        await port.create(SCOPE, spec(extensions={extension.namespace: extension}), key="create")
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests


@pytest.mark.parametrize(
    ("rows", "cursor", "more"), [(True, "next", True), (False, "next", False), (True, "", False)]
)
async def test_list_preserves_sdk_terminal_page_behavior(
    rows: bool, cursor: str, more: bool
) -> None:
    from mux.contracts.ids import PageRequest
    from mux.contracts.resources import SessionFilter

    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions",
            httpx.Response(
                200,
                json={
                    "data": [ma_session().model_dump(mode="json")] if rows else [],
                    "next_page": cursor,
                    "prev_page": None,
                },
            ),
        )
    )
    async with transport.client() as client:
        port = AnthropicSessions(client, "org", ResourceAuthorization(SCOPE))
        page = await port.list(SCOPE, filters=SessionFilter(), page=PageRequest())
    transport.assert_consumed()
    assert len(transport.requests) == 1
    assert page.has_more == more
    assert page.next_cursor == (cursor if more else None)


async def test_create_rejects_foreign_tenant_stamp_before_io() -> None:
    transport = ScriptedTransport()
    async with transport.client() as client:
        port = AnthropicSessions(
            client,
            "org",
            ResourceAuthorization(SCOPE, frozenset({("agent", "ag_1"), ("environment", "env_1")})),
        )
        with pytest.raises(ScopeViolation):
            await port.create(SCOPE, spec(metadata={"daimon_tenant": "foreign"}), key="create")
    assert not transport.requests


async def test_list_filters_foreign_tags_and_ungranted_untagged_sessions() -> None:
    from mux.contracts.ids import PageRequest
    from mux.contracts.resources import SessionFilter

    transport = ScriptedTransport()
    transport.queue(
        ScriptedReply(
            "GET",
            "/v1/sessions",
            httpx.Response(
                200,
                json={
                    "data": [
                        ma_session(id="allowed").model_dump(mode="json"),
                        ma_session(id="ungranted").model_dump(mode="json"),
                        ma_session(id="foreign", metadata={"daimon_tenant": "other"}).model_dump(
                            mode="json"
                        ),
                    ],
                    "next_page": None,
                    "prev_page": None,
                },
            ),
        )
    )
    async with transport.client() as client:
        port = AnthropicSessions(
            client,
            "org",
            ResourceAuthorization(
                SCOPE, frozenset({("session", "allowed"), ("session", "foreign")})
            ),
        )
        page = await port.list(SCOPE, filters=SessionFilter(), page=PageRequest())
    transport.assert_consumed()
    assert [session.ref.id for session in page.data] == ["allowed"]
