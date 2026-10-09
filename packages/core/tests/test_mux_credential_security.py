"""Credential material stays in the host and one SDK request, never public DTOs/errors."""

import logging
import traceback
from collections import deque

import httpx
import pytest
from anthropic import APIStatusError
from daimon.core import mux_compat as compat
from daimon.core.mux_backend import managed_agents, platform_scope, resource_ref
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import Scope
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.credential_schemas import CredentialCreate
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.resources.vaults import Vaults
from mux.errors import ScopeViolation

MATERIALS = ["dummy-access-material", "dummy-refresh-material", "dummy-client-material"]
AUTH = {
    "type": "mcp_oauth",
    "mcp_server_url": "https://example.test/mcp",
    "access_token": MATERIALS[0],
    "refresh": {
        "client_id": "client",
        "token_endpoint": "https://example.test/token",
        "refresh_token": MATERIALS[1],
        "token_endpoint_auth": {"type": "client_secret_post", "client_secret": MATERIALS[2]},
    },
}
SCOPE = platform_scope("test credential secrecy")


@pytest.mark.parametrize("status", [400, 401, 403, 409, 422, 500])
async def test_vault_failure_retains_sdk_status_and_copy_without_echoed_material(status):
    body = {
        "type": "error",
        "error": {"type": "api_error", "message": "echo " + ",".join(MATERIALS)},
    }
    sdk = ScriptedTransport(
        deque(
            [
                ScriptedReply(
                    "POST", "/v1/vaults/vault/credentials", httpx.Response(status, json=body)
                )
            ]
        )
    )
    async with sdk.client() as client:
        with pytest.raises(APIStatusError) as failure:
            await compat.create_credential(client, "vault", {"auth": AUTH}, scope=SCOPE)
    error = failure.value
    assert error.status_code == status
    assert error.request.method == "POST"
    assert error.request.url.path == "/v1/vaults/vault/credentials"
    assert error.request.content == b""
    assert "authorization" not in error.request.headers
    displayed = "\n".join(
        [str(error), repr(error), repr(error.body), "".join(traceback.format_exception(error))]
    )
    for material in MATERIALS:
        assert material not in displayed
    assert "[redacted]" in str(error)
    assert len(sdk.requests) == 1
    sdk.assert_consumed()


async def test_closed_specs_and_nested_response_snapshots_exclude_secret_values():
    refs, values = compat._credential_refs({"auth": AUTH})
    config = CredentialCreate.model_validate(refs)
    for material in MATERIALS:
        assert material not in repr(config)
        assert material not in config.model_dump_json()
    response = {
        "id": "credential",
        "type": "vault_credential",
        "display_name": "test",
        "metadata": {},
        "vault_id": "vault",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "auth": AUTH,
    }
    sdk = ScriptedTransport(
        deque(
            [
                ScriptedReply(
                    "POST", "/v1/vaults/vault/credentials", httpx.Response(200, json=response)
                )
            ]
        )
    )
    try:
        async with sdk.client() as client:
            backend = managed_agents(client, scope=SCOPE, secrets=lambda scope, ref: values[ref])
            port = backend.extension(Vaults, namespace="anthropic.vaults", version=1)
            record = await port.create_credential(
                SCOPE, resource_ref(backend, "vault", "vault", scope=SCOPE), config, key="request"
            )
            for material in MATERIALS:
                assert material not in repr(record)
                assert material not in record.model_dump_json()
            assert record.kind == "mcp_oauth"
    finally:
        values.clear()
    sdk.assert_consumed()


async def test_vault_and_session_foreign_references_fail_before_io():
    tenant = Scope(
        tenant_id="tenant", account_id="account", principal_id="host", authorization_id="grant"
    )
    sdk = ScriptedTransport()
    async with sdk.client() as client:
        backend = AnthropicManagedAgents(
            client,
            authorization=ResourceAuthorization(
                tenant, frozenset({("vault", "vault"), ("session", "session")})
            ),
        )
        port = backend.extension(Vaults, namespace="anthropic.vaults", version=1)
        vault = resource_ref(backend, "vault", "vault", scope=tenant).model_copy(
            update={"tenant_id": "foreign"}
        )
        session = resource_ref(backend, "session", "session", scope=tenant).model_copy(
            update={"tenant_id": "foreign"}
        )
        with pytest.raises(ScopeViolation):
            await port.archive(tenant, vault, key="archive")
        with pytest.raises(ScopeViolation):
            await backend.session_admin.archive(tenant, session, key="archive")
    assert not sdk.requests


