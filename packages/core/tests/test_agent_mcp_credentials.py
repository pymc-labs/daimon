"""The DB read behind per-caller MCP server visibility — real Postgres.

`resolve_hidden_mcp_server_names` is the OAuth counterpart of the credential
mirror: it answers which of an agent's servers this caller has no way to
authenticate, so `create_session` can leave them off. The classification
itself is unit-tested in `test_mcp_personal_servers.py`.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from cryptography.fernet import Fernet
from daimon.core.agent_mcp_credentials import (
    forget_mirrored_mcp_token,
    resolve_hidden_mcp_server_names,
    save_agent_mcp_credential,
)
from daimon.core.credential_requests import mint_request_token
from daimon.core.github_credentials import build_multifernet
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.testing.factories import make_account, make_tenant
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NOW = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
_SERVER_URL = "https://mcp.example.com/docs"


async def _record_sign_in(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    requester: str,
) -> None:
    """Drive one OAuth handshake through to a stored grant."""
    request = await requests_store.create_credential_request(
        session,
        token=mint_request_token(),
        kind="mcp_oauth",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=account_id,
        target="docs",
        mcp_server_url=_SERVER_URL,
        requester_platform_user_id=requester,
        channel_id="chan-1",
        expires_at=_NOW + timedelta(minutes=30),
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id="ag_test",
        target_name="daimon",
        requested_work=None,
    )
    flow = await flows_store.create_flow(
        session,
        state="st_" + uuid.uuid4().hex,
        request_token=request.token,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=agent_id,
        server_name="docs",
        mcp_server_url=_SERVER_URL,
        redirect_uri="https://d.example/oauth/mcp/callback",
        code_verifier="verifier",
        expires_at=_NOW + timedelta(minutes=10),
    )
    await flows_store.consume_flow(session, state=flow.state, now=_NOW)
    await flows_store.mark_flow_completed(session, state=flow.state, now=_NOW)
    await session.commit()


async def test_a_server_one_person_connected_is_hidden_from_everyone_else(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """One account signs in and every other member's turn opens a server they
    hold no token for, so every reply carries the degraded-turn notice."""
    tenant = await make_tenant(db_session)
    connected = await make_account(db_session, tenant=tenant)
    bystander = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()
    await _record_sign_in(
        db_session,
        tenant_id=tenant.id,
        account_id=connected.id,
        agent_id=agent_id,
        requester="requester-connected",
    )

    assert await resolve_hidden_mcp_server_names(
        db_session_factory,
        tenant_id=tenant.id,
        agent_id=agent_id,
        account_id=bystander.id,
        server_urls={"docs": _SERVER_URL},
    ) == frozenset({"docs"}), "a member who never signed in must not be handed the server"
    assert (
        await resolve_hidden_mcp_server_names(
            db_session_factory,
            tenant_id=tenant.id,
            agent_id=agent_id,
            account_id=connected.id,
            server_urls={"docs": _SERVER_URL},
        )
        == frozenset()
    ), "the member who signed in keeps it"


async def test_nothing_is_hidden_for_an_agent_nobody_signed_in_to(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    assert (
        await resolve_hidden_mcp_server_names(
            db_session_factory,
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            account_id=account.id,
            server_urls={"daimon-mcp": "https://daimon.example/mcp"},
        )
        == frozenset()
    ), "the common case must cost one read and hide nothing"


async def test_an_agent_wide_token_keeps_the_server_visible_to_everyone(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An admin's stored token is mirrored into every caller's vault, so the
    server stays even though somebody also signed in to it personally."""
    tenant = await make_tenant(db_session)
    connected = await make_account(db_session, tenant=tenant)
    bystander = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()
    await _record_sign_in(
        db_session,
        tenant_id=tenant.id,
        account_id=connected.id,
        agent_id=agent_id,
        requester="requester-connected",
    )
    await save_agent_mcp_credential(
        sessionmaker=db_session_factory,
        fernet=build_multifernet((Fernet.generate_key().decode(),)),
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url=_SERVER_URL,
        plaintext_token="tok_shared",
    )

    assert (
        await resolve_hidden_mcp_server_names(
            db_session_factory,
            tenant_id=tenant.id,
            agent_id=agent_id,
            account_id=bystander.id,
            server_urls={"docs": _SERVER_URL},
        )
        == frozenset()
    ), "a token every caller's vault gets is not a personal connection"


