"""Compare migrated host decisions with their original requests at the SDK boundary."""

import datetime as dt
import io
import uuid
from collections import deque
from email.parser import BytesParser
from email.policy import default
from typing import Any, cast

import httpx
import pytest
from daimon.core.agent_mcp_credentials import (
    METADATA_VERSION_KEY,
    ResolvedMcpCredential,
    mirror_credentials_into_vault,
)
from daimon.core.github_app_session import AppSessionAccess, AppToken, add_app_credentials
from daimon.core.mcp_auth import mint_jwt
from daimon.core.mcp_oauth.models import ClientRegistration, TokenResponse
from daimon.core.mcp_oauth.vault import put_mcp_oauth_credential
from daimon.core.mcp_vault import (
    GITHUB_COPILOT_MCP_URL,
    _ensure_agent_mcp_vault_locked,
    add_github_copilot_credential,
)
from daimon.core.mux_backend import resource_scope
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport

NOW = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
STAMP = NOW.isoformat()
ACCOUNT = uuid.UUID(int=1)
AGENT = uuid.UUID(int=2)
SCOPE = resource_scope(tenant_id="tenant", account_id=str(ACCOUNT))
SERVER = "https://example.test/mcp"
PUBLIC = "https://daimon.test/mcp"
SECRET = b"dummy-jwt-signing-key-for-offline-tests"


def credential(native_id, auth, metadata=None):
    return {
        "id": native_id,
        "type": "vault_credential",
        "vault_id": "vault",
        "created_at": STAMP,
        "updated_at": STAMP,
        "metadata": metadata or {},
        "display_name": None,
        "auth": auth,
    }


def static(native_id, url, metadata=None):
    return credential(native_id, {"type": "static_bearer", "mcp_server_url": url}, metadata)


def environment(native_id, name):
    return credential(
        native_id,
        {
            "type": "environment_variable",
            "secret_name": name,
            "networking": {"type": "limited", "allowed_hosts": ["github.com"]},
            "injection_location": {"header": True, "body": False},
        },
    )


def page(rows):
    return {"data": rows, "next_page": None}


def script(replies):
    return ScriptedTransport(
        deque(
            ScriptedReply(method, path, httpx.Response(status, json=body))
            for method, path, status, body in replies
        )
    )


def equal_requests(old, new):
    old.assert_consumed()
    new.assert_consumed()
    assert [request.to_dict() for request in new.requests] == [
        request.to_dict() for request in old.requests
    ]


