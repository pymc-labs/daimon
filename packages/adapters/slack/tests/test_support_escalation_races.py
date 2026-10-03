"""Action-time races in Slack "Ask a human" — real Postgres, separate connections.

The source decision and the credit write are one serialized step with policy
edits (ledger lock, then the tenant policy lock, then a fresh policy read), and
the destination is decided from a fresh policy read after the permalink round
trip, right before the post. Each test drives the real submission handler on
its own connection while another connection holds a lock or commits an edit.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from aioresponses import CallbackResult
from cryptography.fernet import Fernet
from daimon.adapters.slack import support_escalation as slack_support
from daimon.adapters.slack.support_escalation import (
    NOT_ALLOWED,
    evaluate_support_submission,
    run_support_submission,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.config import SupportSettings
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores.access_policy import (
    load_access_policy,
    policy_write_transaction,
    set_access_policy,
)
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.core.stores.tenants import get_tenant
from daimon.core.support_escalation import RECORDED_UNDELIVERED, received_text
from daimon.testing.db import build_test_engine
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from .harness import build_slack_runtime
from .test_support_escalation import (
    _CHANNEL,
    _ESC_CHANNEL,
    _PERMALINK,
    _PERMALINK_PATTERN,
    _TEAM,
    _USER,
    _ephemeral_texts,
    _posts,
    _submit_payload,
)

pytestmark = pytest.mark.asyncio

_OPS_TEAM = "T_OPS"


@pytest_asyncio.fixture
async def race_engine(db_engine: AsyncEngine, db_schema: str) -> AsyncIterator[AsyncEngine]:
    engine = build_test_engine(
        db_engine.url.render_as_string(hide_password=False), db_schema, poolclass=NullPool
    )
    try:
        yield engine
    finally:
        await engine.dispose()


async def _seed_workspace(session: AsyncSession, *, team_id: str, fernet_key: str) -> uuid.UUID:
    fernet = build_multifernet((fernet_key,))
    tenant = await make_tenant(session, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        session, team_id=team_id, encrypted_token=encrypt_token(fernet, f"xoxb-{team_id}")
    )
    return tenant.id


async def _seed(
    session: AsyncSession, *, policy_row: bool, with_account: bool = False
) -> tuple[uuid.UUID, uuid.UUID | None, str]:
    key = Fernet.generate_key().decode()
    tenant_id = await _seed_workspace(session, team_id=_TEAM, fernet_key=key)
    account_id: uuid.UUID | None = None
    if with_account:
        row = await _tenant_row(session, tenant_id)
        account = await make_account(session, tenant=row)
        await make_platform_principal(
            session, platform="slack", external_id=_USER, tenant=row, account=account
        )
        account_id = account.id
    if policy_row:
        await set_access_policy(session, tenant_id=tenant_id, policy=TenantAccessPolicy())
    await session.commit()
    return tenant_id, account_id, key


async def _tenant_row(session: AsyncSession, tenant_id: uuid.UUID) -> TenantRow:
    row = await get_tenant(session, tenant_id)
    assert row is not None
    return row


def _runtime(key: str, factory: async_sessionmaker[AsyncSession], **support: Any) -> Any:
    settings = MagicMock()
    settings.support = SupportSettings(slack_escalation_channel_id=_ESC_CHANNEL, **support)
    return build_slack_runtime(key, factory, settings=settings)


async def _pid(session: AsyncSession) -> int:
    return int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())


@pytest_asyncio.fixture
async def recorder_pid(monkeypatch: pytest.MonkeyPatch) -> asyncio.Future[int]:
    """Observe the real recorder's connection before it takes any locks."""
    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    record = slack_support.record_escalation_once

    async def recording(session: AsyncSession, **kwargs: Any) -> Any:
        pid.set_result(await _pid(session))
        return await record(session, **kwargs)

    monkeypatch.setattr(slack_support, "record_escalation_once", recording)
    return pid


async def _blocked_by(engine: AsyncEngine, waiter_pid: int, holder_pid: int) -> None:
    """Wait for this backend to block on this holder, excluding other workers."""
    async with engine.connect() as probe:
        for _ in range(200):
            pid = (
                await probe.execute(
                    text(
                        "SELECT pid FROM pg_stat_activity"
                        " WHERE pid = :waiter AND :holder = ANY(pg_blocking_pids(pid))"
                    ),
                    {"waiter": waiter_pid, "holder": holder_pid},
                )
            ).scalar_one_or_none()
            if pid is not None:
                return
            await probe.rollback()
            await asyncio.sleep(0.025)
    raise AssertionError(f"backend {waiter_pid} never waited on backend {holder_pid}")


async def _count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as s:
        return int((await s.execute(text("SELECT count(*) FROM support_escalations"))).scalar_one())


_EDITS = {
    "protect-source": TenantAccessPolicy(protected_channel_ids=(_CHANNEL,)),
    "drop-invoker": TenantAccessPolicy(invoker_user_ids=("U_SOMEONE_ELSE",)),
}


