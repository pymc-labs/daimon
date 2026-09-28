"""Tests for the agent-scoped external MCP credential store."""

from __future__ import annotations

import datetime as dt
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
    sync_agent_mcp_credentials,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.stores import agent_mcp_credentials as cred_store
from daimon.core.stores.domain import AgentMcpCredentialRow
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
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
    conflict: bool = False,
) -> Callable[[httpx.Request], httpx.Response]:
    """Serves one vault's credential endpoints, recording every mutation.

    `conflict=True` answers every create with MA's 409 for a URL already held.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/credentials"):
            return httpx.Response(200, json={"data": creds, "has_more": False})
        if request.method == "POST" and request.url.path.endswith("/credentials"):
            body = json.loads(request.content)
            created.append(body)
            if conflict:
                return httpx.Response(
                    409,
                    json={
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": "A credential already exists for this MCP server URL.",
                        },
                    },
                )
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


async def test_mirror_leaves_a_url_held_by_the_callers_own_oauth_grant_alone() -> None:
    """Staging, 2026-09-15: after one person connected Notion by OAuth, every turn
    failed with 409 because the mirror only saw static_bearer credentials and
    tried to create the agent's shared token next to their grant."""
    created: list[dict[str, Any]] = []
    updated: list[tuple[str, dict[str, Any]]] = []
    deleted: list[str] = []
    client = build_fake_anthropic(
        _vault_handler(
            creds=[
                {
                    "id": "vcrd_grant",
                    "type": "credential",
                    "vault_id": "vlt_1",
                    "auth": {"type": "mcp_oauth", "mcp_server_url": _URL + "/"},
                    "metadata": None,
                }
            ],
            created=created,
            updated=updated,
            deleted=deleted,
        )
    )

    await mirror_credentials_into_vault(
        client, vault_id="vlt_1", credentials=_stored(token="tok_shared", version="v9")
    )

    assert created == [] and updated == [] and deleted == [], (
        "the person's own sign-in outranks the shared token; nothing is written or removed"
    )


async def test_mirror_tolerates_a_409_from_a_concurrent_create() -> None:
    """Two turns for the same caller can both find the slot empty; the loser's
    409 means the credential exists, which is what the mirror wanted."""
    created: list[dict[str, Any]] = []
    client = build_fake_anthropic(
        _vault_handler(creds=[], created=created, updated=[], deleted=[], conflict=True)
    )

    await mirror_credentials_into_vault(
        client, vault_id="vlt_1", credentials=_stored(token="tok_1", version="v1")
    )

    # The SDK retries a 409 on its own before raising ConflictError, so the
    # fake sees more than one identical create; what matters is no exception.
    assert created and all(body == created[0] for body in created), (
        "the create was attempted and its 409 was not a failure"
    )


async def test_reused_session_refresh_succeeds_when_the_caller_holds_an_oauth_grant(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The staging failure end to end: the agent keeps its shared static token
    row, the caller's vault holds an `mcp_oauth` grant at that URL, and the
    per-turn refresh (`RemirrorVaultCredentials` -> `sync_agent_mcp_credentials`)
    must complete without writing anything."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    agent_id = uuid.uuid4()
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    notion = "https://mcp.notion.com/mcp"
    public_url = "https://mcp.example.com/mcp"
    await save_agent_mcp_credential(
        sessionmaker=db_session_factory,
        fernet=fernet,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url=notion,
        plaintext_token="ntn_rejected_long_ago",
    )

    writes: list[str] = []
    router = MARouter()
    router.add(
        "GET",
        r"/v1/vaults",
        lambda _r, _m: list_response(
            [
                {
                    "id": "vlt_me",
                    "type": "vault",
                    "display_name": f"daimon-mcp:{account.id}:{agent_id}",
                    "metadata": None,
                    "archived_at": None,
                    "created_at": "2026-09-01T00:00:00Z",
                }
            ]
        ),
    )
    reads: list[str] = []

    def list_creds(_r: httpx.Request, _m: Any) -> httpx.Response:
        reads.append("list")
        return list_response(
            [
                {
                    "id": "vcrd_jwt",
                    "metadata": {"daimon_chat_identity": str(agent_id)},
                    "type": "vault_credential",
                    "vault_id": "vlt_me",
                    "auth": {"type": "static_bearer", "mcp_server_url": public_url},
                    "created_at": "2026-09-01T00:00:00Z",
                    "updated_at": "2026-09-01T00:00:00Z",
                    "archived_at": None,
                    "display_name": None,
                },
                {
                    "id": "vcrd_grant",
                    "type": "vault_credential",
                    "vault_id": "vlt_me",
                    "auth": {"type": "mcp_oauth", "mcp_server_url": notion},
                    "created_at": "2026-09-15T14:04:00Z",
                    "updated_at": "2026-09-15T14:04:00Z",
                    "archived_at": None,
                    "display_name": None,
                    "metadata": None,
                },
            ]
        )

    router.add("GET", r"/v1/vaults/vlt_me/credentials", list_creds)

    def refuse(req: httpx.Request, _m: Any) -> httpx.Response:
        writes.append(f"{req.method} {req.url.path}")
        return httpx.Response(409, json={"type": "error", "error": {"message": "exists"}})

    router.add("POST", r"/v1/vaults/vlt_me/credentials.*", refuse)
    router.add("DELETE", r"/v1/vaults/vlt_me/credentials/.*", refuse)

    await sync_agent_mcp_credentials(
        build_fake_anthropic(router.dispatch),
        sessionmaker=db_session_factory,
        fernet=fernet,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=account.id,
        jwt_secret=b"x" * 32,
        public_url=public_url,
        now=dt.datetime(2026, 9, 15, 14, 5, tzinfo=dt.UTC),
    )

    assert reads, "the refresh reached the mirror and read the vault"
    assert writes == [], f"the refresh must not touch the vault; it tried {writes}"


async def test_delete_credential_matches_a_row_stored_with_a_trailing_slash(
    db_session: AsyncSession,
) -> None:
    """The setup panel stored URLs as typed; detach strips the slash before asking."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await cred_store.upsert_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url="https://example.com/mcp/",
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
    ), "a slash-only difference must not leave the token row behind"
    assert not await cred_store.list_credentials(
        db_session, tenant_id=tenant.id, agent_id=agent_id
    ), "no token row is left for the mirror to keep pushing"