async def test_tenant_vault_lists_expose_only_host_granted_ids_without_extra_requests():
    from mux.contracts.ids import PageRequest

    tenant = Scope(
        tenant_id="tenant", account_id="account", principal_id="host", authorization_id="grant"
    )
    rows = [
        {
            "id": native_id,
            "type": "vault",
            "display_name": native_id,
            "metadata": {"daimon_tenant": "another-tenant"} if native_id == "retagged" else {},
            "created_at": "2026-01-01T00:00:00Z",
        }
        for native_id in ("owned", "foreign", "retagged")
    ]
    sdk = ScriptedTransport(
        deque(
            [
                ScriptedReply(
                    "GET", "/v1/vaults", httpx.Response(200, json={"data": rows, "next_page": None})
                )
                for _ in range(3)
            ]
        )
    )
    async with sdk.client() as client:
        backend = AnthropicManagedAgents(
            client,
            authorization=ResourceAuthorization(
                tenant, frozenset({("vault", "owned"), ("vault", "retagged")})
            ),
        )
        port = backend.extension(Vaults, namespace="anthropic.vaults", version=1)
        page = await port.list(tenant, page=PageRequest())
        walked = [vault async for vault in port.walk(tenant)]
        privileged = await port.list(SCOPE, page=PageRequest())
        assert [vault.ref.id for vault in page.data] == ["owned"]
        assert [vault.ref.id for vault in walked] == ["owned"]
        assert [vault.ref.id for vault in privileged.data] == ["owned", "foreign", "retagged"]
    assert len(sdk.requests) == 3
    sdk.assert_consumed()


async def test_env_file_failure_drops_multipart_secret_request_and_redacts_echo():
    material = "dummy-env-secret"
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": material}}
    sdk = ScriptedTransport(
        deque([ScriptedReply("POST", "/v1/files", httpx.Response(400, json=body))])
    )
    async with sdk.client() as client:
        with pytest.raises(APIStatusError) as failure:
            await compat.upload_credential_file(
                client,
                ("KEY=" + material + "\n").encode(),
                filename=".env",
                media_type="text/plain",
                secret_values=[material],
                scope=SCOPE,
            )
    assert material not in str(failure.value)
    assert material not in repr(failure.value.body)
    assert failure.value.request.content == b""
    assert material not in "".join(traceback.format_exception(failure.value))
    assert len(sdk.requests) == 1
    sdk.assert_consumed()


async def test_same_key_sends_twice_and_sdk_debug_logs_never_keep_material(caplog):
    # A newline also exercises the SDK's repr-escaped request-options log.
    material = "dummy-log-secret\nsecond-line"
    reference = "opaque-log-secret"
    config = CredentialCreate.model_validate(
        {
            "auth": {
                "type": "static_bearer",
                "mcp_server_url": "https://example.test/mcp",
                "token_ref": reference,
            }
        }
    )
    response = {
        "id": "credential",
        "type": "vault_credential",
        "vault_id": "vault",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "auth": {"type": "static_bearer", "mcp_server_url": "https://example.test/mcp"},
    }
    sdk = ScriptedTransport(
        deque(
            [
                ScriptedReply(
                    "POST", "/v1/vaults/vault/credentials", httpx.Response(200, json=response)
                )
                for _ in range(2)
            ]
        )
    )
    caplog.set_level(logging.DEBUG, logger="anthropic._base_client")
    async with sdk.client() as client:
        backend = managed_agents(client, scope=SCOPE, secrets=lambda scope, ref: material)
        port = backend.extension(Vaults, namespace="anthropic.vaults", version=1)
        vault = resource_ref(backend, "vault", "vault", scope=SCOPE)
        for _ in range(2):
            await port.create_credential(SCOPE, vault, config, key="same-key")
    assert len(sdk.requests) == 2
    sdk.assert_consumed()
    assert "Request options:" in caplog.text
    assert "[redacted]" in caplog.text
    for displayed in (caplog.text, repr(caplog.records), repr(config)):
        assert material not in displayed
        assert repr(material)[1:-1] not in displayed
        assert "dummy-log-secret" not in displayed
    # Credential I/O must leave no logger context or retained request material.
    from mux.drivers.anthropic.resources._secrets import _active_materials

    assert _active_materials.get() is None