@pytest.mark.parametrize("policy_row", [False, True], ids=["open-default", "stored-row"])
@pytest.mark.parametrize("edit", sorted(_EDITS))
async def test_an_edit_committed_while_waiting_on_the_credit_lock_refuses(
    db_session: AsyncSession,
    race_engine: AsyncEngine,
    fake_slack_web_client: Any,
    recorder_pid: asyncio.Future[int],
    policy_row: bool,
    edit: str,
) -> None:
    tenant_id, _account, key = await _seed(db_session, policy_row=policy_row)
    factory = async_sessionmaker(race_engine, expire_on_commit=False)
    runtime = _runtime(key, factory)
    decision = evaluate_support_submission(_submit_payload("help"))

    async with factory() as holder, holder.begin():
        # Another request of the same person holds the ledger lock.
        await ledger._lock_user_ledger(  # pyright: ignore[reportPrivateUsage]
            holder, tenant_id=tenant_id, platform_user_id=_USER
        )
        submitting = asyncio.create_task(run_support_submission(runtime, decision))
        await _blocked_by(race_engine, await recorder_pid, await _pid(holder))
        async with policy_write_transaction(factory, tenant_id=tenant_id) as editor:
            await set_access_policy(editor, tenant_id=tenant_id, policy=_EDITS[edit])
    await asyncio.wait_for(submitting, 10)

    assert await _count(factory) == 0, "a refused request must spend no credit"
    assert _posts(fake_slack_web_client) == []
    assert _ephemeral_texts(fake_slack_web_client) == [NOT_ALLOWED]


@pytest.mark.parametrize("policy_row", [False, True], ids=["open-default", "stored-row"])
async def test_an_edit_arriving_while_the_recorder_holds_the_locks_waits_then_applies(
    db_session: AsyncSession,
    race_engine: AsyncEngine,
    fake_slack_web_client: Any,
    recorder_pid: asyncio.Future[int],
    policy_row: bool,
) -> None:
    """Also the deadlock check: the edit's lock wait and the recorder's
    foreign-key wait must not form a cycle, and a row keyed to the tenant is
    inserted by the account holder while the recorder holds the policy lock."""
    tenant_id, account_id, key = await _seed(db_session, policy_row=policy_row, with_account=True)
    assert account_id is not None
    fake_slack_web_client.mock.get(  # pyright: ignore[reportUnknownMemberType]
        _PERMALINK_PATTERN, payload={"ok": True, "permalink": _PERMALINK}, repeat=True
    )
    factory = async_sessionmaker(race_engine, expire_on_commit=False)
    runtime = _runtime(key, factory)
    decision = evaluate_support_submission(_submit_payload("help"))
    editor_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def edit() -> None:
        async with policy_write_transaction(factory, tenant_id=tenant_id) as editor:
            editor_pid.set_result(await _pid(editor))
            await set_access_policy(editor, tenant_id=tenant_id, policy=_EDITS["protect-source"])

    async with factory() as holder, holder.begin():
        # Hold the asker's account row, so the recorder's insert (whose
        # foreign-key check takes KEY SHARE on it) waits AFTER both locks.
        await holder.execute(
            text("SELECT id FROM accounts WHERE id = :id FOR UPDATE"), {"id": account_id}
        )
        submitting = asyncio.create_task(run_support_submission(runtime, decision))
        recorder = await recorder_pid
        await _blocked_by(race_engine, recorder, await _pid(holder))
        editing = asyncio.create_task(edit())
        await _blocked_by(race_engine, await editor_pid, recorder)
        # A row keyed to the tenant, written by the account holder while the
        # recorder holds the tenant's policy lock: KEY SHARE must not wait on
        # NO KEY UPDATE, or this is a deadlock.
        await make_account(holder, tenant=await _tenant_row(holder, tenant_id))
    await asyncio.wait_for(asyncio.gather(submitting, editing), 15)

    assert await _count(factory) == 1, "the request that decided first is recorded"
    assert len(_posts(fake_slack_web_client)) == 1
    assert _ephemeral_texts(fake_slack_web_client) == [received_text(remaining=19)]
    async with factory() as s:
        policy = await load_access_policy(s, tenant_id=tenant_id)
    assert policy.protected_channel_ids == (_CHANNEL,), "the edit applies after the request"


@pytest.mark.parametrize(
    "separate_workspace", [False, True], ids=["same-workspace", "ops-workspace"]
)
async def test_destination_protected_during_the_permalink_call_gets_no_post(
    db_session: AsyncSession,
    race_engine: AsyncEngine,
    fake_slack_web_client: Any,
    separate_workspace: bool,
) -> None:
    tenant_id, _account, key = await _seed(db_session, policy_row=False)
    dest_tenant = tenant_id
    if separate_workspace:
        dest_tenant = await _seed_workspace(db_session, team_id=_OPS_TEAM, fernet_key=key)
        await db_session.commit()
    factory = async_sessionmaker(race_engine, expire_on_commit=False)
    support: dict[str, Any] = {"slack_escalation_team_id": _OPS_TEAM} if separate_workspace else {}
    runtime = _runtime(key, factory, **support)
    protect = TenantAccessPolicy(protected_channel_ids=(_ESC_CHANNEL,))

    async def permalink_then_protect(url: Any, **kwargs: Any) -> CallbackResult:
        # The protection commits while the permalink request is in flight.
        async with policy_write_transaction(factory, tenant_id=dest_tenant) as editor:
            await set_access_policy(editor, tenant_id=dest_tenant, policy=protect)
        return CallbackResult(payload={"ok": True, "permalink": _PERMALINK})

    fake_slack_web_client.mock.get(  # pyright: ignore[reportUnknownMemberType]
        _PERMALINK_PATTERN, callback=permalink_then_protect, repeat=True
    )

    await run_support_submission(runtime, evaluate_support_submission(_submit_payload("help")))

    async with factory() as s:
        policy = await load_access_policy(s, tenant_id=dest_tenant)
        delivered = (
            await s.execute(text("SELECT delivered_at FROM support_escalations"))
        ).scalar_one()
    assert not authorize(
        policy, subject=Subject(), action=Action.POST, place=Place(channel_id=_ESC_CHANNEL)
    )
    assert _posts(fake_slack_web_client) == [], "a destination protected meanwhile gets no post"
    assert delivered is None, "the committed request stays recorded, undelivered"
    assert _ephemeral_texts(fake_slack_web_client) == [RECORDED_UNDELIVERED]
