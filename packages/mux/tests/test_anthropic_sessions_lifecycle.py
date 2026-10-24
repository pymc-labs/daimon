"""Offline session lifecycle requests, scopes, omissions and SDK error causes."""

from __future__ import annotations

import json
from typing import cast

import httpx
import pytest
from anthropic import NotFoundError
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ResourceRef, Revision, Scope
from mux.contracts.resources import ResourceBinding, SessionSpec
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.sessions_lifecycle import AnthropicSessions
from mux.errors import ProviderError, ScopeViolation
from mux.testing.drivers.anthropic.ma_models import ma_session
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport
from pydantic import JsonValue, ValidationError

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
    expected: dict[str, JsonValue] = {"agent": "ag_1", "environment_id": "env_1"}
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


@pytest.mark.parametrize("model_fields", [{}, {"model": None}, {"model": "claude-haiku-5-5"}])
async def test_override_cannot_select_another_agent(model_fields: dict[str, JsonValue]) -> None:
    transport = ScriptedTransport()
    config = ExtensionConfig(
        namespace="anthropic.session_create",
        version=1,
        value={
            "agent": {
                "type": "agent_with_overrides",
                "id": "foreign",
                **model_fields,
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


@pytest.mark.parametrize(
    "model_fields",
    [{}, {"model": None}, {"model": "claude-haiku-5-5"}, {"system": "You are Daimon."}],
)
async def test_create_preserves_override_tool_key_order_in_request_bytes(
    model_fields: dict[str, JsonValue],
) -> None:
    # SDK response models dump configs/default_config before the type tag;
    # schema reserialization would reorder those keys and change the request.
    agent: dict[str, JsonValue] = {
        "type": "agent_with_overrides",
        "id": "ag_1",
        **model_fields,
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
    "override_fields",
    [{"model": 42}, {"model": False}, {"model": {"id": "claude-haiku-5-5"}}, {"unapproved": True}],
)
async def test_create_rejects_invalid_override_fields_before_io(
    override_fields: dict[str, JsonValue],
) -> None:
    transport = ScriptedTransport()
    extension = ExtensionConfig(
        namespace="anthropic.session_create",
        version=1,
        value={"agent": {"type": "agent_with_overrides", "id": "ag_1", **override_fields}},
    )
    async with transport.client() as client:
        port = AnthropicSessions(
            client,
            "org",
            ResourceAuthorization(SCOPE, frozenset({("agent", "ag_1"), ("environment", "env_1")})),
        )
        with pytest.raises(ValidationError):
            await port.create(
                SCOPE, spec(extensions={extension.namespace: extension}), key="create"
            )
    assert not transport.requests


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


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("credential", [False, True])
async def test_claimed_session_create_lost_ack_posts_once_with_production_retries(
    configured: bool,
    credential: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from mux.drivers.anthropic.transport import single_attempt_mutation

    transport = ScriptedTransport()
    material = "offline-repository-credential-marker"
    transport.queue(ScriptedReply("POST", "/v1/sessions", httpx.ReadError(material)))
    mounts = (
        (
            ResourceBinding(
                id="repository",
                kind="repository",
                native=ExtensionConfig(
                    namespace="anthropic.session_resource_create",
                    version=1,
                    value={
                        "resource": {
                            "type": "github_repository",
                            "url": "https://github.com/example/repo",
                            "authorization_token_ref": "repository-credential",
                        }
                    },
                ),
            ),
        )
        if credential
        else ()
    )
    async with transport.client(max_retries=8) as client:
        port = AnthropicSessions(
            client,
            "org",
            ResourceAuthorization(SCOPE, frozenset({("agent", "ag_1"), ("environment", "env_1")})),
            secrets=lambda scope, reference: material,
        )
        with single_attempt_mutation(), pytest.raises(ProviderError) as caught:
            await port.create(
                SCOPE,
                spec(
                    resources=mounts,
                    **(
                        {
                            "extensions": {
                                "anthropic.session_create": ExtensionConfig(
                                    namespace="anthropic.session_create",
                                    version=1,
                                    value={
                                        "budget": {
                                            "type": "limit",
                                            "max_list_cost": {"amount": "1", "currency": "USD"},
                                        }
                                    },
                                )
                            }
                        }
                        if configured
                        else {}
                    ),
                ),
                key="allocation",
            )
        assert client.max_retries == 8
    transport.assert_consumed()
    assert len(transport.requests) == 1
    if credential:
        assert transport.requests[0].json()["resources"][0]["authorization_token"] == material
        assert material not in str(caught.value) + repr(caught.value.__cause__) + caplog.text


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("repository", [False, True])
async def test_native_budget_wire_and_omitted_byte_parity(configured, repository):
    transport = ScriptedTransport()
    body = ma_session(id="sess_1").model_dump(mode="json")
    transport.queue(ScriptedReply("POST", "/v1/sessions", httpx.Response(200, json=body)))
    budget = {"type": "limit", "max_list_cost": {"amount": "1", "currency": "USD"}}
    mounts = (
        (
            ResourceBinding(
                id="repository",
                kind="repository",
                native=ExtensionConfig(
                    namespace="anthropic.session_resource_create",
                    version=1,
                    value={
                        "resource": {
                            "type": "github_repository",
                            "url": "https://github.com/example/repo",
                            "authorization_token_ref": "credential",
                        }
                    },
                ),
            ),
        )
        if repository
        else ()
    )
    values = {"resources": mounts} if repository else {}
    if configured:
        values["extensions"] = {
            "anthropic.session_create": ExtensionConfig(
                namespace="anthropic.session_create", version=1, value={"budget": budget}
            )
        }
    async with transport.client() as client:
        port = AnthropicSessions(
            client,
            "org",
            ResourceAuthorization(SCOPE, frozenset({("agent", "ag_1"), ("environment", "env_1")})),
            secrets=lambda scope, reference: "offline-credential",
        )
        await port.create(SCOPE, spec(**values), key="budgeted")
    transport.assert_consumed()
    expected = {"agent": "ag_1", "environment_id": "env_1"}
    if repository:
        expected["resources"] = [
            {
                "type": "github_repository",
                "url": "https://github.com/example/repo",
                "authorization_token": "offline-credential",
            }
        ]
    if configured:
        expected["budget"] = budget
    assert transport.requests[0].json() == expected
    assert transport.requests[0].body == json.dumps(expected, separators=(",", ":")).encode()


@pytest.mark.parametrize("amount", [True, 1, 1.0, "0", "01", "-1", "1.5"])
async def test_invalid_native_budget_refuses_before_sdk(amount):
    transport = ScriptedTransport()
    async with transport.client() as client:
        port = AnthropicSessions(
            client,
            "org",
            ResourceAuthorization(SCOPE, frozenset({("agent", "ag_1"), ("environment", "env_1")})),
        )
        with pytest.raises(ValidationError):
            await port.create(
                SCOPE,
                spec(
                    extensions={
                        "anthropic.session_create": ExtensionConfig(
                            namespace="anthropic.session_create",
                            version=1,
                            value={
                                "budget": {
                                    "type": "limit",
                                    "max_list_cost": {"amount": amount, "currency": "USD"},
                                }
                            },
                        )
                    }
                ),
                key="invalid",
            )
    assert transport.requests == []