@pytest.mark.parametrize("case", ["cold", "upgrade", "heal", "unchanged"])
async def test_vault_bootstrap_keeps_discovery_then_tenant_writes(case, monkeypatch):
    import daimon.core.mcp_vault as host

    name = f"daimon-mcp:{ACCOUNT}:{AGENT}"
    vault = {
        "id": "vault",
        "type": "vault",
        "display_name": name,
        "created_at": STAMP,
        "metadata": {},
    }
    foreign = {**vault, "id": "foreign", "display_name": "another-account"}
    metadata = {"daimon_chat_identity": str(AGENT)} if case == "unchanged" else {"kept": "yes"}
    current = static("existing", PUBLIC if case != "heal" else SERVER, metadata)
    replies = [("GET", "/v1/vaults", 200, page([foreign] if case == "cold" else [foreign, vault]))]
    if case == "cold":
        replies.append(("POST", "/v1/vaults", 200, vault))
    else:
        replies.append(("GET", "/v1/vaults/vault/credentials", 200, page([current])))
    if case in ("cold", "heal"):
        replies.append(("POST", "/v1/vaults/vault/credentials", 200, static("created", PUBLIC)))
    elif case == "upgrade":
        replies.append(("POST", "/v1/vaults/vault/credentials/existing", 200, current))
    old, new = script(replies), script(replies)
    seen = []
    for operation in (
        "list_vaults",
        "list_credentials",
        "create_vault",
        "store_credential",
        "update_credential",
    ):
        original = getattr(host, operation)

        def spy(*args, _operation=operation, _original=original, **kwargs):
            seen.append((_operation, kwargs["scope"]))
            return _original(*args, **kwargs)

        monkeypatch.setattr(host, operation, spy)
    token = mint_jwt(account_id=ACCOUNT, chat_agent_id=AGENT, secret=SECRET, now=NOW)
    async with old.client() as before, new.client() as after:
        # Original host leaf calls and literal payloads, independently pinned.
        _ = [row async for row in before.beta.vaults.list()]
        if case == "cold":
            await before.beta.vaults.create(display_name=name)
        else:
            _ = [row async for row in before.beta.vaults.credentials.list(vault_id="vault")]
        if case in ("cold", "heal"):
            await before.beta.vaults.credentials.create(
                vault_id="vault",
                metadata={"daimon_chat_identity": str(AGENT)},
                auth={"type": "static_bearer", "mcp_server_url": PUBLIC, "token": token},
            )
        elif case == "upgrade":
            await before.beta.vaults.credentials.update(
                "existing",
                vault_id="vault",
                auth={"type": "static_bearer", "token": token},
                metadata={"kept": "yes", "daimon_chat_identity": str(AGENT)},
            )
        assert (
            await _ensure_agent_mcp_vault_locked(
                after,
                account_id=ACCOUNT,
                agent_id=AGENT,
                jwt_secret=SECRET,
                public_url=PUBLIC,
                now=NOW,
                scope=SCOPE,
            )
            == "vault"
        )
    equal_requests(old, new)
    assert seen[0][0] == "list_vaults"
    assert seen[0][1].is_legacy_host_authorized  # Name discovery precedes knowing the ID.
    assert all(scope == SCOPE for _, scope in seen[1:])


@pytest.mark.parametrize("in_place", [False, True])
async def test_copilot_replacement_keeps_duplicate_order_and_token_only_update(in_place):
    rows = [static("first", GITHUB_COPILOT_MCP_URL), static("duplicate", GITHUB_COPILOT_MCP_URL)]
    replies = [("GET", "/v1/vaults/vault/credentials", 200, page(rows))]
    if in_place:
        replies.append(("POST", "/v1/vaults/vault/credentials/first", 200, rows[0]))
        replies.append(("DELETE", "/v1/vaults/vault/credentials/duplicate", 200, {}))
    else:
        replies.extend(
            [
                ("DELETE", "/v1/vaults/vault/credentials/first", 200, {}),
                ("DELETE", "/v1/vaults/vault/credentials/duplicate", 200, {}),
                ("POST", "/v1/vaults/vault/credentials", 200, rows[0]),
            ]
        )
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        _ = [row async for row in before.beta.vaults.credentials.list(vault_id="vault")]
        if in_place:
            await before.beta.vaults.credentials.update(
                "first", vault_id="vault", auth={"type": "static_bearer", "token": "dummy-pat"}
            )
            await before.beta.vaults.credentials.delete("duplicate", vault_id="vault")
        else:
            for native_id in ("first", "duplicate"):
                await before.beta.vaults.credentials.delete(native_id, vault_id="vault")
            await before.beta.vaults.credentials.create(
                vault_id="vault",
                auth={
                    "type": "static_bearer",
                    "mcp_server_url": GITHUB_COPILOT_MCP_URL,
                    "token": "dummy-pat",
                },
            )
        await add_github_copilot_credential(
            after, vault_id="vault", token="dummy-pat", in_place=in_place, scope=SCOPE
        )
    equal_requests(old, new)


