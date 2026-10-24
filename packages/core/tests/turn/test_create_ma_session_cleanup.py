"""`create_ma_session` archives the upstream session it created when it fails
before it can hand the session to its caller.

`create_session` archives its own session on any failure up to its return. After
that return `create_ma_session` still awaits the personal-memory lookup and builds
the snapshot; a cancel (turn Stop, an expiring ceiling) or a database
error there unwinds with no `CreatedSession` ever reaching the caller, so no
caller cleanup runs and no row names the session. Without a cleanup in
`create_ma_session` the upstream session stays live forever.

Model: `formal/session_create_cleanup/`. Each test blocks or fails the lookup
(`get_personal_memory_store_id`, patched in the prepare module) after the fake
upstream has created the session, then reads which sessions the fake saw archived.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from daimon.core._models import ThreadSession
from daimon.core.stores.thread_sessions import create_thread_session, mark_dead
from daimon.core.turn import prepare as prepare_module
from daimon.core.turn import run as run_module
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.prepare import CreatedSession, create_ma_session
from daimon.testing.db import build_test_engine
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.testing.drivers.anthropic.ma import MARouter
from mux.testing.drivers.anthropic.ma_models import ma_session
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from .test_frozen_root import root_turn as root_turn
from .test_root_controls import select_controls
from .test_run_prepared_turn import (  # pyright: ignore[reportPrivateUsage]
    _admission,
    _deps,
    _prepared_turn,
    _router,
)

from daimon.testing.factories import (  # isort: skip
    make_account,
    make_tenant,
    make_thread_session,
)

LOOKUP = "get_personal_memory_store_id"


class _LookupBoom(Exception):
    """Stands in for a database error during the personal-memory lookup."""


class _Upstream:
    """The fake Managed Agents API plus a record of which sessions it archived."""

    def __init__(self, *, dead_session_ids: set[str] | None = None, archive_status: int = 200):
        self.session_bodies: list[dict[str, object]] = []
        self.archived: list[str] = []
        self.router: MARouter = _router(
            session_bodies=self.session_bodies, dead_session_ids=dead_session_ids or set()
        )

        def archive(_request: httpx.Request, match: re.Match[str]) -> httpx.Response:
            self.archived.append(match.group("sid"))
            if archive_status != 200:
                return httpx.Response(
                    archive_status, json={"type": "error", "error": {"type": "api_error"}}
                )
            return httpx.Response(
                200, json=ma_session(id=match.group("sid")).model_dump(mode="json")
            )

        self.router.add("POST", r"/v1/sessions/(?P<sid>[^/]+)/archive", archive)

    def deps(self, sessionmaker: async_sessionmaker[AsyncSession]) -> TurnDeps:
        return _deps(sessionmaker=sessionmaker, router=self.router)


def _patch_lookup(
    monkeypatch: pytest.MonkeyPatch, body: Callable[[], Awaitable[Any]]
) -> asyncio.Event:
    """Replace the lookup; the returned event is set when `create_ma_session` reaches it."""
    reached = asyncio.Event()

    async def lookup(*_args: object, **_kwargs: object) -> Any:
        reached.set()
        return await body()

    monkeypatch.setattr(prepare_module, LOOKUP, lookup)
    return reached


async def _never() -> None:
    await asyncio.Event().wait()


async def _fail() -> None:
    raise _LookupBoom("connection lost during the personal-memory lookup")


async def _run_until_ceiling_expires(reached: asyncio.Event, work: Awaitable[Any]) -> Any:
    """Run `work` under a real timeout (the machinery behind `asyncio.wait_for`) that
    expires as soon as `work` reaches the blocked lookup, however slow the machine."""
    ceilings: list[asyncio.Timeout] = []

    async def bounded() -> Any:
        async with asyncio.timeout(None) as ceiling:
            ceilings.append(ceiling)
            return await work

    task = asyncio.create_task(bounded())
    await asyncio.wait_for(reached.wait(), timeout=10)
    ceilings[0].reschedule(asyncio.get_running_loop().time() + 0.05)
    return await task


async def _create(deps: TurnDeps, tenant_id: uuid.UUID, account_id: uuid.UUID) -> CreatedSession:
    agent = ma_agent(id="ag_1", tenant_id=tenant_id)
    env = ma_environment(id="env_1", tenant_id=tenant_id)
    admission = _admission(account_id=account_id, agent=agent, env=env)
    return await create_ma_session(deps, admission, tenant_id=tenant_id, platform="discord")


async def test_cancel_during_the_personal_memory_lookup_archives_the_created_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    upstream = _Upstream()
    reached = _patch_lookup(monkeypatch, _never)

    task = asyncio.create_task(_create(upstream.deps(db_session_factory), tenant.id, account.id))
    await asyncio.wait_for(reached.wait(), timeout=10)
    assert len(upstream.session_bodies) == 1, "the session exists upstream before the lookup"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert upstream.archived == ["sess_1"], (
        "a cancel after sessions.create and before CreatedSession is returned leaves "
        "a live upstream session no row names"
    )


async def test_ceiling_expiring_during_the_lookup_archives_the_created_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    upstream = _Upstream()
    reached = _patch_lookup(monkeypatch, _never)

    with pytest.raises(TimeoutError):
        await _run_until_ceiling_expires(
            reached, _create(upstream.deps(db_session_factory), tenant.id, account.id)
        )

    assert upstream.archived == ["sess_1"]


async def test_database_error_during_the_lookup_archives_the_created_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    upstream = _Upstream()
    _patch_lookup(monkeypatch, _fail)

    with pytest.raises(_LookupBoom):
        await _create(upstream.deps(db_session_factory), tenant.id, account.id)

    assert upstream.archived == ["sess_1"]


async def test_a_failing_archive_does_not_mask_the_lookup_error(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    upstream = _Upstream(archive_status=500)
    _patch_lookup(monkeypatch, _fail)

    with pytest.raises(_LookupBoom):
        await _create(upstream.deps(db_session_factory), tenant.id, account.id)

    assert upstream.archived == ["sess_1"], "the archive was attempted; its failure is logged only"


async def test_a_successful_create_archives_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    upstream = _Upstream()

    created = await _create(upstream.deps(db_session_factory), tenant.id, account.id)

    assert created.ma_session_id == "sess_1"
    assert upstream.archived == []


@asynccontextmanager
async def _committing_deps(
    db_schema: str, upstream: _Upstream
) -> AsyncIterator[tuple[TurnDeps, async_sessionmaker[AsyncSession]]]:
    """Deps on a NullPool engine: recovery commits and takes a connection-scoped
    advisory lock, which the shared-connection fixture would hide."""
    engine = build_test_engine(
        os.environ["DAIMON_DATABASE__TEST_URL"], db_schema, poolclass=NullPool
    )
    try:
        sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
        yield dataclasses.replace(upstream.deps(sessionmaker)), sessionmaker
    finally:
        await engine.dispose()


async def _recovery_rows(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, thread_id: str
) -> list[ThreadSession]:
    async with sessionmaker() as s:
        result = await s.execute(
            select(ThreadSession).where(
                ThreadSession.tenant_id == tenant_id, ThreadSession.thread_id == thread_id
            )
        )
        return list(result.scalars().all())


async def _replace_dead_session(
    deps: TurnDeps,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    mapping_id: uuid.UUID,
    thread_id: str,
) -> run_module._Replacement:  # pyright: ignore[reportPrivateUsage]
    agent = ma_agent(id="ag_1", tenant_id=tenant_id)
    env = ma_environment(id="env_1", tenant_id=tenant_id)
    admission = _admission(account_id=account_id, agent=agent, env=env)
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant_id,
        external_user_id="user-1",
        ma_session_id="sess_old",
        mapping_id=mapping_id,
        session_account_id=account_id,
    )
    return await run_module._replace_dead_session_locked(  # pyright: ignore[reportPrivateUsage]
        deps,
        prepared,
        tenant_id=tenant_id,
        platform="discord",
        thread_id=thread_id,
        dead_session_id="sess_old",
        dead_mapping_id=mapping_id,
    )


async def test_ceiling_during_recovery_lookup_archives_the_replacement_session(
    db_session: AsyncSession,
    db_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dead-session recovery has `created is None` when the cancel lands inside
    `create_ma_session`, so its own orphan cleanup skips the archive."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-lookup-ceiling",
        ma_session_id="sess_old",
    )
    await db_session.commit()
    upstream = _Upstream(dead_session_ids={"sess_old"})
    reached = _patch_lookup(monkeypatch, _never)

    async with _committing_deps(db_schema, upstream) as (deps, sessionmaker):
        with pytest.raises(TimeoutError):
            await _run_until_ceiling_expires(
                reached,
                _replace_dead_session(
                    deps,
                    tenant_id=tenant.id,
                    account_id=account.id,
                    mapping_id=row.id,
                    thread_id="thread-lookup-ceiling",
                ),
            )
        rows = await _recovery_rows(sessionmaker, tenant.id, "thread-lookup-ceiling")

    assert [r.ma_session_id for r in rows] == ["sess_old"], "the recovery rolled back"
    assert upstream.archived == ["sess_1"]


async def test_database_error_during_recovery_lookup_archives_the_replacement_session(
    db_session: AsyncSession,
    db_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-lookup-error",
        ma_session_id="sess_old",
    )
    await db_session.commit()
    upstream = _Upstream(dead_session_ids={"sess_old"})
    _patch_lookup(monkeypatch, _fail)

    async with _committing_deps(db_schema, upstream) as (deps, sessionmaker):
        with pytest.raises(_LookupBoom):
            await _replace_dead_session(
                deps,
                tenant_id=tenant.id,
                account_id=account.id,
                mapping_id=row.id,
                thread_id="thread-lookup-error",
            )
        rows = await _recovery_rows(sessionmaker, tenant.id, "thread-lookup-error")

    assert [r.ma_session_id for r in rows] == ["sess_old"]
    assert upstream.archived == ["sess_1"]


async def test_successful_recovery_archives_nothing(
    db_session: AsyncSession,
    db_schema: str,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-recovery-ok",
        ma_session_id="sess_old",
    )
    await db_session.commit()
    upstream = _Upstream(dead_session_ids={"sess_old"})

    async with _committing_deps(db_schema, upstream) as (deps, sessionmaker):
        replacement = await _replace_dead_session(
            deps,
            tenant_id=tenant.id,
            account_id=account.id,
            mapping_id=row.id,
            thread_id="thread-recovery-ok",
        )
        rows = await _recovery_rows(sessionmaker, tenant.id, "thread-recovery-ok")

    assert not replacement.adopted and replacement.ma_session_id == "sess_1"
    assert sorted(r.ma_session_id for r in rows) == ["sess_1", "sess_old"]
    assert upstream.archived == []


async def test_recovery_adopting_another_turns_replacement_archives_nothing(
    db_session: AsyncSession,
    db_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adopted session is a live row another turn created. It never passes
    through `create_ma_session`, so no cleanup may touch it."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    dead_row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-adopt",
        ma_session_id="sess_old",
    )
    await mark_dead(db_session, id=dead_row.id)
    await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-adopt",
        account_id=account.id,
        ma_session_id="sess_other_turn",
        created_at=datetime.now(UTC),
    )
    await db_session.commit()
    upstream = _Upstream(dead_session_ids={"sess_old"})
    # An adopt that reached create would also reach the lookup: make that loud.
    _patch_lookup(monkeypatch, _fail)

    async with _committing_deps(db_schema, upstream) as (deps, sessionmaker):
        replacement = await _replace_dead_session(
            deps,
            tenant_id=tenant.id,
            account_id=account.id,
            mapping_id=dead_row.id,
            thread_id="thread-adopt",
        )
        rows = await _recovery_rows(sessionmaker, tenant.id, "thread-adopt")

    assert replacement.adopted and replacement.ma_session_id == "sess_other_turn"
    assert upstream.session_bodies == [], "adopting creates no upstream session"
    assert upstream.archived == []
    assert sorted(r.ma_session_id for r in rows) == ["sess_old", "sess_other_turn"]


async def _prepared_root_create(root_turn, monkeypatch: pytest.MonkeyPatch):
    """A controlled-root preparation whose session create goes through
    `persistence.mutate("session_create")`; returns the request, the thing that
    creates, and the paths the fake saw archived."""
    from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT, MA_METADATA_KEY_TENANT
    from daimon.core.turn.runtime_manifest import run_controlled_preparation
    from mux.testing.drivers.anthropic.ma import make_fake_memory_store_handler
    from mux.testing.drivers.anthropic.ma_transport import ScriptedReply

    request = await select_controls(root_turn)
    transport = root_turn[-1]
    native = ma_session(
        id="fresh-controlled-root",
        agent_id=request.admission.agent.id,
        model=request.admission.agent.model.id,
        environment_id=request.admission.environment.id,
        metadata={
            MA_METADATA_KEY_TENANT: str(request.tenant_id),
            MA_METADATA_KEY_ACCOUNT: str(request.admission.account_id),
        },
    ).model_dump(mode="json")
    native["budget"] = {"type": "limit", "max_list_cost": {"amount": "3", "currency": "USD"}}
    transport.queue(ScriptedReply("POST", "/v1/sessions", httpx.Response(200, json=native)))
    transport.router.add(
        "GET",
        r"/v1/sessions/fresh-controlled-root",
        lambda _r, _m: httpx.Response(200, json=native),
    )
    transport.router.add(
        "POST",
        r"/v1/sessions/fresh-controlled-root/archive",
        lambda _r, _m: httpx.Response(200, json=native),
    )
    memory = make_fake_memory_store_handler()
    transport.router.add("POST", r"/v1/memory_stores", lambda r, _m: memory(r))

    async def create() -> CreatedSession:
        return await run_controlled_preparation(
            request,
            create_ma_session(
                request.deps,
                request.admission,
                tenant_id=request.tenant_id,
                preparation=request,
                platform="discord",
            ),
        )

    def archive_posts() -> list[str]:
        return [
            r.path for r in transport.requests if r.method == "POST" and r.path.endswith("/archive")
        ]

    return request, create, archive_posts


async def test_prepared_create_archives_the_session_when_the_lookup_fails(
    root_turn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded `session_create` path binds the archive to the preparation's scope."""
    _, create, archive_posts = await _prepared_root_create(root_turn, monkeypatch)
    _patch_lookup(monkeypatch, _fail)

    with pytest.raises(_LookupBoom):
        await create()

    assert archive_posts() == ["/v1/sessions/fresh-controlled-root/archive"]


