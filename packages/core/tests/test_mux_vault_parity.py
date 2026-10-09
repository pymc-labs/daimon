"""Vault, secret file and session-admin request equivalence at the real SDK boundary."""

import io
from collections import deque
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest
from daimon.core import mux_compat as compat
from daimon.core.mux_backend import platform_scope
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport

STAMP = "2026-01-01T00:00:00Z"
SCOPE = platform_scope("test vault request fidelity")
VAULT = {
    "id": "vault",
    "type": "vault",
    "display_name": "test",
    "created_at": STAMP,
    "metadata": {},
}


def credential(auth):
    return {
        "id": "credential",
        "type": "vault_credential",
        "vault_id": "vault",
        "created_at": STAMP,
        "updated_at": STAMP,
        "metadata": {},
        "display_name": "test",
        "auth": auth,
    }


def response_auth(auth):
    result = dict(auth)
    for name in ("token", "access_token", "secret_value"):
        result.pop(name, None)
    if result.get("refresh") is not None:
        result["refresh"] = dict(result["refresh"])
        result["refresh"].pop("refresh_token", None)
        result["refresh"]["token_endpoint_auth"] = dict(result["refresh"]["token_endpoint_auth"])
        result["refresh"]["token_endpoint_auth"].pop("client_secret", None)
    if result["type"] == "environment_variable":
        result.setdefault("injection_location", {"header": True, "body": False})
    return result


def transport(replies):
    return ScriptedTransport(
        deque(
            ScriptedReply(method, path, httpx.Response(200, json=body))
            for method, path, body in replies
        )
    )


def compare(old, new):
    old.assert_consumed()
    new.assert_consumed()
    assert [request.to_dict() for request in new.requests] == [
        request.to_dict() for request in old.requests
    ]


async def test_vault_list_create_archive_preserves_pagination_and_models():
    replies = [
        ("GET", "/v1/vaults", {"data": [VAULT], "next_page": "second"}),
        ("GET", "/v1/vaults", {"data": [VAULT], "next_page": None}),
        ("POST", "/v1/vaults", VAULT),
        ("POST", "/v1/vaults/vault/archive", VAULT),
    ]
    old, new = transport(replies), transport(replies)
    async with old.client() as before, new.client() as after:
        expected = [v.model_dump(mode="json") async for v in before.beta.vaults.list()]
        actual = [v.model_dump(mode="json") async for v in compat.list_vaults(after, scope=SCOPE)]
        assert actual == expected
        expected = await before.beta.vaults.create(display_name="test")
        actual = await compat.create_vault(after, "test", scope=SCOPE)
        assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
        await before.beta.vaults.archive("vault")
        await compat.archive_vault(after, "vault", scope=SCOPE)
    compare(old, new)


@pytest.mark.parametrize(
    "auth",
    [
        {
            "type": "static_bearer",
            "mcp_server_url": "https://example.test/mcp",
            "token": "dummy-bearer",
        },
        {
            "type": "mcp_oauth",
            "mcp_server_url": "https://example.test/mcp",
            "access_token": "dummy-access",
            "expires_at": STAMP,
            "refresh": {
                "client_id": "client",
                "refresh_token": "dummy-refresh",
                "token_endpoint": "https://example.test/token",
                "token_endpoint_auth": {
                    "type": "client_secret_basic",
                    "client_secret": "dummy-client",
                },
                "resource": "https://example.test/mcp",
                "scope": "read",
            },
        },
        {
            "type": "mcp_oauth",
            "mcp_server_url": "https://example.test/mcp",
            "access_token": "dummy-access",
            "expires_at": None,
            "refresh": None,
        },
        {
            "type": "environment_variable",
            "secret_name": "GH_TOKEN",
            "secret_value": "dummy-env",
            "networking": {"type": "limited", "allowed_hosts": ["github.com"]},
            "injection_location": {"header": True, "body": False},
        },
    ],
)
async def test_credential_create_list_delete_keeps_auth_and_metadata_payload(auth):
    record = credential(response_auth(auth))
    replies = [
        ("POST", "/v1/vaults/vault/credentials", record),
        ("GET", "/v1/vaults/vault/credentials", {"data": [record], "next_page": "second"}),
        ("GET", "/v1/vaults/vault/credentials", {"data": [record], "next_page": None}),
        (
            "DELETE",
            "/v1/vaults/vault/credentials/credential",
            {"id": "credential", "type": "vault_credential_deleted"},
        ),
    ]
    old, new = transport(replies), transport(replies)
    payload = {
        "auth": auth,
        "display_name": None,
        "metadata": {"daimon_chat_identity": "agent", "token_ref": "ordinary-metadata"},
    }
    async with old.client() as before, new.client() as after:
        expected = await before.beta.vaults.credentials.create(vault_id="vault", **payload)
        actual = await compat.create_credential(after, "vault", payload, scope=SCOPE)
        assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
        expected = [
            v.model_dump(mode="json")
            async for v in before.beta.vaults.credentials.list(vault_id="vault")
        ]
        actual = [
            v.model_dump(mode="json")
            async for v in compat.list_credentials(after, "vault", scope=SCOPE)
        ]
        assert actual == expected
        await before.beta.vaults.credentials.delete("credential", vault_id="vault")
        await compat.delete_credential(after, "vault", "credential", scope=SCOPE)
    compare(old, new)


