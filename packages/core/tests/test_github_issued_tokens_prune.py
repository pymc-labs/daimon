"""Deleting closed GitHub token rows must never touch a token that may be live.

The policy: a `revoked` row goes 7 days after `expires_at`, a never-stored `pending`
row 1 hour after it, and `stored` and `delivered` rows never go. The model in
formal/github_issued_tokens_prune/ explains why a late store cannot lose a live token;
these tests replay its cases against PostgreSQL, including two real connections for the
row-lock behaviour. Table rows are written directly so each test controls status and age.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.core._models import (
    GitHubIssuedToken,
    SecurityAuditEvent,
    Tenant,
    TenantGitHubRepo,
)
from daimon.core.config import GithubAppSettings
from daimon.core.github_app_session import prepare_app_access
from daimon.core.github_credentials import build_multifernet
from daimon.core.github_issued_token_sweep import sweep_closed_github_issued_tokens
from daimon.core.github_requester_access import PermissionCache
from daimon.core.security_audit import GITHUB_TOKEN_MINT
from daimon.core.stores import github_access, github_app_installations, github_issued_tokens
from daimon.core.stores.github_issued_tokens import (
    PENDING_GRACE,
    REVOKED_RETENTION,
    prune_closed_tokens,
    store_token,
)
from daimon.core.stores.security_audit import append_github_token_event
from pydantic import SecretStr
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

Status = Literal["pending", "stored", "delivered", "revoked"]
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
SWEEP = "daimon.core.github_issued_token_sweep"


def _row(
    tenant_id: uuid.UUID,
    status: Status,
    *,
    expired_ago: timedelta,
    session_id: str = "sesn_1",
) -> GitHubIssuedToken:
    return GitHubIssuedToken(
        token_id=uuid.uuid4(),
        tenant_id=tenant_id,
        agent_id=uuid.uuid4(),
        session_id=session_id,
        installation_id=77,
        repo_ids=[1],
        permissions={"contents": "read"},
        grant_versions={"grant:1": 1, "authorization:1": 1},
        expires_at=NOW - expired_ago,
        status=status,
        revoke_attempts=0,
        encrypted_token=None if status == "pending" else b"sealed",
    )


async def _tenant(session: AsyncSession) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await session.flush()
    return tenant_id


async def _remaining(session: AsyncSession) -> set[uuid.UUID]:
    return set(await session.scalars(select(GitHubIssuedToken.token_id)))


@pytest.mark.asyncio
async def test_prune_deletes_only_old_revoked_and_pending_rows(db_session: AsyncSession) -> None:
    tenant_id = await _tenant(db_session)
    day, hour = timedelta(days=1), timedelta(hours=1)
    doomed = [
        _row(tenant_id, "revoked", expired_ago=REVOKED_RETENTION + hour),
        _row(tenant_id, "revoked", expired_ago=30 * day),
        _row(tenant_id, "pending", expired_ago=PENDING_GRACE + timedelta(minutes=1)),
        _row(tenant_id, "pending", expired_ago=3 * day),
    ]
    kept = [
        _row(tenant_id, "revoked", expired_ago=REVOKED_RETENTION - hour),
        _row(tenant_id, "revoked", expired_ago=-hour),  # not yet expired
        _row(tenant_id, "pending", expired_ago=PENDING_GRACE - timedelta(minutes=1)),
        _row(tenant_id, "pending", expired_ago=-hour),
        # A token that may still be live, or its session's record, goes at no age.
        _row(tenant_id, "stored", expired_ago=-hour),
        _row(tenant_id, "stored", expired_ago=90 * day, session_id="pending:abc"),
        _row(tenant_id, "delivered", expired_ago=-hour),
        _row(tenant_id, "delivered", expired_ago=90 * day),
    ]
    db_session.add_all(doomed + kept)
    await db_session.flush()

    deleted = await prune_closed_tokens(db_session, now=NOW, batch=100)

    assert deleted == len(doomed)
    assert await _remaining(db_session) == {row.token_id for row in kept}


@pytest.mark.asyncio
async def test_prune_cutoffs_are_strict_and_independent_of_status(db_session: AsyncSession) -> None:
    tenant_id = await _tenant(db_session)
    at_cutoff = [
        _row(tenant_id, "revoked", expired_ago=REVOKED_RETENTION),
        _row(tenant_id, "pending", expired_ago=PENDING_GRACE),
    ]
    # A pending row is not held to the revoked row's 7 days, nor a revoked row to 1 hour.
    revoked_past_grace = _row(tenant_id, "revoked", expired_ago=PENDING_GRACE * 2)
    db_session.add_all([*at_cutoff, revoked_past_grace])
    await db_session.flush()

    assert await prune_closed_tokens(db_session, now=NOW, batch=100) == 0
    assert await _remaining(db_session) == {
        row.token_id for row in [*at_cutoff, revoked_past_grace]
    }
    assert await prune_closed_tokens(db_session, now=NOW + timedelta(seconds=1), batch=100) == 2
    assert await _remaining(db_session) == {revoked_past_grace.token_id}


@pytest.mark.asyncio
async def test_prune_leaves_the_security_audit_log_alone(db_session: AsyncSession) -> None:
    tenant_id = await _tenant(db_session)
    row = _row(tenant_id, "revoked", expired_ago=REVOKED_RETENTION + timedelta(days=1))
    db_session.add(row)
    await db_session.flush()
    event = await append_github_token_event(
        db_session,
        tenant_id=tenant_id,
        agent_id=row.agent_id,
        account_id=None,
        kind=GITHUB_TOKEN_MINT,
        outcome="allowed",
        reason="issued",
        token_id=row.token_id,
        session_id="sesn_1",
        installation_id=77,
        repo_ids=[1],
        permissions={"contents": "read"},
        expires_at=row.expires_at,
    )
    assert event is not None

    assert await prune_closed_tokens(db_session, now=NOW, batch=100) == 1

    assert await db_session.scalar(select(func.count()).select_from(SecurityAuditEvent)) == 1
    assert await db_session.scalar(select(SecurityAuditEvent.github_token_id)) == row.token_id


@pytest.mark.asyncio
async def test_prune_takes_the_oldest_rows_first_and_respects_the_batch(
    db_session: AsyncSession,
) -> None:
    tenant_id = await _tenant(db_session)
    rows = [
        _row(tenant_id, "revoked", expired_ago=REVOKED_RETENTION + timedelta(days=age))
        for age in (1, 5, 3, 2, 4)
    ]
    db_session.add_all(rows)
    await db_session.flush()
    by_age = sorted(rows, key=lambda row: row.expires_at)

    assert await prune_closed_tokens(db_session, now=NOW, batch=2) == 2
    assert await _remaining(db_session) == {row.token_id for row in by_age[2:]}
    assert await prune_closed_tokens(db_session, now=NOW, batch=2) == 2
    assert await prune_closed_tokens(db_session, now=NOW, batch=2) == 1
    assert await prune_closed_tokens(db_session, now=NOW, batch=2) == 0
    with pytest.raises(ValueError, match="batch"):
        await prune_closed_tokens(db_session, now=NOW, batch=0)


@pytest.mark.asyncio
async def test_sweep_stops_at_the_cap_and_the_next_run_continues(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    db_session.add_all(
        _row(tenant_id, "revoked", expired_ago=REVOKED_RETENTION + timedelta(days=1))
        for _ in range(7)
    )
    live = _row(tenant_id, "delivered", expired_ago=timedelta(days=60))
    db_session.add(live)
    await db_session.commit()

    capped = await sweep_closed_github_issued_tokens(
        db_session_factory, now=NOW, batch=2, max_batches=3
    )
    assert capped == 6
    assert await db_session.scalar(select(func.count()).select_from(GitHubIssuedToken)) == 2

    assert (
        await sweep_closed_github_issued_tokens(db_session_factory, now=NOW, batch=2, max_batches=3)
        == 1
    )
    assert await _remaining(db_session) == {live.token_id}


@pytest.mark.asyncio
async def test_sweep_stops_after_a_short_batch(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = await _tenant(db_session)
    db_session.add_all(_row(tenant_id, "pending", expired_ago=timedelta(days=1)) for _ in range(3))
    await db_session.commit()
    calls: list[int] = []
    real = prune_closed_tokens

    async def spy(session: AsyncSession, *, now: datetime, batch: int) -> int:
        deleted = await real(session, now=now, batch=batch)
        calls.append(deleted)
        return deleted

    monkeypatch.setattr(f"{SWEEP}.prune_closed_tokens", spy)

    assert await sweep_closed_github_issued_tokens(db_session_factory, now=NOW, batch=5) == 3
    assert calls == [3], "a short batch ends the run without another scan"


@pytest.mark.asyncio
async def test_sweep_commits_each_batch(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = await _tenant(db_session)
    db_session.add_all(
        _row(tenant_id, "revoked", expired_ago=REVOKED_RETENTION + timedelta(days=1))
        for _ in range(4)
    )
    await db_session.commit()
    real = prune_closed_tokens
    calls = 0

    async def fail_second(session: AsyncSession, *, now: datetime, batch: int) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second batch fails")
        return await real(session, now=now, batch=batch)

    monkeypatch.setattr(f"{SWEEP}.prune_closed_tokens", fail_second)

    with pytest.raises(RuntimeError, match="second batch"):
        await sweep_closed_github_issued_tokens(db_session_factory, now=NOW, batch=2)

    assert await db_session.scalar(select(func.count()).select_from(GitHubIssuedToken)) == 2


@pytest.mark.asyncio
async def test_prune_skips_a_locked_row_and_never_deletes_one_stored_meanwhile(
    db_engine: AsyncEngine, db_clean: None
) -> None:
    """Two real connections: FOR UPDATE SKIP LOCKED skips, it does not wait or delete."""
    sessions = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sessions.begin() as session:
        tenant_id = await _tenant(session)
        locked = _row(tenant_id, "pending", expired_ago=timedelta(days=1))
        free = _row(tenant_id, "pending", expired_ago=timedelta(days=1))
        session.add_all([locked, free])
    fernet = build_multifernet((Fernet.generate_key().decode(),))

    async with sessions() as minter, sessions() as sweeper:
        # The late minter takes the row lock inside store_token and has not committed.
        await store_token(minter, token_id=locked.token_id, token="ghs_late", fernet=fernet)
        deleted = await asyncio.wait_for(prune_closed_tokens(sweeper, now=NOW, batch=10), timeout=5)
        await sweeper.commit()
        assert deleted == 1, "the unlocked row went; the locked one was skipped, not waited on"
        await minter.commit()

    async with sessions() as session:
        assert await _remaining(session) == {locked.token_id}
        stored = await session.get(GitHubIssuedToken, locked.token_id)
        assert stored is not None and stored.status == "stored"
        # The next run sees the committed `stored` status and still leaves it.
        assert await prune_closed_tokens(session, now=NOW, batch=10) == 0


@pytest.mark.asyncio
async def test_prune_takes_a_row_it_skipped_once_the_lock_is_gone(
    db_engine: AsyncEngine, db_clean: None
) -> None:
    sessions = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sessions.begin() as session:
        tenant_id = await _tenant(session)
        row = _row(tenant_id, "revoked", expired_ago=REVOKED_RETENTION + timedelta(days=1))
        session.add(row)
    async with sessions() as holder, sessions() as sweeper:
        await github_issued_tokens.record_revoke_attempt(holder, token_id=row.token_id)
        assert await asyncio.wait_for(prune_closed_tokens(sweeper, now=NOW, batch=10), 5) == 0
        await sweeper.commit()
        await holder.commit()
        assert await prune_closed_tokens(sweeper, now=NOW, batch=10) == 1


@pytest.mark.asyncio
async def test_sweep_ends_a_delete_stuck_behind_a_table_lock(
    db_engine: AsyncEngine, db_clean: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with sessions.begin() as session:
        tenant_id = await _tenant(session)
        session.add(_row(tenant_id, "revoked", expired_ago=REVOKED_RETENTION + timedelta(days=1)))
    monkeypatch.setattr(f"{SWEEP}.STATEMENT_TIMEOUT_MS", 200)

    async with db_engine.connect() as blocker:
        await blocker.execute(text("LOCK TABLE github_issued_tokens IN ACCESS EXCLUSIVE MODE"))
        started = asyncio.get_running_loop().time()
        with pytest.raises(DBAPIError, match="statement timeout"):
            await sweep_closed_github_issued_tokens(sessions, now=NOW)
        assert asyncio.get_running_loop().time() - started < 5
        await blocker.rollback()


def _plan_nodes(node: dict[str, Any]) -> list[str]:
    found = [node["Node Type"]]
    for child in node.get("Plans", []):
        found.extend(_plan_nodes(child))
    return found


@pytest.mark.asyncio
async def test_prune_scans_the_table_once_and_locks_in_a_single_subquery_pass(
    db_session: AsyncSession,
) -> None:
    """Production has about 22,000 closed rows and no index covers this predicate.

    On 22,000 and on 60,000 rows the whole statement took 3 and 9 ms, one sequential scan
    every 10 minutes. The existing reads that the live-session index serves run about 1.8
    times a second. An index on `expires_at` would cover 99% of the table and be updated on
    every revoke, so none is added. What this test pins is that the locking subquery runs
    once (MATERIALIZED), which makes the rows it locks exactly the rows the DELETE deletes.
    """
    tenant_id = await _tenant(db_session)
    await db_session.execute(
        text("""
        INSERT INTO github_issued_tokens
            (tenant_id, agent_id, session_id, installation_id, repo_ids, permissions,
             grant_versions, expires_at, status, revoked_at)
        SELECT :tenant, gen_random_uuid(), 'sesn_' || (n % 120), 77, ARRAY[1]::bigint[],
               '{}'::jsonb, '{}'::jsonb, now() - interval '8 days' - n * interval '1 second',
               'revoked', now()
        FROM generate_series(1, 20000) AS n
        """),
        {"tenant": tenant_id},
    )
    await db_session.execute(text("ANALYZE github_issued_tokens"))
    connection = await db_session.connection()
    sent: list[tuple[str, Any]] = []

    def record(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        if statement.startswith("WITH victims"):
            sent.append((statement, parameters))

    event.listen(connection.sync_connection, "before_cursor_execute", record)
    try:
        assert await prune_closed_tokens(db_session, now=datetime.now(UTC), batch=2000) == 2000
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", record)
    statement, parameters = sent[0]
    result = await connection.exec_driver_sql(f"EXPLAIN (FORMAT JSON) {statement}", parameters)
    raw = result.scalar_one()
    plan = (json.loads(raw) if isinstance(raw, str) else raw)[0]["Plan"]
    nodes = _plan_nodes(plan)

    assert nodes.count("LockRows") == 1
    assert nodes.count("Limit") == 1
    assert "CTE Scan" in nodes, "the delete reads the materialized, locked ids"


async def _grant_one_repo(
    sessions: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> None:
    async with sessions.begin() as session:
        session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
        await session.flush()
        await github_app_installations.upsert(
            session, installation_id=771, account_login="example", repo_full_names=["example/repo"]
        )
        session.add(
            TenantGitHubRepo(
                tenant_id=tenant_id,
                repo_id=991,
                owner_id=1,
                installation_id=771,
                repo_full_name="example/repo",
                max_access="write",
                authorized_by_github_user_id=2,
                status="active",
                version=1,
            )
        )
        await session.flush()
        await github_access.stage_grant(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=991,
            baseline_access="read",
            ceiling_access="read",
            granted_by_account_id=None,
        )


@pytest.mark.asyncio
async def test_late_store_after_its_pending_row_was_pruned_revokes_the_github_token(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model's NoGraceMisses trace, forced past the 1 hour grace.

    The minter's GitHub call outlasts the pending row's grace, a sweep deletes the row,
    and the late store finds nothing. The caller must still delete the token at GitHub.
    With the real grace this takes a minter stalled for over two hours.
    """
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    await _grant_one_repo(db_session_factory, tenant_id, agent_id)

    async def mint_after_prune(*args: object, **kwargs: object) -> str:
        async with db_session_factory.begin() as session:
            deleted = await prune_closed_tokens(
                session, now=datetime.now(UTC) + timedelta(hours=3), batch=10
            )
        assert deleted == 1
        return "late-token"

    monkeypatch.setattr(
        "daimon.core.github_app_session.build_app_jwt",
        lambda *a, **k: "jwt",  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    )
    monkeypatch.setattr("daimon.core.github_app_session.mint_installation_token", mint_after_prune)
    deleted_at_github: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        deleted_at_github.append(request.headers["Authorization"])
        return httpx.Response(204)

    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="token is not pending"):
            await prepare_app_access(
                db_session_factory,
                client,
                tenant_id=tenant_id,
                agent_id=agent_id,
                account_id=None,
                provisional_session_id="pending-pruned",
                config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
                fernet=fernet,
                cache=PermissionCache(),
            )

    assert deleted_at_github == ["Bearer late-token"]
    async with db_session_factory() as session:
        assert await _remaining(session) == set(), "the row stays pruned; nothing holds the token"
