"""Router session records and the token rows a routed session's rotations stamp.

A record authenticates by the SHA-256 of an opaque bearer, closes locally,
and binds every token row its session mints, through full rotation, failed
delivery and restore, so the router finds the replacement rows once the old
ones are superseded.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet, MultiFernet
from daimon.core import github_app_session
from daimon.core._models import (
    AgentGitHubGrant,
    GitHubAppSessionVault,
    GitHubIssuedToken,
    GitHubRouterSession,
    Tenant,
    TenantGitHubRepo,
    ThreadSession,
)
from daimon.core.config import GithubAppSettings
from daimon.core.github_app_session import (
    AppSessionAccess,
    close_headless_app_session,
    finish_app_delivery,
    prepare_app_access,
    revoke_session_tokens,
    rotate_live_app_tokens,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.github_requester_access import PermissionCache
from daimon.core.github_router_tokens import (
    TokenChoice,
    choose_token,
    load_grant_view,
    load_usable_rows,
    resolve_call,
)
from daimon.core.purge import purge_account
from daimon.core.session_snapshot import SessionSnapshot
from daimon.core.stores import github_app_installations, github_issued_tokens
from daimon.core.stores import github_router_sessions as router_sessions
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.testing.factories import make_account, make_tenant
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SESSION_ID = "sesn_router"
LATER = timedelta(hours=1)


async def _tenant(session: AsyncSession) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await session.flush()
    return tenant_id


async def _record(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    ma_session_id: str | None = SESSION_ID,
    account_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
) -> tuple[router_sessions.GitHubRouterSessionRecord, str]:
    return await router_sessions.create(
        session,
        tenant_id=tenant_id,
        agent_id=uuid.uuid4(),
        account_id=account_id,
        expires_at=expires_at or datetime.now(UTC) + LATER,
        ma_session_id=ma_session_id,
    )


# --- Store lifecycle -----------------------------------------------------------


async def test_create_stores_only_the_bearer_hash_and_verify_finds_the_record(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _tenant(db_session)
    record, bearer = await _record(db_session, tenant_id)
    assert len(bearer) >= 43, "32 random bytes, urlsafe base64"
    row = await db_session.get(GitHubRouterSession, record.id)
    assert row is not None
    assert row.credential_sha256 == hashlib.sha256(bearer.encode()).digest()
    stored_text = " ".join(str(value) for value in vars(row).values())
    assert bearer not in stored_text, "the plaintext bearer is never stored"
    assert not hasattr(record, "credential_sha256"), "the record model hides the hash"
    verified = await router_sessions.verify(db_session, bearer=bearer)
    assert verified == record
    assert await router_sessions.verify(db_session, bearer=bearer + "x") is None
    assert await router_sessions.verify(db_session, bearer="") is None


async def test_two_records_get_different_bearers(db_session: AsyncSession) -> None:
    tenant_id = await _tenant(db_session)
    _, first = await _record(db_session, tenant_id)
    _, second = await _record(db_session, tenant_id)
    assert first != second


async def test_expired_record_does_not_verify(db_session: AsyncSession) -> None:
    tenant_id = await _tenant(db_session)
    record, bearer = await _record(db_session, tenant_id)
    later = record.expires_at + timedelta(seconds=1)
    assert await router_sessions.verify(db_session, bearer=bearer, now=later) is None


async def test_close_local_is_idempotent_and_stops_verify(db_session: AsyncSession) -> None:
    tenant_id = await _tenant(db_session)
    record, bearer = await _record(db_session, tenant_id)
    first = datetime.now(UTC)
    await router_sessions.close_local(db_session, router_session_id=record.id, now=first)
    await router_sessions.close_local(
        db_session, router_session_id=record.id, now=first + timedelta(minutes=5)
    )
    closed = await router_sessions.get(db_session, router_session_id=record.id)
    assert closed is not None and closed.closed_at == first
    assert await router_sessions.verify(db_session, bearer=bearer) is None
    assert not await router_sessions.renew(
        db_session, router_session_id=record.id, expires_at=first + LATER * 2
    )


async def test_close_for_ma_session_closes_its_open_records(db_session: AsyncSession) -> None:
    tenant_id = await _tenant(db_session)
    _, bearer = await _record(db_session, tenant_id)
    _, other = await _record(db_session, tenant_id, ma_session_id="sesn_other")
    assert await router_sessions.close_for_ma_session(db_session, ma_session_id=SESSION_ID) == 1
    assert await router_sessions.verify(db_session, bearer=bearer) is None
    assert await router_sessions.verify(db_session, bearer=other) is not None


async def test_renew_extends_but_never_shortens(db_session: AsyncSession) -> None:
    tenant_id = await _tenant(db_session)
    record, _ = await _record(db_session, tenant_id)
    longer = record.expires_at + LATER
    assert await router_sessions.renew(db_session, router_session_id=record.id, expires_at=longer)
    assert await router_sessions.renew(
        db_session, router_session_id=record.id, expires_at=record.expires_at
    )
    renewed = await router_sessions.get(db_session, router_session_id=record.id)
    assert renewed is not None and renewed.expires_at == longer
    with pytest.raises(ValueError, match="timezone"):
        await router_sessions.renew(
            db_session, router_session_id=record.id, expires_at=datetime(2030, 1, 1)
        )


async def test_set_ma_session_id_binds_once(db_session: AsyncSession) -> None:
    tenant_id = await _tenant(db_session)
    record, _ = await _record(db_session, tenant_id, ma_session_id=None)
    await router_sessions.set_ma_session_id(
        db_session, router_session_id=record.id, ma_session_id=SESSION_ID
    )
    await router_sessions.set_ma_session_id(
        db_session, router_session_id=record.id, ma_session_id=SESSION_ID
    )
    with pytest.raises(ValueError, match="another session"):
        await router_sessions.set_ma_session_id(
            db_session, router_session_id=record.id, ma_session_id="sesn_other"
        )


async def test_touch_last_used_writes_at_most_once_a_minute(db_session: AsyncSession) -> None:
    tenant_id = await _tenant(db_session)
    record, _ = await _record(db_session, tenant_id)
    now = datetime.now(UTC)
    assert await router_sessions.touch_last_used(db_session, router_session_id=record.id, now=now)
    assert not await router_sessions.touch_last_used(
        db_session, router_session_id=record.id, now=now + timedelta(seconds=30)
    )
    assert await router_sessions.touch_last_used(
        db_session, router_session_id=record.id, now=now + timedelta(seconds=61)
    )
    touched = await router_sessions.get(db_session, router_session_id=record.id)
    assert touched is not None and touched.last_used_at == now + timedelta(seconds=61)


# --- Live-session binding --------------------------------------------------------


async def test_chat_session_is_live_while_its_thread_session_is_live(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _tenant(db_session)
    record, _ = await _record(db_session, tenant_id)
    assert not await router_sessions.is_session_live(db_session, record)
    await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="thread-1",
        account_id=uuid.uuid4(),
        ma_session_id=SESSION_ID,
    )
    assert await router_sessions.is_session_live(db_session, record)
    await db_session.execute(
        update(ThreadSession).where(ThreadSession.ma_session_id == SESSION_ID).values(status="dead")
    )
    assert not await router_sessions.is_session_live(db_session, record)


async def _headless_vault(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    await github_issued_tokens.register_headless_app_session(
        session, session_id=SESSION_ID, tenant_id=tenant_id, vault_id="vlt_1"
    )
    # A single MCP-origin turn can run for hours while the scheduler rotates
    # its tokens; its last turn start stays where it was.
    await session.execute(
        update(GitHubAppSessionVault)
        .where(GitHubAppSessionVault.session_id == SESSION_ID)
        .values(last_started_at=datetime.now(UTC) - timedelta(hours=2))
    )


async def test_long_headless_turn_stays_live_until_its_vault_finishes(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _tenant(db_session)
    record, _ = await _record(db_session, tenant_id)
    await _headless_vault(db_session, tenant_id)
    assert await router_sessions.is_session_live(db_session, record)
    await db_session.execute(
        update(GitHubAppSessionVault)
        .where(GitHubAppSessionVault.session_id == SESSION_ID)
        .values(finished_at=datetime.now(UTC))
    )
    assert not await router_sessions.is_session_live(db_session, record)


async def test_long_headless_turn_stops_being_live_when_its_vault_closes(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _tenant(db_session)
    record, _ = await _record(db_session, tenant_id)
    await _headless_vault(db_session, tenant_id)
    await github_issued_tokens.mark_headless_app_session_closed(db_session, session_id=SESSION_ID)
    assert not await router_sessions.is_session_live(db_session, record)


async def test_live_session_of_another_tenant_does_not_count(db_session: AsyncSession) -> None:
    tenant_id = await _tenant(db_session)
    other_tenant = await _tenant(db_session)
    record, _ = await _record(db_session, tenant_id)
    await github_issued_tokens.register_headless_app_session(
        db_session, session_id=SESSION_ID, tenant_id=other_tenant, vault_id="vlt_1"
    )
    assert not await router_sessions.is_session_live(db_session, record)


async def test_record_without_a_session_id_is_not_live(db_session: AsyncSession) -> None:
    tenant_id = await _tenant(db_session)
    record, _ = await _record(db_session, tenant_id, ma_session_id=None)
    assert not await router_sessions.is_session_live(db_session, record)


# --- Privacy erasure --------------------------------------------------------------


async def test_account_erasure_closes_the_persons_router_sessions(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    record, bearer = await _record(db_session, tenant.id, account_id=account.id)
    _, shared_bearer = await _record(db_session, tenant.id, ma_session_id="sesn_shared")
    await db_session.commit()

    await purge_account(sm=db_session_factory, account_id=account.id)

    async with db_session_factory() as session:
        erased = await router_sessions.get(session, router_session_id=record.id)
        assert erased is not None
        assert erased.closed_at is not None, "erasure revokes the bearer locally"
        assert erased.account_id is None, "ON DELETE SET NULL detaches the person"
        assert await router_sessions.verify(session, bearer=bearer) is None
        assert await router_sessions.verify(session, bearer=shared_bearer) is not None


# --- Token rows ---------------------------------------------------------------------


def _fernet() -> MultiFernet:
    return build_multifernet((Fernet.generate_key().decode(),))


async def _token_row(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    fernet: MultiFernet,
    *,
    repo_id: int,
    router_session_id: uuid.UUID | None,
    session_id: str = SESSION_ID,
    status: str = "delivered",
    superseded: bool = False,
    expires_in: timedelta = timedelta(minutes=50),
    value: str = "ghs_value",
) -> uuid.UUID:
    row = GitHubIssuedToken(
        tenant_id=tenant_id,
        agent_id=uuid.uuid4(),
        session_id=session_id,
        installation_id=771,
        repo_ids=[repo_id],
        permissions={"contents": "write"},
        grant_versions={f"grant:{repo_id}": 1, f"authorization:{repo_id}": 1},
        encrypted_token=fernet.encrypt(value.encode()),
        expires_at=datetime.now(UTC) + expires_in,
        status=status,
        revoke_attempts=0,
        superseded_at=datetime.now(UTC) if superseded else None,
        router_session_id=router_session_id,
    )
    session.add(row)
    await session.flush()
    return row.token_id


async def test_load_usable_rows_reads_only_current_rows_of_the_router_session(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _tenant(db_session)
    record, _ = await _record(db_session, tenant_id)
    other, _ = await _record(db_session, tenant_id, ma_session_id="sesn_other")
    fernet = _fernet()
    current = await _token_row(
        db_session, tenant_id, fernet, repo_id=1, router_session_id=record.id, value="ghs_now"
    )
    for kwargs in (
        {"superseded": True},
        {"status": "stored"},
        {"status": "revoked"},
        {"expires_in": timedelta(seconds=30)},
    ):
        await _token_row(
            db_session, tenant_id, fernet, repo_id=1, router_session_id=record.id, **kwargs
        )
    await _token_row(db_session, tenant_id, fernet, repo_id=1, router_session_id=other.id)
    await _token_row(db_session, tenant_id, fernet, repo_id=1, router_session_id=None)

    rows = await load_usable_rows(db_session, record.id, fernet=fernet)

    assert [(row.token_id, row.token) for row in rows] == [(current, "ghs_now")]


async def test_row_that_cannot_be_decrypted_is_skipped(db_session: AsyncSession) -> None:
    tenant_id = await _tenant(db_session)
    record, _ = await _record(db_session, tenant_id)
    await _token_row(db_session, tenant_id, _fernet(), repo_id=1, router_session_id=record.id)
    assert await load_usable_rows(db_session, record.id, fernet=_fernet()) == []


async def test_store_steps_stamp_the_router_session_and_none_keeps_it(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _tenant(db_session)
    record, _ = await _record(db_session, tenant_id)
    fernet = _fernet()
    provisional = await _token_row(
        db_session, tenant_id, fernet, repo_id=1, router_session_id=None, session_id="pending:x"
    )
    restored = await _token_row(
        db_session, tenant_id, fernet, repo_id=2, router_session_id=None, superseded=True
    )
    await github_issued_tokens.set_session_id(
        db_session,
        provisional_session_id="pending:x",
        session_id=SESSION_ID,
        router_session_id=record.id,
    )
    await github_issued_tokens.restore_session_tokens(
        db_session, token_ids=frozenset({restored}), router_session_id=record.id
    )
    # A later step without a router session id must not clear the stamp.
    await github_issued_tokens.set_session_id(
        db_session, provisional_session_id=SESSION_ID, session_id=SESSION_ID
    )
    result = await db_session.execute(
        select(GitHubIssuedToken.token_id, GitHubIssuedToken.router_session_id)
    )
    stamps = {token_id: router_id for token_id, router_id in result.all()}
    # keep_failed_rotation_tokens is covered with real grants by the failed-delivery test.
    assert stamps == {provisional: record.id, restored: record.id}


async def test_router_session_lookup_prefers_the_open_record_then_the_rows(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _tenant(db_session)
    record, _ = await _record(db_session, tenant_id)
    assert (
        await router_sessions.router_session_id_for_ma_session(db_session, ma_session_id=SESSION_ID)
        == record.id
    )
    unbound, _ = await _record(db_session, tenant_id, ma_session_id=None)
    await _token_row(
        db_session,
        tenant_id,
        _fernet(),
        repo_id=1,
        router_session_id=unbound.id,
        session_id="sesn_rows",
    )
    assert (
        await router_sessions.router_session_id_for_ma_session(
            db_session, ma_session_id="sesn_rows"
        )
        == unbound.id
    )
    assert (
        await router_sessions.router_session_id_for_ma_session(
            db_session, ma_session_id="sesn_direct"
        )
        is None
    )


# --- Rotation propagation -------------------------------------------------------------


def _fake_jwt(*_args: Any, **_kwargs: Any) -> str:
    return "jwt"


async def _no_call(*_args: Any, **_kwargs: Any) -> None:
    return None


class _GitHub:
    """A mock GitHub that mints a fresh token value per request."""

    def __init__(self) -> None:
        self.minted: list[list[int]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        self.minted.append(json.loads(request.content)["repository_ids"])
        return httpx.Response(201, json={"token": f"ghs_minted_{len(self.minted)}"})


async def _seed_two_installations(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    async with factory.begin() as session:
        session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
        await session.flush()
        for installation_id, repo_id, name in ((771, 101, "a/one"), (772, 102, "b/two")):
            session.add(
                TenantGitHubRepo(
                    tenant_id=tenant_id,
                    repo_id=repo_id,
                    owner_id=installation_id,
                    installation_id=installation_id,
                    repo_full_name=name,
                    max_access="write",
                    authorized_by_github_user_id=2,
                    status="active",
                    version=1,
                )
            )
        await session.flush()
        for installation_id, owner in ((771, "a"), (772, "b")):
            await github_app_installations.upsert_github_app(
                session,
                installation_id=installation_id,
                account_id=installation_id,
                account_login=owner,
                account_type="Organization",
                repository_selection="selected",
                suspended_at=None,
            )
        for repo_id in (101, 102):
            session.add(
                AgentGitHubGrant(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    repo_id=repo_id,
                    baseline_access="write",
                    ceiling_access="write",
                    mount_path=None,
                    is_working_repo=repo_id == 101,
                    granted_by_account_id=None,
                )
            )
    return tenant_id, agent_id


async def _routed_session(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    github: _GitHub,
    fernet: MultiFernet,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, AppSessionAccess]:
    """A delivered two-installation session whose rows carry its router session."""
    tenant_id, agent_id = await _seed_two_installations(factory)
    async with factory.begin() as session:
        record, _ = await router_sessions.create(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            expires_at=datetime.now(UTC) + LATER,
            ma_session_id=SESSION_ID,
        )
    monkeypatch.setattr(github_app_session, "build_app_jwt", _fake_jwt)
    monkeypatch.setattr(github_app_session, "revoke_token", _no_call)
    monkeypatch.setattr(github_app_session, "rotate_session_repo_token", _no_call)
    config = GithubAppSettings(app_id="123", private_key=SecretStr("unused"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as client:
        initial = await prepare_app_access(
            factory,
            client,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=None,
            provisional_session_id="pending:initial",
            config=config,
            fernet=fernet,
            cache=PermissionCache(),
            router_session_id=record.id,
        )
        await finish_app_delivery(
            factory,
            client,
            access=initial,
            provisional_session_id="pending:initial",
            session_id=SESSION_ID,
        )
    async with factory.begin() as session:
        await create_thread_session(
            session,
            tenant_id=tenant_id,
            platform="discord",
            thread_id="two-installations",
            account_id=uuid.uuid4(),
            ma_session_id=SESSION_ID,
            effective_config=SessionSnapshot.model_validate(
                {
                    "ma_agent_id": "agent_router",
                    "model_id": "test-model",
                    "system_sha256": None,
                    "skills_sha256": "skills",
                    "environment_id": "environment",
                    "github_mode": "app",
                    "repo_urls": ["https://github.com/a/one"],
                    "token_repo_urls": ["https://github.com/a/one", "https://github.com/b/two"],
                    "token_scopes": initial.token_scopes,
                    "token_issued_at_by_installation": {
                        token.installation_id: token.issued_at for token in initial.tokens
                    },
                    "repo_url": None,
                    "repo_branch": None,
                    "memory_store_id": None,
                    "vault_id": "vault",
                    "tools_sha256": "tools",
                    "mcp_servers_sha256": "servers",
                    "env_sha256": None,
                    "agent_version": 1,
                    "agent_name": "Agent",
                }
            ),
        )

    async def prepare_with_transport(*args: Any, **kwargs: Any) -> AppSessionAccess:
        kwargs.pop("anthropic", None)
        kwargs.pop("vault_id", None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as mock_client:
            return await prepare_app_access(factory, mock_client, **kwargs)

    monkeypatch.setattr(github_app_session, "prepare_app_access", prepare_with_transport)
    return tenant_id, agent_id, record.id, initial


async def _live_rows(
    factory: async_sessionmaker[AsyncSession],
) -> list[github_issued_tokens.IssuedToken]:
    async with factory() as session:
        return await github_issued_tokens.list_session_tokens(session, session_id=SESSION_ID)


async def _rotate(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    fernet: MultiFernet,
    *,
    active_turn: bool = False,
) -> AppSessionAccess:
    return await rotate_live_app_tokens(
        None,  # type: ignore[arg-type]  # every MA call is stubbed
        factory,
        session_id=SESSION_ID,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        vault_id="vault",
        resource_ids={},
        config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
        fernet=fernet,
        active_turn=active_turn,
    )


async def _route(
    factory: async_sessionmaker[AsyncSession],
    router_session_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    fernet: MultiFernet,
    owner: str,
    repo: str,
) -> str:
    async with factory() as session:
        rows = await load_usable_rows(session, router_session_id, fernet=fernet)
        view = await load_grant_view(session, tenant_id=tenant_id, agent_id=agent_id)
    decision = choose_token(
        resolve_call("get_file_contents", {"owner": owner, "repo": repo}),
        rows,
        view,
        view.working_repo_id,
    )
    assert isinstance(decision, TokenChoice), decision
    return decision.token.token


async def test_full_rotation_stamps_new_rows_and_the_router_finds_them(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, fernet = _GitHub(), _fernet()
    tenant_id, agent_id, router_id, initial = await _routed_session(
        db_session_factory, monkeypatch, github, fernet
    )
    assert {row.router_session_id for row in await _live_rows(db_session_factory)} == {router_id}
    before = await _route(db_session_factory, router_id, tenant_id, agent_id, fernet, "b", "two")
    assert before == next(t.token for t in initial.tokens if t.installation_id == 772)
    monkeypatch.setattr(github_app_session, "add_app_credentials", _no_call)

    # The scheduler does not pass the router session id; rotation finds it.
    started = datetime.now(UTC)
    rotated = await _rotate(db_session_factory, tenant_id, agent_id, fernet)
    async with db_session_factory() as session:
        renewed = await router_sessions.get(session, router_session_id=router_id)
    assert renewed is not None
    assert renewed.expires_at >= started + router_sessions.GITHUB_ROUTER_SESSION_TTL, (
        "a successful rotation renews the router record"
    )

    rows = await _live_rows(db_session_factory)
    new_ids = {token.token_id for token in rotated.tokens}
    assert {row.token_id for row in rows if row.superseded_at is None} == new_ids
    assert {row.token_id for row in rows if row.superseded_at is not None} == {
        token.token_id for token in initial.tokens
    }
    assert {row.router_session_id for row in rows} == {router_id}, "every row stays routed"
    after = await _route(db_session_factory, router_id, tenant_id, agent_id, fernet, "b", "two")
    assert after == next(t.token for t in rotated.tokens if t.installation_id == 772)
    assert after != before
    assert await _route(
        db_session_factory, router_id, tenant_id, agent_id, fernet, "a", "one"
    ) == next(t.token for t in rotated.tokens if t.installation_id == 771)


async def test_failed_delivery_restores_old_rows_and_keeps_every_row_routed(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github, fernet = _GitHub(), _fernet()
    tenant_id, agent_id, router_id, initial = await _routed_session(
        db_session_factory, monkeypatch, github, fernet
    )
    vault_writes: list[AppSessionAccess] = []

    async def deliver_one_then_fail(
        *_args: Any,
        access: AppSessionAccess,
        on_delivered: Callable[[str], None] | None = None,
        **_kwargs: Any,
    ) -> None:
        vault_writes.append(access)
        if len(vault_writes) == 1:
            # A running tool may already hold the first new token.
            assert on_delivered is not None
            on_delivered(access.tokens[0].token)
            raise RuntimeError("vault write failed")

    monkeypatch.setattr(github_app_session, "add_app_credentials", deliver_one_then_fail)

    with pytest.raises(RuntimeError, match="vault write failed"):
        await _rotate(db_session_factory, tenant_id, agent_id, fernet, active_turn=True)

    assert len(vault_writes) == 2, "the old tokens were written back to the vault"
    rows = await _live_rows(db_session_factory)
    initial_ids = {token.token_id for token in initial.tokens}
    current = {row.token_id for row in rows if row.superseded_at is None}
    assert current == initial_ids, "restore made the old rows current again"
    kept = {row.token_id for row in rows if row.superseded_at is not None}
    assert kept == {vault_writes[0].tokens[0].token_id}, "the delivered new token is kept"
    assert {row.router_session_id for row in rows} == {router_id}, "every row stays routed"
    assert await _route(
        db_session_factory, router_id, tenant_id, agent_id, fernet, "b", "two"
    ) == next(t.token for t in initial.tokens if t.installation_id == 772)


async def test_failed_idle_rotation_after_a_swap_closes_the_router_record_before_archiving(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bearers: list[str] = []
    real_create = router_sessions.create

    async def capture_create(*args: Any, **kwargs: Any) -> Any:
        record, bearer = await real_create(*args, **kwargs)
        bearers.append(bearer)
        return record, bearer

    monkeypatch.setattr(router_sessions, "create", capture_create)
    github, fernet = _GitHub(), _fernet()
    tenant_id, agent_id, router_id, _ = await _routed_session(
        db_session_factory, monkeypatch, github, fernet
    )
    bearer = bearers[0]
    async with db_session_factory() as session:
        assert await router_sessions.verify(session, bearer=bearer) is not None

    archived: list[str] = []
    closed_when_archived: list[bool] = []

    async def archive(_anthropic: Any, session_id: str, **_kwargs: Any) -> None:
        async with db_session_factory() as session:
            row = await session.get(GitHubRouterSession, router_id)
            assert row is not None
            closed_when_archived.append(row.closed_at is not None)
        archived.append(session_id)

    async def swap_then_fail(*_args: Any, on_mutation: Callable[[], None], **_kwargs: Any) -> None:
        on_mutation()  # MA already holds a new vault credential
        raise RuntimeError("vault write failed")

    monkeypatch.setattr(github_app_session, "add_app_credentials", swap_then_fail)
    monkeypatch.setattr(github_app_session, "archive_session", archive)

    with pytest.raises(RuntimeError, match="vault write failed"):
        await _rotate(db_session_factory, tenant_id, agent_id, fernet, active_turn=False)

    assert archived == [SESSION_ID]
    assert closed_when_archived == [True], "the record closes before the archive call"
    async with db_session_factory() as session:
        row = await session.get(GitHubRouterSession, router_id)
        assert row is not None
        assert row.closed_at is not None
        assert await router_sessions.verify(session, bearer=bearer) is None


# --- Lifecycle hooks ---------------------------------------------------------------


async def test_revoking_session_tokens_closes_the_record_before_github_is_called(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(db_session)
    record, bearer = await _record(db_session, tenant_id)
    fernet = _fernet()
    await _token_row(db_session, tenant_id, fernet, repo_id=1, router_session_id=record.id)
    await db_session.commit()

    def github_down(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(github_down)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await revoke_session_tokens(
                db_session_factory, client, session_id=SESSION_ID, fernet=fernet
            )

    async with db_session_factory() as session:
        assert await router_sessions.verify(session, bearer=bearer) is None, (
            "a failed GitHub revoke leaves the router bearer closed"
        )


async def test_closing_a_headless_session_closes_its_record_with_the_finish(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = await _tenant(db_session)
    _, bearer = await _record(db_session, tenant_id)
    await github_issued_tokens.register_headless_app_session(
        db_session, session_id=SESSION_ID, tenant_id=tenant_id, vault_id="vlt_1"
    )
    await db_session.commit()
    monkeypatch.setattr(github_app_session, "archive_app_vault", _no_call)

    # Without encryption the token revoke is skipped; the record still closes.
    await close_headless_app_session(
        None,  # type: ignore[arg-type]  # the vault archive is stubbed
        db_session_factory,
        session_id=SESSION_ID,
        fernet=None,
    )

    async with db_session_factory() as session:
        assert await router_sessions.verify(session, bearer=bearer) is None


async def test_a_headless_turn_start_renews_the_record(db_session: AsyncSession) -> None:
    tenant_id = await _tenant(db_session)
    soon = datetime.now(UTC) + timedelta(minutes=5)
    record, _ = await _record(db_session, tenant_id, expires_at=soon)
    await github_issued_tokens.register_headless_app_session(
        db_session, session_id=SESSION_ID, tenant_id=tenant_id, vault_id="vlt_1"
    )
    started = datetime.now(UTC)

    assert await github_issued_tokens.touch_unmapped_app_session(db_session, session_id=SESSION_ID)

    renewed = await router_sessions.get(db_session, router_session_id=record.id)
    assert renewed is not None
    assert renewed.expires_at >= started + router_sessions.GITHUB_ROUTER_SESSION_TTL


async def test_a_finished_headless_session_does_not_renew_the_record(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _tenant(db_session)
    soon = datetime.now(UTC) + timedelta(minutes=5)
    record, _ = await _record(db_session, tenant_id, expires_at=soon)
    await github_issued_tokens.register_headless_app_session(
        db_session, session_id=SESSION_ID, tenant_id=tenant_id, vault_id="vlt_1"
    )
    await github_issued_tokens.finish_headless_app_session(db_session, session_id=SESSION_ID)

    assert not await github_issued_tokens.touch_unmapped_app_session(
        db_session, session_id=SESSION_ID
    )

    unchanged = await router_sessions.get(db_session, router_session_id=record.id)
    assert unchanged is not None and unchanged.expires_at == soon
