"""MCP resource ports preserve SDK bytes, pages, partial replies and tenant scopes."""

import datetime as dt
import gzip
import hashlib
import io
import uuid
from collections import deque
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from anthropic import APIStatusError, AsyncAnthropic
from anthropic.types.beta import SkillListResponse
from daimon.adapters.mcp import bundles, hosted_artifacts, resource_ports
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import (
    hub,
    self_edit,
    sessions,
    skills,
    vault,
)
from daimon.adapters.mcp.tools.self_edit import (
    _set_repo_binding_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core import mux_compat, output_ports_compat
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_backend import managed_agents, resource_ref
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.stores.domain import AgentRepoBindingRow, Role
from daimon.testing.ma_models import ma_agent, ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from mux.contracts.ids import Scope
from mux.drivers.anthropic.credential_schemas import CredentialCreate
from mux.drivers.anthropic.resources._secrets import SecretResolver
from mux.drivers.anthropic.resources.vaults import Vaults
from mux.errors import ScopeViolation
from pydantic import BaseModel, PostgresDsn, SecretStr
from starlette.applications import Starlette
from starlette.routing import Route

TENANT = uuid.UUID(int=1)
ACCOUNT = uuid.UUID(int=2)
AGENT = ma_agent(id="agent", tenant_id=TENANT)
AUTH = AuthIdentity(
    tenant_id=TENANT,
    account_id=ACCOUNT,
    role=Role.USER,
    agent_id=derive_agent_uuid(tenant_id=TENANT, ma_agent_id="agent"),
)
SCOPE = resource_ports.mcp_scope(AUTH)
SESSION = ma_session(
    id="session",
    agent=AGENT,
    metadata={"daimon_tenant": str(TENANT), "daimon_account": str(ACCOUNT)},
)
BODY = SESSION.model_dump(mode="json")


def script(replies: list[tuple[str, str, int, dict[str, Any]]]) -> ScriptedTransport:
    return ScriptedTransport(
        deque(
            ScriptedReply(method, path, httpx.Response(status, json=body))
            for method, path, status, body in replies
        )
    )


def same(old: ScriptedTransport, new: ScriptedTransport) -> None:
    old.assert_consumed()
    new.assert_consumed()
    assert [r.to_dict() for r in old.requests] == [r.to_dict() for r in new.requests]
    # Parsed JSON equality misses serialization/key-order regressions.
    assert [r.body for r in old.requests] == [r.body for r in new.requests]


def same_model(old: BaseModel, new: BaseModel) -> None:
    assert old.model_dump(mode="json") == new.model_dump(mode="json")
    assert old.model_dump(mode="json", exclude_unset=True) == new.model_dump(
        mode="json", exclude_unset=True
    )
    assert old.model_fields_set == new.model_fields_set


class SessionFactory:
    async def __aenter__(self) -> "SessionFactory":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def __call__(self) -> "SessionFactory":
        return self

    def begin(self) -> "SessionFactory":
        return self


def runtime(client: AsyncAnthropic) -> McpRuntime:
    return cast(
        Any,
        SimpleNamespace(
            client=client,
            session_factory=SessionFactory(),
            settings=Settings(
                database=DatabaseSettings(
                    url=PostgresDsn("postgresql+asyncpg://offline.invalid/n12")
                ),
                anthropic=AnthropicSettings(api_key=SecretStr("n12-offline-test-key")),
            ),
            fernet=None,
        ),
    )


@pytest.fixture
def host_guards(monkeypatch: pytest.MonkeyPatch) -> list[Scope]:
    scopes: list[Scope] = []
    for module in (resource_ports, mux_compat, output_ports_compat):
        original = module.managed_agents

        def make_backend(
            client: AsyncAnthropic,
            *,
            scope: Scope,
            resources: frozenset[tuple[str, str]] = frozenset(),
            secrets: SecretResolver | None = None,
            _original: Any = original,
        ) -> Any:
            assert scope.tenant_id == str(TENANT)
            assert scope.account_id == str(ACCOUNT)
            assert not scope.is_platform
            assert not scope.is_legacy_host_authorized
            scopes.append(scope)
            return _original(client, scope=scope, resources=resources, secrets=secrets)

        monkeypatch.setattr(module, "managed_agents", make_backend)
    return scopes


VAULT_NAME = f"daimon-mcp:{ACCOUNT}:{AUTH.agent_id}"
VAULT_BODY: dict[str, Any] = {
    "id": "vault",
    "display_name": VAULT_NAME,
    "type": "vault",
    "created_at": "2026-01-01T00:00:00Z",
    "metadata": {},
}
CREDENTIAL_BODY: dict[str, Any] = {
    "id": "credential",
    "vault_id": "vault",
    "type": "vault_credential",
    "mcp_server_url": "https://legacy.example.test",
    "metadata": {"service": "github"},
    "auth": {
        "type": "static_bearer",
        "mcp_server_url": "https://github.com",
        "token": "must-not-leak",
    },
}


@pytest.mark.parametrize("stop", ["last", "next", "empty-cursor", "empty-next"])
async def test_named_vault_walk_keeps_lazy_sdk_pages_and_exact_name_filter(stop: str) -> None:
    other: dict[str, Any] = {**VAULT_BODY, "id": "other", "display_name": "other-account"}
    page: dict[str, Any] = {
        "data": [] if stop == "empty-cursor" else [other, VAULT_BODY],
        "next_page": "next"
        if stop in {"next", "empty-cursor"}
        else ""
        if stop == "empty-next"
        else None,
    }
    replies: list[tuple[str, str, int, dict[str, Any]]] = [("GET", "/v1/vaults", 200, page)]
    if stop == "next":
        replies.append(("GET", "/v1/vaults", 200, {"data": [VAULT_BODY], "next_page": None}))
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        expected = [v async for v in before.beta.vaults.list() if v.display_name == VAULT_NAME]
        actual = [v async for v in resource_ports.walk_named_vaults(after, VAULT_NAME, scope=SCOPE)]
        for a, b in zip(expected, actual, strict=True):
            same_model(a, b)
    same(old, new)


@pytest.mark.parametrize(
    "body",
    [
        CREDENTIAL_BODY,
        {
            "id": "credential",
            "vault_id": "vault",
            "type": "credential",
            "auth": None,
            "mcp_server_url": "https://legacy.example.test",
        },
    ],
)
@pytest.mark.parametrize("next_page", [None, "", "opaque"])
async def test_native_credential_walk_preserves_the_consumed_summary_without_unused_fields(
    body: dict[str, Any],
    next_page: str | None,
) -> None:
    replies: list[tuple[str, str, int, dict[str, Any]]] = [
        ("GET", "/v1/vaults/vault/credentials", 200, {"data": [body], "next_page": next_page})
    ]
    if next_page:
        replies.append(
            ("GET", "/v1/vaults/vault/credentials", 200, {"data": [], "next_page": "unused"})
        )
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        expected = [
            vault.VaultCredentialSummary.model_validate(c.model_dump(mode="json"))
            async for c in before.beta.vaults.credentials.list(vault_id="vault")
        ]
        rows = [c async for c in resource_ports.walk_vault_credentials(after, "vault", scope=SCOPE)]
        actual = [
            vault.VaultCredentialSummary.model_validate(c.model_dump(mode="json")) for c in rows
        ]
        assert actual == expected
        assert "must-not-leak" not in repr(rows)
    same(old, new)


async def test_public_vault_summary_keeps_oldest_matching_vault_and_legacy_url(
    host_guards: list[Scope],
) -> None:
    newer: dict[str, Any] = {**VAULT_BODY, "id": "newer", "created_at": "2026-09-01T00:00:00Z"}
    other: dict[str, Any] = {**VAULT_BODY, "id": "other", "display_name": "other-account"}
    replies: list[tuple[str, str, int, dict[str, Any]]] = [
        ("GET", "/v1/vaults", 200, {"data": [other, newer], "next_page": "older"}),
        ("GET", "/v1/vaults", 200, {"data": [VAULT_BODY], "next_page": None}),
        (
            "GET",
            "/v1/vaults/vault/credentials",
            200,
            {"data": [CREDENTIAL_BODY], "next_page": None},
        ),
    ]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        matching = [v async for v in before.beta.vaults.list() if v.display_name == VAULT_NAME]
        identity = min(matching, key=lambda v: v.created_at).id
        expected = [
            vault.VaultCredentialSummary.model_validate(c.model_dump(mode="json"))
            async for c in before.beta.vaults.credentials.list(vault_id=identity)
        ]
        actual = await vault._list_credentials_impl(after, AUTH)  # pyright: ignore[reportPrivateUsage]
        assert actual == expected
    assert host_guards
    same(old, new)


@pytest.mark.parametrize("body", [{"id": "credential"}, CREDENTIAL_BODY])
async def test_repo_credential_json_keeps_native_key_order_and_partial_id_reply(
    body: dict[str, Any],
    host_guards: list[Scope],
) -> None:
    token = "single'double\"世界\n"
    metadata = {"service": "github", "agent_id": str(AUTH.agent_id), "repo_url": "owner/repo"}
    replies: list[tuple[str, str, int, dict[str, Any]]] = [
        ("POST", "/v1/vaults/vault/credentials", 200, body)
    ]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        expected = await before.beta.vaults.credentials.create(
            vault_id="vault",
            auth={"type": "static_bearer", "mcp_server_url": "https://github.com", "token": token},
            metadata=metadata,
        )
        actual = await resource_ports.create_repo_credential(
            after,
            "vault",
            token=token,
            metadata=metadata,
            scope=SCOPE,
        )
        assert actual.id == expected.id
        assert token not in repr(actual)
    assert host_guards
    same(old, new)


@pytest.mark.parametrize("outcome", ["success", "db-failure", "revoke-failure"])
async def test_public_repo_binding_keeps_vault_write_commit_compensation_and_old_revoke(
    outcome: str,
    host_guards: list[Scope],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = dt.datetime(2026, 10, 9, 12, tzinfo=dt.UTC)
    row = AgentRepoBindingRow(
        tenant_id=TENANT,
        agent_id=cast(uuid.UUID, AUTH.agent_id),
        repo_url="owner/repo",
        default_branch="main",
        ma_secret_ref="old",
        created_at=clock,
        updated_at=clock,
    )
    monkeypatch.setattr(self_edit, "require_pin_write_access", AsyncMock())
    monkeypatch.setattr(self_edit, "dispatch_mint_token", AsyncMock(return_value="offline-pat"))
    monkeypatch.setattr(self_edit, "pat_can_access_repo", AsyncMock(return_value=True))
    monkeypatch.setattr(self_edit, "get_binding", AsyncMock(return_value=row))

    async def write(*args: object, **kwargs: object) -> AgentRepoBindingRow:
        assert kwargs["ma_secret_ref"] == "credential"
        assert [r.method for r in new.requests] == ["GET", "POST"]
        if outcome == "db-failure":
            raise RuntimeError("write failed")
        return row.model_copy(update={"ma_secret_ref": "credential"})

    monkeypatch.setattr(self_edit, "set_binding", write)
    revoked = "credential" if outcome == "db-failure" else "old"
    replies: list[tuple[str, str, int, dict[str, Any]]] = [
        ("GET", "/v1/vaults", 200, {"data": [VAULT_BODY], "next_page": None}),
        ("POST", "/v1/vaults/vault/credentials", 200, {"id": "credential"}),
        (
            "DELETE",
            f"/v1/vaults/vault/credentials/{revoked}",
            500 if outcome == "revoke-failure" else 200,
            {"type": "error", "error": {"type": "api_error", "message": "rejected"}}
            if outcome == "revoke-failure"
            else {},
        ),
    ]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        _ = [v async for v in before.beta.vaults.list() if v.display_name == VAULT_NAME]
        await before.beta.vaults.credentials.create(
            vault_id="vault",
            auth={
                "type": "static_bearer",
                "mcp_server_url": "https://github.com",
                "token": "offline-pat",
            },
            metadata={
                "service": "github",
                "agent_id": str(AUTH.agent_id),
                "repo_url": "owner/repo",
            },
        )
        if outcome == "revoke-failure":
            with pytest.raises(APIStatusError):
                await before.beta.vaults.credentials.delete(revoked, vault_id="vault")
        else:
            await before.beta.vaults.credentials.delete(revoked, vault_id="vault")
        host = runtime(after)
        if outcome == "db-failure":
            with pytest.raises(RuntimeError, match="write failed"):
                await _set_repo_binding_impl(
                    host,
                    AUTH,
                    repo_url="owner/repo",
                    default_branch="main",
                    service="github",
                    http_client=cast(Any, object()),
                )  # pyright: ignore[reportPrivateUsage]
        elif outcome == "revoke-failure":
            with pytest.raises(
                ToolError,
                match="the repo is now bound, but the credential from the previous binding could not be revoked and is still live",
            ):
                await _set_repo_binding_impl(
                    host,
                    AUTH,
                    repo_url="owner/repo",
                    default_branch="main",
                    service="github",
                    http_client=cast(Any, object()),
                )  # pyright: ignore[reportPrivateUsage]
        else:
            actual = await _set_repo_binding_impl(
                host,
                AUTH,
                repo_url="owner/repo",
                default_branch="main",
                service="github",
                http_client=cast(Any, object()),
            )  # pyright: ignore[reportPrivateUsage]
            assert actual.repo_url == "owner/repo"
    assert host_guards
    same(old, new)


async def test_public_clear_binding_keeps_vault_delete_before_database_clear(
    host_guards: list[Scope],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = dt.datetime(2026, 10, 9, 12, tzinfo=dt.UTC)
    row = AgentRepoBindingRow(
        tenant_id=TENANT,
        agent_id=cast(uuid.UUID, AUTH.agent_id),
        repo_url="owner/repo",
        default_branch="main",
        ma_secret_ref="old",
        created_at=clock,
        updated_at=clock,
    )
    monkeypatch.setattr(self_edit, "require_pin_write_access", AsyncMock())
    monkeypatch.setattr(self_edit, "get_binding", AsyncMock(return_value=row))

    async def clear(*args: object, **kwargs: object) -> None:
        assert [r.method for r in new.requests] == ["GET", "DELETE"]

    monkeypatch.setattr(self_edit, "clear_binding", clear)
    replies: list[tuple[str, str, int, dict[str, Any]]] = [
        ("GET", "/v1/vaults", 200, {"data": [VAULT_BODY], "next_page": None}),
        ("DELETE", "/v1/vaults/vault/credentials/old", 200, {}),
    ]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        _ = [v async for v in before.beta.vaults.list() if v.display_name == VAULT_NAME]
        await before.beta.vaults.credentials.delete("old", vault_id="vault")
        actual = await self_edit._clear_repo_binding_impl(runtime(after), AUTH)  # pyright: ignore[reportPrivateUsage]
        assert actual == {"cleared": True}
    assert host_guards
    same(old, new)


async def test_public_skill_version_count_keeps_partial_rows_and_sdk_stop_rules(
    host_guards: list[Scope],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = SkillListResponse(
        type="skill",
        id="skill",
        display_title="test",
        source="custom",
        latest_version=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    monkeypatch.setattr(skills, "find_skill_by_display_title", AsyncMock(return_value=skill))
    monkeypatch.setattr(skills, "_hidden", AsyncMock(return_value=False))
    replies: list[tuple[str, str, int, dict[str, Any]]] = [
        (
            "GET",
            "/v1/skills/skill/versions",
            200,
            {"data": [{"version": "1"}, {}], "next_page": "next"},
        ),
        ("GET", "/v1/skills/skill/versions", 200, {"data": [], "next_page": "unused"}),
    ]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        count = len([v async for v in before.beta.skills.versions.list("skill")])
        actual = await skills._get_impl(runtime(after), AUTH, "test")  # pyright: ignore[reportPrivateUsage]
        assert actual == skills.SkillDetail(
            name="test",
            id="skill",
            created_at=dt.datetime.fromisoformat(skill.created_at),
            version_count=count,
        )
    assert host_guards
    same(old, new)


async def test_public_hub_session_list_keeps_account_and_legacy_filters(
    host_guards: list[Scope],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = ma_session(id="legacy", agent=AGENT, metadata={})
    excluded = ma_session(id="excluded", agent=AGENT, metadata={"daimon_account": "other"})
    monkeypatch.setattr(hub, "admin_readable_legacy_sessions", AsyncMock(return_value={"legacy"}))

    async def visible(*args: Any, **kwargs: object) -> Any:
        return args[2]

    monkeypatch.setattr(hub, "sessions_outside_seals", visible)
    replies: list[tuple[str, str, int, dict[str, Any]]] = [
        (
            "GET",
            "/v1/sessions",
            200,
            {
                "data": [BODY, legacy.model_dump(mode="json"), excluded.model_dump(mode="json")],
                "next_page": None,
            },
        )
    ]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        rows = [s async for s in before.beta.sessions.list(agent_id="agent")]
        expected = [sessions.SessionInfo.from_ma(s) for s in rows if s.id in {"session", "legacy"}]
        actual = await hub._list_my_sessions_impl(runtime(after), AUTH, AGENT)  # pyright: ignore[reportPrivateUsage]
        assert actual == expected
    assert host_guards
    same(old, new)


def multipart_request(request: Any) -> dict[str, object]:
    content_type = dict(request.protocol_headers)["content-type"]
    boundary = content_type.split("boundary=", 1)[1].encode()
    # Compare raw multipart bytes; normalize only the SDK's random delimiter.
    return {
        "method": request.method,
        "path": request.path,
        "query": request.query,
        "body": request.body.replace(boundary, b"N12-PARITY-BOUNDARY"),
        "headers": tuple(
            (k, v.replace(boundary.decode(), "N12-PARITY-BOUNDARY"))
            for k, v in request.protocol_headers
        ),
    }


@pytest.mark.parametrize(
    "reply",
    [{"id": "file"}, {"id": "file", "filename": "bundle.tar.gz", "mime_type": "application/gzip"}],
)
async def test_public_bundle_upload_keeps_stream_multipart_digest_and_retention(
    reply: dict[str, Any],
    host_guards: list[Scope],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = gzip.compress(b"offline archive\x00bytes", mtime=0)
    retained: list[dict[str, object]] = []

    async def enqueue(*args: object, **kwargs: object) -> None:
        retained.append(kwargs)

    monkeypatch.setattr(bundles, "enqueue_pending_file_delete", enqueue)
    replies: list[tuple[str, str, int, dict[str, Any]]] = [("POST", "/v1/files", 200, reply)]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        await before.beta.files.upload(
            file=("bundle.tar.gz", io.BytesIO(archive), "application/gzip")
        )
        host = runtime(after)
        host.settings.mcp.jwt_secret = SecretStr("n12-offline-test-key")
        verifier = StaticTokenVerifier(
            tokens={
                "offline-token": {
                    "client_id": str(ACCOUNT),
                    "sub": str(ACCOUNT),
                    "tenant_id": str(TENANT),
                    "agent_id": str(AUTH.agent_id),
                }
            }
        )
        handler = bundles.build_bundles_route(
            anthropic=after,
            session_factory=host.session_factory,
            auth=verifier,
            mcp_settings=host.settings.mcp,
            rate_limiter=RateLimiter(max_requests=20),
        )
        app = Starlette(routes=[Route("/bundles", handler, methods=["PUT"])])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://offline"
        ) as http:
            result = await http.put(
                "/bundles", headers={"authorization": "Bearer offline-token"}, content=archive
            )
        assert result.status_code == 200
        payload = result.json()
        assert payload["sha256"] == hashlib.sha256(archive).hexdigest()
        assert retained[0]["file_id"] == "file"
    assert host_guards
    old.assert_consumed()
    new.assert_consumed()
    assert [multipart_request(r) for r in old.requests] == [
        multipart_request(r) for r in new.requests
    ]
    raw = new.requests[0].body
    assert b'filename="bundle.tar.gz"' in raw
    assert b"Content-Type: application/gzip\r\n" in raw
    assert archive in raw


async def test_public_chart_delivery_keeps_files_beta_queries_and_download_bytes(
    host_guards: list[Scope],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = dt.datetime(2026, 10, 9, 12, tzinfo=dt.UTC)
    content = b"\x89PNG\r\n\x1a\nsynthetic-offline-png"
    listing: dict[str, Any] = {
        "data": [
            {
                "id": "file",
                "filename": "chart.png",
                "size_bytes": len(content),
                "created_at": clock.isoformat(),
            }
        ],
        "has_more": False,
    }

    def transport() -> ScriptedTransport:
        return ScriptedTransport(
            deque(
                [
                    ScriptedReply("GET", "/v1/files", httpx.Response(200, json=listing)),
                    ScriptedReply(
                        "GET", "/v1/files/file/content", httpx.Response(200, content=content)
                    ),
                ]
            )
        )

    def no_embed(*args: object) -> None:
        return None

    monkeypatch.setattr(hosted_artifacts, "_bounded_image_block", no_embed)
    old, new = transport(), transport()
    async with old.client() as before, new.client() as after:
        _ = [
            f
            async for f in before.beta.files.list(
                scope_id="session", limit=200, betas=["managed-agents-2026-04-01"]
            )
        ]
        assert (
            await (
                await before.beta.files.download("file", betas=["managed-agents-2026-04-01"])
            ).read()
            == content
        )
        failure = AsyncMock()
        actual = await hosted_artifacts._deliver_hosted_charts_impl(  # pyright: ignore[reportPrivateUsage]
            after,
            settings=None,
            tenant_id=str(TENANT),
            account_id=str(ACCOUNT),
            session_id="session",
            turn_started_at=clock,
            message="answer",
            record_failure=failure,
            upload_deadline=float("inf"),
        )
        assert actual == hosted_artifacts.HostedChartDelivery(message="answer")
        failure.assert_not_called()
    assert host_guards
    same(old, new)


async def test_chart_scan_cap_stops_the_sdk_paginator_before_another_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = dt.datetime(2026, 10, 9, 12, tzinfo=dt.UTC)
    listing: dict[str, Any] = {
        "data": [
            {
                "id": "file",
                "filename": "chart.png",
                "size_bytes": 4,
                "created_at": clock.isoformat(),
            }
        ],
        "has_more": True,
        "last_id": "file",
    }
    monkeypatch.setattr(hosted_artifacts, "_MAX_SCANNED", 1)
    replies: list[tuple[str, str, int, dict[str, Any]]] = [("GET", "/v1/files", 200, listing)]
    old, new = script(replies), script(replies)
    async with old.client() as before, new.client() as after:
        async for _ in before.beta.files.list(
            scope_id="session", limit=1, betas=["managed-agents-2026-04-01"]
        ):
            break
        actual = await hosted_artifacts._discover_chart_outputs(  # pyright: ignore[reportPrivateUsage]
            after,
            session_id="session",
            turn_started_at=clock,
            scope=SCOPE,
        )
        assert len(actual) == 1
    same(old, new)


@pytest.mark.parametrize("operation", ["name", "credentials", "create"])
async def test_native_vault_operations_refuse_an_ungranted_scope_before_io(operation: str) -> None:
    transport = script([])
    async with transport.client() as client:
        backend = managed_agents(client, scope=SCOPE)
        port = backend.extension(Vaults, namespace="anthropic.vaults", version=1)
        with pytest.raises(ScopeViolation):
            if operation == "name":
                _ = [v async for v in port.walk_named(SCOPE, VAULT_NAME)]
            elif operation == "credentials":
                _ = [
                    v
                    async for v in port.credential_walk_native(
                        SCOPE, resource_ref(backend, "vault", "vault", scope=SCOPE)
                    )
                ]
            else:
                await port.create_credential_id(
                    SCOPE,
                    resource_ref(backend, "vault", "vault", scope=SCOPE),
                    CredentialCreate.model_validate(
                        {
                            "auth": {
                                "type": "static_bearer",
                                "mcp_server_url": "https://github.com",
                                "token_ref": "ref",
                            }
                        }
                    ),
                    key="key",
                )
    assert transport.requests == []


@pytest.mark.parametrize("operation", ["credentials", "create", "chart"])
@pytest.mark.parametrize("violation", ["tenant", "account", "grant"])
async def test_resource_ref_denials_are_checked_before_native_requests(
    operation: str, violation: str
) -> None:
    from mux.drivers.anthropic.resources.artifacts import Artifacts

    kind = "session" if operation == "chart" else "vault"
    transport = script([])
    async with transport.client() as client:
        backend = managed_agents(
            client,
            scope=SCOPE,
            resources=frozenset() if violation == "grant" else frozenset({(kind, "target")}),
        )
        ref = resource_ref(backend, kind, "target", scope=SCOPE)
        if violation != "grant":
            field = "tenant_id" if violation == "tenant" else "account_id"
            ref = ref.model_copy(update={field: "different"})
        with pytest.raises(ScopeViolation):
            if operation == "chart":
                files = backend.extension(Artifacts, namespace="anthropic.artifacts", version=1)
                _ = [f async for f in files.walk_session_native(SCOPE, ref, limit=200)]
            else:
                vaults = backend.extension(Vaults, namespace="anthropic.vaults", version=1)
                if operation == "credentials":
                    _ = [c async for c in vaults.credential_walk_native(SCOPE, ref)]
                else:
                    await vaults.create_credential_id(
                        SCOPE,
                        ref,
                        CredentialCreate.model_validate(
                            {
                                "auth": {
                                    "type": "static_bearer",
                                    "mcp_server_url": "https://github.com",
                                    "token_ref": "not-resolved",
                                }
                            }
                        ),
                        key="key",
                    )
    assert transport.requests == []


async def test_bundle_upload_rejects_scope_mismatch_before_reading_its_spool() -> None:
    from mux.drivers.anthropic.resources.artifacts import Artifacts

    class Unreadable(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise AssertionError("unauthorized spool was read")

    transport = script([])
    async with transport.client() as client:
        backend = managed_agents(client, scope=SCOPE)
        files = backend.extension(Artifacts, namespace="anthropic.artifacts", version=1)
        with pytest.raises(ScopeViolation):
            await files.upload_native(
                SCOPE.model_copy(update={"tenant_id": "other"}),
                Unreadable(b"archive"),
                filename="bundle.tar.gz",
                media_type="application/gzip",
                key="key",
            )
    assert transport.requests == []
