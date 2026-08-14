"""Tests for the agent-scoped external MCP credential store."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from cryptography.fernet import Fernet
from daimon.core.agent_mcp_credentials import (
    METADATA_VERSION_KEY,
    ResolvedMcpCredential,
    mirror_credentials_into_vault,
    resolve_agent_mcp_credentials,
    save_agent_mcp_credential,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.stores import agent_mcp_credentials as cred_store
from daimon.core.stores.domain import AgentMcpCredentialRow
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_upsert_credential_returns_pydantic_row(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()

    row = await cred_store.upsert_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url="https://example.com/mcp",
        encrypted_token=b"ciphertext",
    )

    assert isinstance(row, AgentMcpCredentialRow), "store should return Pydantic, not ORM"
    assert row.mcp_server_url == "https://example.com/mcp"
    assert row.encrypted_token == b"ciphertext"


async def test_upsert_credential_replaces_token_for_the_same_url(
    db_session: AsyncSession,
) -> None:
    """Re-entering a token for a server the agent already has must replace it,
    not accumulate rows MA would then race over."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()

    await cred_store.upsert_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url="https://example.com/mcp",
        encrypted_token=b"old",
    )
    await cred_store.upsert_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url="https://example.com/mcp",
        encrypted_token=b"new",
    )

    rows = await cred_store.list_credentials(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert len(rows) == 1, "one row per (tenant, agent, url)"
    assert rows[0].encrypted_token == b"new", "the newer token wins"


async def test_list_credentials_scopes_to_the_requested_agent(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    mine = uuid.uuid4()
    theirs = uuid.uuid4()

    await cred_store.upsert_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=mine,
        mcp_server_url="https://mine.example.com/mcp",
        encrypted_token=b"a",
    )
    await cred_store.upsert_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=theirs,
        mcp_server_url="https://theirs.example.com/mcp",
        encrypted_token=b"b",
    )

    rows = await cred_store.list_credentials(db_session, tenant_id=tenant.id, agent_id=mine)
    assert [r.mcp_server_url for r in rows] == ["https://mine.example.com/mcp"], (
        "another agent's credential must never leak into this agent's session"
    )


async def test_delete_credential_reports_whether_a_row_was_removed(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await cred_store.upsert_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url="https://example.com/mcp",
        encrypted_token=b"x",
    )

    assert (
        await cred_store.delete_credential(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            mcp_server_url="https://example.com/mcp",
        )
        is True
    ), "deleting an existing credential reports True"
    assert (
        await cred_store.delete_credential(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            mcp_server_url="https://example.com/mcp",
        )
        is False
    ), "deleting an absent credential is idempotent and reports False"


async def test_save_then_resolve_round_trips_the_plaintext_token(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The token is what MA needs; encryption must be transparent to callers."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    fernet = build_multifernet((Fernet.generate_key().decode(),))

    await save_agent_mcp_credential(
        sessionmaker=db_session_factory,
        fernet=fernet,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url="https://internal.example.com/mcp",
        plaintext_token="tok_secret",
    )

    resolved = await resolve_agent_mcp_credentials(
        sessionmaker=db_session_factory,
        fernet=fernet,
        tenant_id=tenant.id,
        agent_id=agent_id,
    )

    assert len(resolved) == 1
    assert resolved[0].mcp_server_url == "https://internal.example.com/mcp"
    assert resolved[0].token == "tok_secret", "resolve must return the decrypted token"


async def test_resolve_returns_empty_for_an_agent_with_no_credentials(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Empty means 'no external MCP servers', never 'something broke'."""
    tenant = await make_tenant(db_session)
    fernet = build_multifernet((Fernet.generate_key().decode(),))

    resolved = await resolve_agent_mcp_credentials(
        sessionmaker=db_session_factory,
        fernet=fernet,
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
    )

    assert resolved == (), "no rows resolves to an empty tuple"


async def test_stored_token_is_not_recoverable_without_the_key(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The DB row must hold ciphertext — this table is the only place the token
    is readable, so a DB dump alone must not yield live MCP tokens."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    fernet = build_multifernet((Fernet.generate_key().decode(),))

    await save_agent_mcp_credential(
        sessionmaker=db_session_factory,
        fernet=fernet,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url="https://internal.example.com/mcp",
        plaintext_token="tok_secret",
    )

    rows = await cred_store.list_credentials(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert b"tok_secret" not in rows[0].encrypted_token, "token must be stored encrypted"


# --- mirror: create / rotate-in-place / skip ---------------------------------


def _vault_handler(
    *,
    creds: list[dict[str, Any]],
    created: list[dict[str, Any]],
    updated: list[tuple[str, dict[str, Any]]],
    deleted: list[str],
) -> Callable[[httpx.Request], httpx.Response]:
    """Serves one vault's credential endpoints, recording every mutation."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/credentials"):
            return httpx.Response(200, json={"data": creds, "has_more": False})
        if request.method == "POST" and request.url.path.endswith("/credentials"):
            body = json.loads(request.content)
            created.append(body)
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_created",
                    "type": "credential",
                    "vault_id": "vlt_1",
                    "auth": {
                        "type": "static_bearer",
                        "mcp_server_url": body["auth"]["mcp_server_url"],
                    },
                    "metadata": body.get("metadata"),
                },
            )
        if request.method == "POST" and "/credentials/" in request.url.path:
            # The SDK issues an update as a POST to /credentials/{id}.
            body = json.loads(request.content)
            updated.append((request.url.path.rsplit("/", 1)[-1], body))
            return httpx.Response(
                200,
                json={
                    "id": request.url.path.rsplit("/", 1)[-1],
                    "type": "credential",
                    "vault_id": "vlt_1",
                    "auth": {"type": "static_bearer", "mcp_server_url": _URL},
                    "metadata": body.get("metadata"),
                },
            )
        if request.method == "DELETE":
            deleted.append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"id": "gone", "type": "credential"})
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    return _handler


