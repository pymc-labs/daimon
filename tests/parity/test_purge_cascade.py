"""Scenario (f): purge -> full cascade INCLUDING MA-side effects via a
populated fake workspace, on both platforms.

`packages/adapters/slack/tests/test_privacy_submit.py`'s
`test_run_purge_and_update_deletes_account_rows_and_calls_views_update` seeds
an EMPTY MARouter agents list -- proving the DB-side cascade but leaving the
upstream MA-session-deletion cascade (`daimon.core.ma.delete_sessions_for_account`,
invoked from inside `purge_account`) completely untested. This scenario closes
that blind spot: the MARouter here returns a real agent tagged for the
tenant AND real sessions tagged for the account, so `AccountPurgeResult.sessions`
reflects an actual upstream deletion, not a vacuous zero.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import httpx
from daimon.core.purge import AccountPurgeResult
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Platform
from daimon.core.stores.identity import find_platform_principal
from daimon.testing import ma_agent, ma_session
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from daimon.testing.ma import MARouter, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .drivers.protocol import PlatformDriver

_SESSIONS_COUNT = 2


def _build_populated_router(*, tenant_id: Any, account_id: Any) -> MARouter:
    """A MARouter with a real tenant-tagged agent AND `_SESSIONS_COUNT`
    account-tagged sessions -- the populated fake this scenario requires
    (never a bare, empty `MARouter()`)."""
    now = datetime.now(UTC)
    agent = ma_agent(
        id="agent_parity_purge",
        name="purge-test-agent",
        metadata={"daimon_tenant": str(tenant_id)},
        created_at=now,
    )
    agent_dict = agent.model_dump(mode="json")

    session_dicts: list[dict[str, Any]] = [
        ma_session(
            id=f"sesn_parity_purge_{i}",
            agent=agent,
            environment_id="env_parity_purge",
            metadata={"daimon_account": str(account_id)},
            created_at=now,
        ).model_dump(mode="json")
        for i in range(_SESSIONS_COUNT)
    ]

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([agent_dict]))
    router.add("GET", r"/v1/sessions", lambda req, _m: list_response(session_dicts))
    router.add(
        "DELETE",
        r"/v1/sessions/(?P<sid>[^/]+)",
        lambda req, m: httpx.Response(200, json={"id": m.group("sid"), "type": "session_deleted"}),
    )
    return router


async def test_purge_account_full_cascade(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(
        db_session,
        platform=cast(Platform, driver.param_id),
        workspace_id=f"purge-{driver.param_id}",
    )
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform=driver.param_id,
        external_id=f"U_PURGE_{driver.param_id}",
        tenant=tenant,
        account=account,
    )
    await db_session.commit()

    router = _build_populated_router(tenant_id=tenant.id, account_id=account.id)

    result = await driver.purge_account(
        sessionmaker=db_session_factory, router=router, account_id=account.id
    )

    assert isinstance(result, AccountPurgeResult), (
        f"{driver.param_id}: purge_account must return AccountPurgeResult"
    )
    assert result.db.accounts == 1, f"{driver.param_id}: DB purge must delete the account row"
    assert result.db.platform_principals == 1, (
        f"{driver.param_id}: DB purge must delete the platform principal row"
    )
    assert result.sessions.deleted == _SESSIONS_COUNT, (
        f"{driver.param_id}: the populated MA workspace's {_SESSIONS_COUNT} account-tagged "
        "sessions must be deleted upstream -- this is the populated-fake assertion the "
        "empty-MARouter blind spot (Pitfall 2) could never make"
    )
    assert result.sessions.failed == 0, f"{driver.param_id}: no upstream failures expected"

    async with db_session_factory() as s:
        assert await get_account(s, account.id) is None, (
            f"{driver.param_id}: account row must be gone after purge"
        )
        assert (
            await find_platform_principal(
                s,
                tenant_id=tenant.id,
                platform=driver.param_id,
                external_id=f"U_PURGE_{driver.param_id}",
            )
            is None
        ), f"{driver.param_id}: platform principal must be gone after purge"