async def test_mirror_404_rechecks_once_and_preserves_a_persons_oauth_grant():
    first = static("old", SERVER, {METADATA_VERSION_KEY: "v1"})
    grant = credential("grant", {"type": "mcp_oauth", "mcp_server_url": SERVER})
    replies = [
        ("GET", "/v1/vaults/vault/credentials", 200, page([first])),
        (
            "POST",
            "/v1/vaults/vault/credentials/old",
            404,
            {"type": "error", "error": {"type": "not_found_error", "message": "gone"}},
        ),
        ("GET", "/v1/vaults/vault/credentials", 200, page([grant])),
    ]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        _ = [row async for row in before.beta.vaults.credentials.list(vault_id="vault")]
        from anthropic import NotFoundError

        with pytest.raises(NotFoundError):
            await before.beta.vaults.credentials.update(
                "old",
                vault_id="vault",
                auth={"type": "static_bearer", "token": "dummy-new"},
                metadata={METADATA_VERSION_KEY: "v2"},
            )
        _ = [row async for row in before.beta.vaults.credentials.list(vault_id="vault")]
        await mirror_credentials_into_vault(
            after,
            vault_id="vault",
            credentials=(ResolvedMcpCredential(SERVER, "dummy-new", "v2"),),
            scope=SCOPE,
        )
    equal_requests(old, new)


@pytest.mark.parametrize("resource,token_scope", [(None, None), (SERVER, "read write")])
async def test_oauth_conflict_retries_the_same_requests_and_access_checks(resource, token_scope):
    record = credential("grant", {"type": "mcp_oauth", "mcp_server_url": SERVER})
    replies = [
        ("GET", "/v1/vaults/vault/credentials", 200, page([])),
        (
            "POST",
            "/v1/vaults/vault/credentials",
            409,
            {"type": "error", "error": {"type": "conflict_error", "message": "race"}},
        ),
        ("GET", "/v1/vaults/vault/credentials", 200, page([])),
        ("POST", "/v1/vaults/vault/credentials", 200, record),
    ]
    old, new = script(replies), script(replies)
    checks = []

    async def allowed():
        checks.append(True)

    auth = {
        "type": "mcp_oauth",
        "mcp_server_url": SERVER,
        "access_token": "dummy-access",
        "expires_at": NOW + dt.timedelta(seconds=600),
        "refresh": {
            "client_id": "client",
            "refresh_token": "dummy-refresh",
            "token_endpoint": "https://example.test/token",
            "token_endpoint_auth": {"type": "none"},
        },
    }
    if token_scope:
        auth["refresh"]["scope"] = token_scope
    if resource:
        auth["refresh"]["resource"] = resource
    async with old.client() as before, new.client() as after:
        from anthropic import ConflictError

        for attempt in range(2):
            _ = [row async for row in before.beta.vaults.credentials.list(vault_id="vault")]
            try:
                await before.beta.vaults.credentials.create(
                    vault_id="vault", auth=auth, display_name=f"oauth:{SERVER}"
                )
            except ConflictError:
                assert attempt == 0
        assert (
            await put_mcp_oauth_credential(
                after,
                vault_id="vault",
                mcp_server_url=SERVER,
                tokens=TokenResponse(
                    access_token="dummy-access",
                    refresh_token="dummy-refresh",
                    expires_in=600,
                    scope=token_scope,
                ),
                client=ClientRegistration(client_id="client"),
                token_endpoint="https://example.test/token",
                resource=resource,
                now=NOW,
                before_write=allowed,
                scope=SCOPE,
            )
            == "grant"
        )
    equal_requests(old, new)
    assert [request.body for request in new.requests] == [request.body for request in old.requests]
    assert checks == [True] * 4