_URL = "https://internal.example.com/mcp"


def _stored(*, token: str, version: str) -> tuple[ResolvedMcpCredential, ...]:
    return (ResolvedMcpCredential(mcp_server_url=_URL, token=token, version=version),)


async def test_mirror_creates_credential_when_the_vault_has_none_at_that_url() -> None:
    created: list[dict[str, Any]] = []
    updated: list[tuple[str, dict[str, Any]]] = []
    deleted: list[str] = []
    client = build_fake_anthropic(
        _vault_handler(creds=[], created=created, updated=updated, deleted=deleted)
    )

    await mirror_credentials_into_vault(
        client, vault_id="vlt_1", credentials=_stored(token="tok_1", version="v1")
    )

    assert len(created) == 1, "a caller with no credential gets one created"
    assert created[0]["auth"]["token"] == "tok_1"
    assert created[0]["metadata"] == {METADATA_VERSION_KEY: "v1"}, "creation stamps the version"
    assert updated == [] and deleted == []


async def test_mirror_updates_in_place_when_the_stored_token_was_rotated() -> None:
    """Rotation must reach a caller who is not the rotator — and must do it with
    an update, never a delete, since a concurrent turn may be using the vault."""
    created: list[dict[str, Any]] = []
    updated: list[tuple[str, dict[str, Any]]] = []
    deleted: list[str] = []
    client = build_fake_anthropic(
        _vault_handler(
            creds=[
                {
                    "id": "vcrd_old",
                    "type": "credential",
                    "vault_id": "vlt_1",
                    "auth": {"type": "static_bearer", "mcp_server_url": _URL},
                    "metadata": {METADATA_VERSION_KEY: "v1"},
                }
            ],
            created=created,
            updated=updated,
            deleted=deleted,
        )
    )

    await mirror_credentials_into_vault(
        client, vault_id="vlt_1", credentials=_stored(token="tok_2", version="v2")
    )

    assert len(updated) == 1, "a stale stamp must trigger exactly one update"
    credential_id, body = updated[0]
    assert credential_id == "vcrd_old", "the existing credential is updated, not a new one"
    assert body["auth"] == {"type": "static_bearer", "token": "tok_2"}, (
        "update body carries the token only — MA 400s on mcp_server_url"
    )
    assert body["metadata"] == {METADATA_VERSION_KEY: "v2"}, "the new version is stamped"
    assert deleted == [], "never delete: a concurrent turn may hold this credential"
    assert created == [], "no duplicate POST — MA 409s on the same URL anyway"


async def test_mirror_refreshes_an_unstamped_credential_once() -> None:
    """Credentials written before the stamp existed (or by the attach path) are
    of unknown vintage, so they get refreshed once and stamped — which self-heals
    a vault already holding a stale token."""
    created: list[dict[str, Any]] = []
    updated: list[tuple[str, dict[str, Any]]] = []
    deleted: list[str] = []
    client = build_fake_anthropic(
        _vault_handler(
            creds=[
                {
                    "id": "vcrd_unstamped",
                    "type": "credential",
                    "vault_id": "vlt_1",
                    "auth": {"type": "static_bearer", "mcp_server_url": _URL},
                    "metadata": None,
                }
            ],
            created=created,
            updated=updated,
            deleted=deleted,
        )
    )

    await mirror_credentials_into_vault(
        client, vault_id="vlt_1", credentials=_stored(token="tok_now", version="v9")
    )

    assert len(updated) == 1, "an unstamped credential is refreshed"
    assert updated[0][1]["metadata"] == {METADATA_VERSION_KEY: "v9"}


async def test_mirror_makes_no_call_when_the_stamp_is_already_current() -> None:
    """The steady state: every turn after the first must not touch MA."""
    created: list[dict[str, Any]] = []
    updated: list[tuple[str, dict[str, Any]]] = []
    deleted: list[str] = []
    client = build_fake_anthropic(
        _vault_handler(
            creds=[
                {
                    "id": "vcrd_current",
                    "type": "credential",
                    "vault_id": "vlt_1",
                    "auth": {"type": "static_bearer", "mcp_server_url": _URL},
                    "metadata": {METADATA_VERSION_KEY: "v5"},
                }
            ],
            created=created,
            updated=updated,
            deleted=deleted,
        )
    )

    await mirror_credentials_into_vault(
        client, vault_id="vlt_1", credentials=_stored(token="tok_5", version="v5")
    )

    assert created == [] and updated == [] and deleted == [], (
        "a matching stamp means the vault is already current"
    )
