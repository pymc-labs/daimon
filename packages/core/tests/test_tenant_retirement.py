"""Tenant retirement: what goes, what accounting keeps, and the upstream cleanup queue."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal

import httpx
import pytest
from daimon.core import tenant_retirement
from daimon.core._models import (
    Account,
    AgentMemoryStore,
    Base,
    SlackBotToken,
    SlackUserToken,
    Tenant,
    TenantLedger,
    TenantRetirement,
    ThreadSession,
    UsageEvent,
)
from daimon.core.config import Settings, load_settings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.stores import slack_bot_tokens, tenant_ledger, tenants
from daimon.core.stores import tenant_retirement as store
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import (
    make_account,
    make_agent_memory_store,
    make_platform_principal,
    make_slack_user_token,
    make_tenant,
    make_thread_session,
    make_usage_event,
)
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Tenant tables with a money column that are purged anyway, and why.
PURGED_DESPITE_MONEY = {
    "spend_allowances": "spending limits are configuration, not a record of spend",
    "channel_budgets": "a channel's budget is configuration",
    "tenant_user_caps": "a person's cap is configuration and names the person",
    "long_job_policy": "a job's spending limit is configuration",
    "mcp_tokens": "a token's spending limit is configuration",
    "web_invites": "an invite's credit is granted as a ledger row",
    "web_messages": "a message's cost is shown to people; usage_events bill it",
    "long_job": "a job's cost is shown to people; usage_events bill it",
    "turn_outcomes": "turn diagnostics; usage_events bill the turn",
}
_MONEY = ("_usd", "_tokens")
_PERSON = ("account_id", "platform_user_id", "session_id", "thread_id", "user_id")


def test_every_tenant_table_is_purged_or_kept_for_accounting() -> None:
    scoped = {t.name for t in store.tenant_tables()}
    assert len(scoped) > 100, f"the metadata scan found too few tenant tables: {len(scoped)}"
    assert set(store.KEEP_FOR_ACCOUNTING) <= scoped, "every kept table is tenant-scoped"
    for table in Base.metadata.sorted_tables:
        if table.name not in scoped or table.name in store.KEEP_FOR_ACCOUNTING:
            continue
        money = [c.name for c in table.c if c.name.endswith(_MONEY)]
        assert not money or table.name in PURGED_DESPITE_MONEY, (
            f"{table.name} holds {money}: keep it for accounting or say why it is purged"
        )


def test_kept_tables_list_every_person_column() -> None:
    tables = Base.metadata.tables
    for name in store.KEEP_FOR_ACCOUNTING:
        person = {c.name for c in tables[name].c if c.name.endswith(_PERSON)}
        listed = set(store.PERSON_COLUMNS.get(name, ()))
        assert person <= listed, f"{name} keeps person identifiers {person - listed}"
        assert listed <= set(tables[name].c.keys()), f"{name} lists missing columns"


async def _seed(
    db_session: AsyncSession, sm: async_sessionmaker[AsyncSession], workspace: str
) -> uuid.UUID:
    result = await provision_tenant(sm, platform="discord", workspace_id=workspace)
    row = await get_tenant(db_session, result.tenant_id)
    assert row is not None, "provisioning creates the tenant"
    account = await make_account(db_session, tenant=row)
    await make_thread_session(db_session, tenant=row, account=account, ma_session_id="sesn_a")
    await make_usage_event(db_session, tenant=row, managed_session_id="sesn_a")
    await make_agent_memory_store(db_session, tenant=row, memory_store_id="memstore_a")
    await db_session.flush()
    return result.tenant_id


async def test_retirement_keeps_the_balance_and_the_spent_trial(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _seed(db_session, db_session_factory, "retired-guild")
    balance = await tenant_ledger.get_balance(db_session, tenant_id=tenant_id)
    ledger_rows = await db_session.scalar(select(func.count()).select_from(TenantLedger))

    assert await store.retire_tenant(db_session, tenant_id=tenant_id), "the tenant exists"

    tenant = await db_session.get(Tenant, tenant_id, populate_existing=True)
    assert tenant is not None and tenant.retired_at is not None, "the tenants row stays, retired"
    for model in (Account, ThreadSession, AgentMemoryStore):
        left = await db_session.scalar(
            select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
        )
        assert left == 0, f"{model.__tablename__} rows go with retirement"
    usage = (await db_session.scalars(select(UsageEvent))).one()
    assert usage.platform_user_id is None, "kept usage no longer names the person"
    assert usage.managed_session_id.startswith("retired:"), "nor the conversation"
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant_id) == balance, (
        "the balance survives retirement"
    )
    work = await store.get_work(db_session, tenant_id=tenant_id)
    assert work is not None and work["sessions"] == ["sesn_a"], "the session is queued upstream"
    assert work["memory_stores"] == ["memstore_a"], "the memory store is queued upstream"
    assert work["hosts"] == ["notebooks", "reports"], "both hosts are queued"

    await provision_tenant(db_session_factory, platform="discord", workspace_id="retired-guild")

    db_session.expire_all()
    assert await db_session.scalar(select(func.count()).select_from(TenantLedger)) == ledger_rows, (
        "a reinstall grants no second trial"
    )
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant_id) == balance
    tenant = await db_session.get(Tenant, tenant_id, populate_existing=True)
    assert tenant is not None and tenant.retired_at is None, "a reinstall ends the retirement"


async def test_upstream_cleanup_keeps_what_failed_until_a_retry(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = uuid.uuid4()
    db_session.add(
        TenantRetirement(
            tenant_id=tenant_id,
            pending={"sessions": ["s1", "s2"], "skills": ["sk1"], "hosts": ["reports"]},
        )
    )
    await db_session.flush()
    deleted: list[str] = []
    failing = {"s2"}

    def deleter(kind: str) -> Callable[[str], Awaitable[None]]:
        async def delete(item: str) -> None:
            if item in failing:
                raise httpx.ConnectError("down")
            deleted.append(f"{kind}:{item}")

        return delete

    deleters = {kind: deleter(kind) for kind in store.PENDING_KINDS}
    done = await tenant_retirement.clean_upstream(
        db_session_factory, tenant_id=tenant_id, deleters=deleters
    )
    assert not done, "a failed delete keeps the work"
    work = await store.get_work(db_session, tenant_id=tenant_id) or {}
    assert {k: v for k, v in work.items() if v} == {"sessions": ["s2"]}, (
        "only the failed session is left"
    )

    failing.clear()
    assert await tenant_retirement.clean_upstream(
        db_session_factory, tenant_id=tenant_id, deleters=deleters
    ), "the retry finishes"
    assert await store.get_work(db_session, tenant_id=tenant_id) is None, "the work row goes"
    assert deleted.count("sessions:s1") == 1, "a finished item is not deleted twice"


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    return load_settings(_env_file=None)


@pytest.mark.parametrize("status", [200, 404])
async def test_host_cleanup_calls_the_tenant_admin_route(status: int, settings: Settings) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"deleted": 1})

    settings = settings.model_copy(
        update={
            "report_host": settings.report_host.model_copy(
                update={"host_url": "https://reports.example", "admin_secret": SecretStr("r")}
            ),
            "notebook": settings.notebook.model_copy(
                update={"host_url": "https://notebooks.example", "admin_secret": SecretStr("n")}
            ),
        }
    )
    tenant_id = uuid.uuid4()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        delete = tenant_retirement.deleters(
            client=None,  # type: ignore[arg-type]
            settings=settings,
            http=http,
            tenant_id=tenant_id,
        )["hosts"]
        await delete("reports")
        await delete("notebooks")
    assert [(r.method, str(r.url)) for r in calls] == [
        ("DELETE", f"https://reports.example/admin/tenants/{tenant_id}/reports"),
        ("DELETE", f"https://notebooks.example/admin/tenants/{tenant_id}/notebooks"),
    ], "each host's tenant route is called; a 404 counts as done"
    assert calls[0].headers["Authorization"] == "Bearer r", "the admin secret authorizes it"


async def test_purge_leaves_no_row_in_any_purged_table(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _seed(db_session, db_session_factory, "purged-guild")
    await store.retire_tenant(db_session, tenant_id=tenant_id)
    for table in store.tenant_tables():
        if table.name in store.KEEP_FOR_ACCOUNTING:
            continue
        left = await db_session.scalar(
            select(func.count()).select_from(table).where(table.c.tenant_id == tenant_id)
        )
        assert left == 0, f"{table.name} keeps rows of a retired tenant"
    assert Decimal(0) <= await tenant_ledger.get_balance(db_session, tenant_id=tenant_id)


async def _slack_world(db_session: AsyncSession) -> tuple[TenantRow, TenantRow]:
    """Two Slack workspaces; A's user and a stranger hold tokens in A's team, B has its own."""
    a = await make_tenant(db_session, platform="slack", workspace_id="T_A")
    b = await make_tenant(db_session, platform="slack", workspace_id="T_B")
    await make_platform_principal(db_session, platform="slack", external_id="U_A", tenant=a)
    await make_platform_principal(db_session, platform="slack", external_id="U_B", tenant=b)
    await make_slack_user_token(db_session, team_id="T_A", slack_user_id="U_A")
    await make_slack_user_token(db_session, team_id="T_A", slack_user_id="U_STRANGER")
    await make_slack_user_token(db_session, team_id="T_B", slack_user_id="U_B")
    for team in ("T_A", "T_B"):
        await slack_bot_tokens.upsert_slack_bot_token(
            db_session, team_id=team, encrypted_token=b"bot"
        )
    await db_session.flush()
    return a, b


async def _slack_tokens(db_session: AsyncSession) -> tuple[set[tuple[str, str]], set[str]]:
    db_session.expire_all()
    users = {
        (r.team_id, r.slack_user_id) for r in (await db_session.scalars(select(SlackUserToken)))
    }
    bots = {r.team_id for r in (await db_session.scalars(select(SlackBotToken)))}
    return users, bots


@pytest.mark.parametrize("how", ["retire", "delete"])
async def test_slack_tokens_go_only_for_the_leaving_workspace(
    db_session: AsyncSession, how: str
) -> None:
    a, _b = await _slack_world(db_session)

    if how == "retire":
        assert await store.retire_tenant(db_session, tenant_id=a.id), "the tenant exists"
    else:
        await tenants.delete_tenant(db_session, tenant_id=a.id)

    users, bots = await _slack_tokens(db_session)
    assert ("T_A", "U_A") not in users, "the leaving tenant's own user token goes"
    assert "T_A" not in bots, "the leaving tenant's bot token goes"
    assert ("T_B", "U_B") in users, "another workspace's user token stays"
    assert "T_B" in bots, "another workspace's bot token stays"
    assert ("T_A", "U_STRANGER") in users, "a token for someone this tenant never knew stays"