async def test_sdk_error_repr_redacts_escaped_multiline_credential():
    material = "dummy-escaped-secret\nsecond-line"
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": material}}
    sdk = ScriptedTransport(
        deque(
            [ScriptedReply("POST", "/v1/vaults/vault/credentials", httpx.Response(400, json=body))]
        )
    )
    async with sdk.client() as client:
        with pytest.raises(APIStatusError) as failure:
            await compat.create_credential(
                client,
                "vault",
                {
                    "auth": {
                        "type": "static_bearer",
                        "mcp_server_url": "https://example.test/mcp",
                        "token": material,
                    }
                },
                scope=SCOPE,
            )
    displayed = "\n".join(
        [
            str(failure.value),
            repr(failure.value),
            repr(failure.value.body),
            "".join(traceback.format_exception(failure.value)),
        ]
    )
    assert "dummy-escaped-secret" not in displayed
    assert "[redacted]" in displayed
    sdk.assert_consumed()


async def test_two_repo_resource_tokens_are_resolved_and_redacted_together_on_first_failure(caplog):
    from typing import Literal

    from mux.drivers.anthropic.resources._secrets import _active_materials, credential_request
    from mux.drivers.anthropic.schemas import NativeConfig

    class Repo(NativeConfig):
        type: Literal["github_repository"]
        url: str
        authorization_token_ref: str

    class Create(NativeConfig):
        resources: list[Repo]
        metadata: dict[str, str]

    materials = {
        "opaque-first": "dummy-first-repo\nsecond-line",
        "opaque-second": "dummy-second-repo",
    }
    config = Create(
        resources=[
            Repo(
                type="github_repository",
                url="https://github.com/org/first",
                authorization_token_ref="opaque-first",
            ),
            Repo(
                type="github_repository",
                url="https://github.com/org/second",
                authorization_token_ref="opaque-second",
            ),
        ],
        metadata={"authorization_token_ref": "metadata-is-not-a-reference"},
    )
    lookups = []

    def resolve(scope, ref):
        assert scope == SCOPE
        lookups.append(ref)
        return materials[ref]

    body = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "first repository failed: " + materials["opaque-first"],
        },
        "second_repository_token": materials["opaque-second"],
    }
    sdk = ScriptedTransport(
        deque([ScriptedReply("POST", "/v1/sessions", httpx.Response(400, json=body))])
    )
    caplog.set_level(logging.DEBUG, logger="anthropic._base_client")
    async with sdk.client() as client:

        async def send(kwargs):
            return await client.beta.sessions.create(
                agent="agent", environment_id="environment", **kwargs
            )

        with pytest.raises(APIStatusError) as failure:
            await compat.legacy_call(credential_request(SCOPE, config, resolve, send))
    assert lookups == ["opaque-first", "opaque-second"]
    expected_resources = [
        {
            "type": "github_repository",
            "url": "https://github.com/org/first",
            "authorization_token": materials["opaque-first"],
        },
        {
            "type": "github_repository",
            "url": "https://github.com/org/second",
            "authorization_token": materials["opaque-second"],
        },
    ]
    assert sdk.requests[0].json() == {
        "agent": "agent",
        "environment_id": "environment",
        "resources": expected_resources,
        "metadata": {"authorization_token_ref": "metadata-is-not-a-reference"},
    }
    assert failure.value.request.content == b""
    displayed = "\n".join(
        [
            str(failure.value),
            repr(failure.value),
            repr(failure.value.body),
            "".join(traceback.format_exception(failure.value)),
            caplog.text,
            repr(caplog.records),
            repr(config),
            config.model_dump_json(),
        ]
    )
    assert "Request options:" in caplog.text
    assert "[redacted]" in caplog.text
    for material in materials.values():
        assert material not in displayed
        assert repr(material)[1:-1] not in displayed
    assert _active_materials.get() is None
    assert len(sdk.requests) == 1
    sdk.assert_consumed()