async def test_app_credentials_keep_environment_nulls_cleanup_and_delivery_callbacks():
    rows = [environment("old", "GH_TOKEN_OLD")]
    created = environment("new", "GH_TOKEN_ORG_READ")
    replies = [
        ("GET", "/v1/vaults/vault/credentials", 200, page(rows)),
        ("POST", "/v1/vaults/vault/credentials", 200, created),
        ("DELETE", "/v1/vaults/vault/credentials/old", 200, {}),
        ("GET", "/v1/vaults/vault/credentials", 200, page([])),
    ]
    old, new = script(replies), script(replies)
    access = AppSessionAccess(
        (), (AppToken(uuid.UUID(int=3), "dummy-app", 1, (2,), "read", "GH_TOKEN_ORG_READ"),), None
    )
    mutations, delivered = [], []
    async with old.client() as before, new.client() as after:
        _ = [row async for row in before.beta.vaults.credentials.list(vault_id="vault")]
        await before.beta.vaults.credentials.create(
            vault_id="vault",
            auth={
                "type": "environment_variable",
                "secret_name": "GH_TOKEN_ORG_READ",
                "secret_value": "dummy-app",
                "networking": {
                    "type": "limited",
                    "allowed_hosts": ["api.github.com", "github.com", "uploads.github.com"],
                },
                "injection_location": {"header": True, "body": False},
            },
        )
        await before.beta.vaults.credentials.delete("old", vault_id="vault")
        _ = [row async for row in before.beta.vaults.credentials.list(vault_id="vault")]
        await add_app_credentials(
            after,
            vault_id="vault",
            access=access,
            scope=SCOPE,
            on_mutation=lambda: mutations.append(True),
            on_delivered=delivered.append,
        )
    equal_requests(old, new)
    assert [request.body for request in new.requests] == [request.body for request in old.requests]
    assert mutations == [True, True]
    assert delivered == ["dummy-app"]


async def test_env_upload_call_site_keeps_multipart_filename_type_bytes_and_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core import credential_env as host
    from daimon.core.stores.domain import AgentFileRow

    tenant = uuid.UUID(int=4)
    row = AgentFileRow(
        tenant_id=tenant,
        agent_id=AGENT,
        key="KEY",
        content="dummy-env-secret",
        created_at=NOW,
        updated_at=NOW,
    )
    content = b"KEY=dummy-env-secret\n"
    record = {
        "id": "file",
        "type": "file",
        "filename": ".env",
        "mime_type": "text/plain",
        "size_bytes": len(content),
        "created_at": STAMP,
        "downloadable": True,
    }
    old, new = (
        script([("POST", "/v1/files", 200, record)]),
        script([("POST", "/v1/files", 200, record)]),
    )
    queued: list[str] = []

    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        def begin(self) -> "Session":
            return self

    async def enqueue(session: object, *, file_id: str, delete_after: dt.datetime) -> None:
        assert delete_after.tzinfo is not None
        queued.append(file_id)

    monkeypatch.setattr(host, "enqueue_pending_file_delete", enqueue)
    async with old.client() as before, new.client() as after:
        await before.beta.files.upload(file=(".env", io.BytesIO(content), "text/plain"))
        assert (
            await host.upload_env_file(
                after,
                cast(Any, Session),
                rows=[row],
                scope=resource_scope(tenant_id=str(tenant)),
            )
            == "file"
        )
    for transport in (old, new):
        transport.assert_consumed()
        assert len(transport.requests) == 1
        request = transport.requests[0]
        message = BytesParser(policy=default).parsebytes(
            (
                "Content-Type: " + dict(request.protocol_headers)["content-type"] + "\r\n\r\n"
            ).encode()
            + request.body
        )
        parts = list(message.iter_parts())
        assert len(parts) == 1
        assert parts[0].get_param("name", header="content-disposition") == "file"
        assert parts[0].get_filename() == ".env"
        assert parts[0].get_content_type() == "text/plain"
        assert parts[0].get_payload(decode=True) == content
    assert old.requests[0].method == new.requests[0].method == "POST"
    assert old.requests[0].path == new.requests[0].path == "/v1/files"
    assert old.requests[0].query == new.requests[0].query
    assert (
        dict(old.requests[0].protocol_headers)["anthropic-beta"]
        == dict(new.requests[0].protocol_headers)["anthropic-beta"]
    )
    assert queued == ["file"]