@pytest.mark.parametrize(
    "auth",
    [
        {"type": "static_bearer", "token": "dummy-bearer"},
        {"type": "static_bearer", "token": None},
        {
            "type": "mcp_oauth",
            "access_token": "dummy-access",
            "expires_at": None,
            "refresh": {
                "refresh_token": "dummy-refresh",
                "scope": None,
                "token_endpoint_auth": {
                    "type": "client_secret_post",
                    "client_secret": "dummy-client",
                },
            },
        },
        {
            "type": "environment_variable",
            "secret_value": "dummy-env",
            "networking": None,
            "injection_location": None,
        },
        None,
    ],
)
async def test_credential_update_keeps_explicit_nulls(auth):
    record = credential({"type": "static_bearer", "mcp_server_url": "https://example.test/mcp"})
    replies = [("POST", "/v1/vaults/vault/credentials/credential", record)]
    old, new = transport(replies), transport(replies)
    payload = {"auth": auth, "display_name": None, "metadata": {"old": None}}
    async with old.client() as before, new.client() as after:
        expected = await before.beta.vaults.credentials.update(
            "credential", vault_id="vault", **payload
        )
        actual = await compat.update_credential(after, "vault", "credential", payload, scope=SCOPE)
        assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
    compare(old, new)


async def test_secret_file_upload_keeps_multipart_bytes_filename_type_and_beta():
    record = {
        "id": "file",
        "type": "file",
        "filename": ".env",
        "mime_type": "text/plain",
        "size_bytes": 23,
        "created_at": STAMP,
        "downloadable": True,
    }
    old, new = (
        transport([("POST", "/v1/files", record)]),
        transport([("POST", "/v1/files", record)]),
    )
    content = b"GH_TOKEN=dummy-token\n"
    async with old.client() as before, new.client() as after:
        expected = await before.beta.files.upload(file=(".env", io.BytesIO(content), "text/plain"))
        actual = await compat.upload_credential_file(
            after,
            content,
            filename=".env",
            media_type="text/plain",
            secret_values=["dummy-token"],
            scope=SCOPE,
        )
        assert actual.model_dump(mode="json") == expected.model_dump(mode="json")

    def normalize(request):
        headers = dict(request.protocol_headers)
        message = BytesParser(policy=default).parsebytes(
            ("Content-Type: " + headers["content-type"] + "\r\n\r\n").encode() + request.body
        )
        parts = [
            (
                part.get_param("name", header="content-disposition"),
                part.get_filename(),
                part.get_content_type(),
                part.get_payload(decode=True),
            )
            for part in message.iter_parts()
        ]
        headers["content-type"] = "multipart/form-data"
        return request.method, request.path, request.query, headers, parts

    assert normalize(new.requests[0]) == normalize(old.requests[0])
    old.assert_consumed()
    new.assert_consumed()


async def test_repo_token_update_and_archive_keep_requests_and_touch_no_shared_resources():
    replies = [
        (
            "POST",
            "/v1/sessions/session/resources/repo",
            {
                "id": "repo",
                "type": "github_repository",
                "mount_path": "/repo",
                "repository_url": "https://github.com/org/repo",
            },
        ),
        ("POST", "/v1/sessions/session/archive", {"id": "session", "type": "session"}),
    ]
    old, new = transport(replies), transport(replies)
    async with old.client() as before, new.client() as after:
        await before.beta.sessions.resources.update(
            "repo", session_id="session", authorization_token="dummy-token"
        )
        await compat.rotate_session_repo_token(after, "session", "repo", "dummy-token", scope=SCOPE)
        await before.beta.sessions.archive("session")
        await compat.archive_session(after, "session", scope=SCOPE)
    compare(old, new)
    assert len(new.requests) == 2


async def test_session_resource_list_add_delete_keep_existing_native_requests():
    from daimon.core.mux_backend import managed_agents, resource_ref
    from mux.contracts.resources import ResourceBinding

    row = {
        "id": "mount",
        "type": "file",
        "file_id": "file",
        "mount_path": "/data",
        "created_at": STAMP,
        "updated_at": STAMP,
    }
    replies = [
        ("GET", "/v1/sessions/session/resources", {"data": [row], "next_page": "second"}),
        ("GET", "/v1/sessions/session/resources", {"data": [row], "next_page": None}),
        ("POST", "/v1/sessions/session/resources", row),
        (
            "DELETE",
            "/v1/sessions/session/resources/mount",
            {"id": "mount", "type": "session_resource_deleted"},
        ),
    ]
    old, new = transport(replies), transport(replies)
    async with old.client() as before, new.client() as after:
        expected = [
            r.model_dump(mode="json") async for r in before.beta.sessions.resources.list("session")
        ]
        backend = managed_agents(after)
        session = resource_ref(backend, "session", "session", scope=SCOPE)
        actual = await backend.session_admin.list(SCOPE, session)
        assert [dict(r.native.value) for r in actual] == expected
        await before.beta.sessions.resources.add(
            "session", type="file", file_id="file", mount_path="/data"
        )
        binding = ResourceBinding(
            id="new",
            kind="artifact",
            target_path="/data",
            resource=resource_ref(backend, "file", "file", scope=SCOPE),
        )
        await backend.session_admin.add(SCOPE, session, binding, key="add")
        await before.beta.sessions.resources.delete("mount", session_id="session")
        await backend.session_admin.remove(SCOPE, session, "mount", key="remove")
    compare(old, new)