async def test_a_fork_of_the_agent_hides_the_copied_server_from_the_person_who_signed_in(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Forks copy the source's MCP servers raw; the grant stays in the vault of
    (person, source agent). On the fork nobody can open the server — the
    connector included — so it is hidden there until someone signs in on the
    fork, instead of failing every turn."""
    tenant = await make_tenant(db_session)
    connected = await make_account(db_session, tenant=tenant)
    source_agent_id = uuid.uuid4()
    fork_agent_id = uuid.uuid4()
    await _record_sign_in(
        db_session,
        tenant_id=tenant.id,
        account_id=connected.id,
        agent_id=source_agent_id,
        requester="requester-connected",
    )

    assert (
        await resolve_hidden_mcp_server_names(
            db_session_factory,
            tenant_id=tenant.id,
            agent_id=source_agent_id,
            account_id=connected.id,
            server_urls={"docs": _SERVER_URL},
        )
        == frozenset()
    ), "on the agent they signed in to, the connector keeps the server"
    assert await resolve_hidden_mcp_server_names(
        db_session_factory,
        tenant_id=tenant.id,
        agent_id=fork_agent_id,
        account_id=connected.id,
        server_urls={"docs": _SERVER_URL},
    ) == frozenset({"docs"}), "on a fork, the same person holds no grant and the server is hidden"


async def test_owned_credential_metadata_omits_token_columns_and_foreign_refs(
    db_preflight, db_committing_engine
):
    from daimon.core.stores import agent_mcp_credentials as credentials
    from daimon.testing.db import require_owned_sessionmaker
    from sqlalchemy import event

    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    require_owned_sessionmaker(sessions)
    agent_id = uuid.uuid4()
    async with sessions.begin() as db:
        tenant = await make_tenant(db)
        foreign = await make_tenant(db)
        owned = await credentials.upsert_credential(
            db,
            tenant_id=tenant.id,
            agent_id=agent_id,
            mcp_server_url=_SERVER_URL,
            encrypted_token=b"ciphertext-owned",
        )
        await credentials.upsert_credential(
            db,
            tenant_id=foreign.id,
            agent_id=agent_id,
            mcp_server_url="https://foreign.example.test",
            encrypted_token=b"ciphertext-foreign",
        )
    queries = []

    def capture(connection, cursor, statement, parameters, context, many):
        queries.append(statement)

    event.listen(db_committing_engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with sessions() as db:
            refs = await credentials.list_credential_revisions(
                db, tenant_id=tenant.id, agent_id=agent_id
            )
    finally:
        event.remove(db_committing_engine.sync_engine, "before_cursor_execute", capture)
    assert refs == ((owned.id, _SERVER_URL, owned.updated_at),)
    assert all("encrypted_token" not in sql for sql in queries)
    assert "ciphertext" not in repr(refs)


_BRIDGE_URL = "https://bridge.internal.example/mcp"
_PUBLIC_URL = "https://daimon.example/mcp"


class _Vaults:
    """MA vaults and their credentials, enough for listing and deleting.

    A vault id in `refused` answers its credential listing with a 403."""

    def __init__(self) -> None:
        self.vaults: dict[str, dict[str, Any]] = {}
        self.credentials: dict[str, dict[str, dict[str, Any]]] = {}
        self.refused: set[str] = set()
        self.router = MARouter()
        self.router.add(
            "GET",
            r"/v1/vaults$",
            lambda _r, _m: httpx.Response(
                200, json={"data": list(self.vaults.values()), "next_page": None}
            ),
        )
        self.router.add("GET", r"/v1/vaults/(?P<vid>[^/]+)/credentials$", self._list)
        self.router.add(
            "DELETE", r"/v1/vaults/(?P<vid>[^/]+)/credentials/(?P<cid>[^/]+)$", self._delete
        )

    def vault(self, display_name: str, *creds: tuple[str, str], archived: bool = False) -> str:
        vault_id = f"vlt_{len(self.vaults)}"
        self.vaults[vault_id] = {
            "id": vault_id,
            "type": "vault",
            "display_name": display_name,
            "metadata": {},
            "archived_at": "2026-10-01T00:00:00Z" if archived else None,
            "created_at": "2026-10-01T00:00:00Z",
            "updated_at": "2026-10-01T00:00:00Z",
        }
        self.credentials[vault_id] = {}
        for auth_type, url in creds:
            cred_id = f"vcrd_{vault_id}_{len(self.credentials[vault_id])}"
            self.credentials[vault_id][cred_id] = {
                "id": cred_id,
                "type": "vault_credential",
                "vault_id": vault_id,
                "display_name": None,
                "metadata": {},
                "created_at": "2026-10-01T00:00:00Z",
                "updated_at": "2026-10-01T00:00:00Z",
                "auth": {"type": auth_type, "mcp_server_url": url},
            }
        return vault_id

    def held(self, vault_id: str) -> list[tuple[str, str]]:
        return sorted(
            (c["auth"]["type"], c["auth"]["mcp_server_url"])
            for c in self.credentials[vault_id].values()
        )

    def _list(self, _request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        if match["vid"] in self.refused:
            return httpx.Response(
                403, json={"type": "error", "error": {"type": "permission_error", "message": "no"}}
            )
        return httpx.Response(
            200,
            json={"data": list(self.credentials[match["vid"]].values()), "next_page": None},
        )

    def _delete(self, _request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        del self.credentials[match["vid"]][match["cid"]]
        return httpx.Response(200, json={"id": match["cid"], "type": "vault_credential_deleted"})


def _static(url: str) -> tuple[str, str]:
    return ("static_bearer", url)


async def test_forgetting_a_token_deletes_every_copy_in_the_agents_vaults(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The mirror never deletes, so a detached server's token stayed in each
    caller's vault until someone removed it by hand."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent_id, alice, bob, owner = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    fake = _Vaults()
    alices = fake.vault(
        f"daimon-mcp:{alice}:{agent_id}",
        _static(_PUBLIC_URL),
        _static(_BRIDGE_URL + "/"),
        _static("https://docs.example/mcp"),
    )
    bobs_slack_turn = fake.vault(
        f"daimon-mcp:{bob}:{agent_id}:slack-turn:{uuid.uuid4()}", _static(_BRIDGE_URL)
    )
    shared = fake.vault(f"daimon-shared:{owner}:{agent_id}", _static(_BRIDGE_URL))

    failed = await forget_mirrored_mcp_token(
        build_fake_anthropic(fake.router.dispatch),
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url=_BRIDGE_URL,
    )

    assert failed == ()
    assert fake.held(alices) == [
        ("static_bearer", _PUBLIC_URL),
        ("static_bearer", "https://docs.example/mcp"),
    ], "only the detached server's copy goes; Daimon's own key and other servers stay"
    assert fake.held(bobs_slack_turn) == [], "a Slack-turn vault's copy goes too"
    assert fake.held(shared) == [], "a shared thread's copy goes too"


async def test_forgetting_a_token_leaves_other_agents_and_personal_sign_ins_alone(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent_id, other_agent, alice = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    fake = _Vaults()
    other = fake.vault(f"daimon-mcp:{alice}:{other_agent}", _static(_BRIDGE_URL))
    legacy = fake.vault(f"daimon-mcp:{alice}", _static(_BRIDGE_URL))
    signed_in = fake.vault(f"daimon-mcp:{alice}:{agent_id}", ("mcp_oauth", _BRIDGE_URL))
    archived = fake.vault(f"daimon-mcp:{alice}:{agent_id}", _static(_BRIDGE_URL), archived=True)

    failed = await forget_mirrored_mcp_token(
        build_fake_anthropic(fake.router.dispatch),
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url=_BRIDGE_URL,
    )

    assert failed == ()
    assert fake.held(other) == [("static_bearer", _BRIDGE_URL)], (
        "another agent's own token for the same server is not a copy"
    )
    assert fake.held(legacy) == [("static_bearer", _BRIDGE_URL)], "a vault of no agent stays"
    assert fake.held(signed_in) == [("mcp_oauth", _BRIDGE_URL)], "a person's sign-in stays"
    assert fake.held(archived) == [("static_bearer", _BRIDGE_URL)], "an archived vault is skipped"


async def test_forgetting_a_token_reports_a_vault_it_could_not_clear_and_clears_the_rest(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    agent_id, alice, bob = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    fake = _Vaults()
    refused = fake.vault(f"daimon-mcp:{alice}:{agent_id}", _static(_BRIDGE_URL))
    fake.refused.add(refused)
    cleared = fake.vault(f"daimon-mcp:{bob}:{agent_id}", _static(_BRIDGE_URL))

    failed = await forget_mirrored_mcp_token(
        build_fake_anthropic(fake.router.dispatch),
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
        agent_id=agent_id,
        mcp_server_url=_BRIDGE_URL,
    )

    assert failed == (refused,)
    assert fake.held(cleared) == [], "one vault failing must not stop the others"