async def test_prepared_create_archives_a_replayed_session_no_row_names(
    root_turn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded create is replayed by a later call under the same operation key and hands back
    the same session. With no row naming it (the first attempt died before its insert) the
    replaying call owns it, so its failed lookup archives it."""
    _, create, archive_posts = await _prepared_root_create(root_turn, monkeypatch)
    first = await create()
    assert archive_posts() == [], "a successful create archives nothing"
    _patch_lookup(monkeypatch, _fail)

    with pytest.raises(_LookupBoom):
        await create()

    assert archive_posts() == [f"/v1/sessions/{first.ma_session_id}/archive"]


async def _commit_row_naming(root_turn, request, ma_session_id: str) -> None:
    deps = root_turn[0]
    async with deps.sessionmaker.begin() as db:
        await create_thread_session(
            db,
            tenant_id=request.tenant_id,
            platform=request.platform,
            thread_id=request.thread_id,
            account_id=request.admission.account_id,
            ma_session_id=ma_session_id,
        )


async def test_prepared_create_keeps_a_replayed_session_a_committed_row_names(
    root_turn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first attempt's mapping committed; a replay hands back the same session and its
    lookup fails. The session is the live conversation of that row: it must not be archived."""
    request, create, archive_posts = await _prepared_root_create(root_turn, monkeypatch)
    first = await create()
    await _commit_row_naming(root_turn, request, first.ma_session_id)
    _patch_lookup(monkeypatch, _fail)

    with pytest.raises(_LookupBoom):
        await create()

    assert archive_posts() == []


async def test_prepared_create_keeps_a_replayed_session_when_the_row_read_fails(
    root_turn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If it cannot be shown that no row names a replayed session, it is left alone."""
    _, create, archive_posts = await _prepared_root_create(root_turn, monkeypatch)
    await create()
    _patch_lookup(monkeypatch, _fail)

    async def unreadable(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise _LookupBoom("connection lost during the row read")

    monkeypatch.setattr(prepare_module, "thread_ids_for_sessions", unreadable)

    with pytest.raises(_LookupBoom, match="personal-memory lookup"):
        await create()

    assert archive_posts() == []
