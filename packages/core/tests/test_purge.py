"""Integration tests for the GDPR purge orchestrator (`daimon.core.purge`).

Run against the schema-per-test Postgres fixture; every test exercises real
SQL through the store helpers. The orchestrator opens its own transactions
via `db_session_factory` (sm); `db_session` is used for setup + DB-level
assertions on the same per-test schema.

Importing `daimon.core._models` directly here is the documented test-only
escape hatch — tests live outside the `daimon.*` package and are exempt from
the import-linter ORM contract.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from daimon.core import purge as purge_module
from daimon.core._models import (
    Account,
    ChannelConfig,
    CliPrincipal,
    PlatformPrincipal,
    PrincipalLink,
    Routine,
    TenantConfig,
    UserConfig,
)
from daimon.core.purge import AccountPurgeResult, PurgeReport, purge_account, purge_principal
from daimon.core.stores import github_credentials as github_credentials_store
from daimon.core.stores import github_oauth_states as github_oauth_states_store
from daimon.core.stores import routines as routines_store
from daimon.core.stores import user_skills as user_skills_store
from daimon.testing.factories import (
    link_principals,
    make_account,
    make_cli_principal,
    make_platform_principal,
    make_tenant,
)
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .factories.github import make_oauth_state


async def test_purge_principal_removes_all_principal_scoped_rows(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    pp = await make_platform_principal(
        db_session,
        platform="discord",
        external_id="user-123",
        tenant=tenant,
        account=account,
    )
    cli = await make_cli_principal(db_session, os_user="cli-1", tenant=tenant, account=account)
    await link_principals(db_session, cli=cli, platform=pp)

    # Two routines created by the platform principal, plus one by an unrelated user.
    await routines_store.create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="user-123",
        agent_id="a1",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="m1",
    )
    await routines_store.create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="user-123",
        agent_id="a1",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="m2",
    )
    await routines_store.create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="user-other",
        agent_id="a1",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="m3",
    )
    await db_session.commit()

    report = await purge_principal(sm=db_session_factory, principal_id=pp.id, kind="platform")

    assert report.routines == 2, "should delete both routines for platform principal"
    assert report.principal_links == 1, "should delete the cli<->platform link"
    assert report.platform_principals == 1, "should delete the platform principal row"
    assert report.cli_principals == 0, (
        "cli principal must remain when purging only the platform side"
    )
    assert report.accounts == 0, "account-level deletes are out of scope for purge_principal"

    # DB-level assertions: targeted rows gone, unrelated rows survive.
    pp_row = (
        await db_session.execute(select(PlatformPrincipal).where(PlatformPrincipal.id == pp.id))
    ).scalar_one_or_none()
    assert pp_row is None, "platform principal row must be gone"

    link_rows = (
        (
            await db_session.execute(
                select(PrincipalLink).where(PrincipalLink.platform_principal_id == pp.id)
            )
        )
        .scalars()
        .all()
    )
    assert link_rows == [], "principal_link must be gone"

    remaining_routines = (
        (await db_session.execute(select(Routine).where(Routine.created_by_user_id == "user-123")))
        .scalars()
        .all()
    )
    assert remaining_routines == [], "no routines remain for the purged user"

    other_routines = (
        (
            await db_session.execute(
                select(Routine).where(Routine.created_by_user_id == "user-other")
            )
        )
        .scalars()
        .all()
    )
    assert len(other_routines) == 1, "unrelated user's routine must survive"

    cli_row = (
        await db_session.execute(select(CliPrincipal).where(CliPrincipal.id == cli.id))
    ).scalar_one_or_none()
    assert cli_row is not None, "cli principal must survive when only the platform side was purged"

    account_row = (
        await db_session.execute(select(Account).where(Account.id == account.id))
    ).scalar_one_or_none()
    assert account_row is not None, "account must survive purge_principal"


async def test_purge_principal_idempotent_on_rerun(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    pp = await make_platform_principal(
        db_session,
        platform="discord",
        external_id="user-x",
        tenant=tenant,
        account=account,
    )
    await routines_store.create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="user-x",
        agent_id="a1",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="m1",
    )
    await db_session.commit()

    first = await purge_principal(sm=db_session_factory, principal_id=pp.id, kind="platform")
    assert first.platform_principals == 1, "first call deletes the principal row"

    second = await purge_principal(sm=db_session_factory, principal_id=pp.id, kind="platform")
    assert second == PurgeReport(), "rerun on missing principal returns all-zero report"


async def test_purge_principal_leaves_other_users_routines(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    pp_a = await make_platform_principal(
        db_session, platform="discord", external_id="user-a", tenant=tenant
    )
    pp_b = await make_platform_principal(
        db_session, platform="discord", external_id="user-b", tenant=tenant
    )
    await routines_store.create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="user-a",
        agent_id="a1",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="ma",
    )
    await routines_store.create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="user-b",
        agent_id="a1",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="mb",
    )
    await db_session.commit()

    report = await purge_principal(sm=db_session_factory, principal_id=pp_a.id, kind="platform")
    assert report.routines == 1, "only user-a's routine deleted"

    surviving = (
        (await db_session.execute(select(Routine).where(Routine.created_by_user_id == "user-b")))
        .scalars()
        .all()
    )
    assert len(surviving) == 1, "user-b's routine must survive"

    pp_b_row = (
        await db_session.execute(select(PlatformPrincipal).where(PlatformPrincipal.id == pp_b.id))
    ).scalar_one_or_none()
    assert pp_b_row is not None, "user-b's principal must survive"


async def test_purge_account_respects_fk_order(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    cli = await make_cli_principal(db_session, os_user="cli-1", tenant=tenant, account=account)
    pp = await make_platform_principal(
        db_session,
        platform="discord",
        external_id="user-fk",
        tenant=tenant,
        account=account,
    )
    db_session.add(UserConfig(account_id=account.id, agent_name="a", environment_name="e"))
    await db_session.flush()
    await db_session.commit()

    report = await purge_account(sm=db_session_factory, account_id=account.id)

    assert report.db.cli_principals == 1, "one cli principal deleted"
    assert report.db.platform_principals == 1, "one platform principal deleted"
    assert report.db.user_configs == 1, "user_config deleted"
    assert report.db.accounts == 1, "account deleted"

    # Every row gone — reading by id returns None for each.
    assert (
        await db_session.execute(select(Account).where(Account.id == account.id))
    ).scalar_one_or_none() is None, "account row gone"
    assert (
        await db_session.execute(select(CliPrincipal).where(CliPrincipal.id == cli.id))
    ).scalar_one_or_none() is None, "cli principal gone"
    assert (
        await db_session.execute(select(PlatformPrincipal).where(PlatformPrincipal.id == pp.id))
    ).scalar_one_or_none() is None, "platform principal gone"
    assert (
        await db_session.execute(select(UserConfig).where(UserConfig.account_id == account.id))
    ).scalar_one_or_none() is None, "user_config gone"


async def test_purge_account_with_linked_principals(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    cli = await make_cli_principal(db_session, os_user="cli-2", tenant=tenant, account=account)
    pp = await make_platform_principal(
        db_session,
        platform="discord",
        external_id="user-l",
        tenant=tenant,
        account=account,
    )
    await link_principals(db_session, cli=cli, platform=pp)
    await db_session.commit()

    report = await purge_account(sm=db_session_factory, account_id=account.id)

    assert report.db.principal_links >= 1, "link row deleted as part of account purge"
    assert report.db.cli_principals == 1, "cli principal deleted"
    assert report.db.platform_principals == 1, "platform principal deleted"
    assert report.db.accounts == 1, "account deleted"

    remaining_links = (
        (
            await db_session.execute(
                select(PrincipalLink).where(PrincipalLink.cli_principal_id == cli.id)
            )
        )
        .scalars()
        .all()
    )
    assert remaining_links == [], "no links remain"


async def test_purge_account_rolls_back_on_partial_failure(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    cli = await make_cli_principal(db_session, os_user="cli-rb", tenant=tenant, account=account)
    pp = await make_platform_principal(
        db_session,
        platform="discord",
        external_id="user-rb",
        tenant=tenant,
        account=account,
    )
    await routines_store.create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="user-rb",
        agent_id="a1",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="m",
    )
    await db_session.commit()

    async def boom(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("boom")

    # Patch the symbol the orchestrator sees (purge_module rebinds names).
    monkeypatch.setattr(purge_module.accounts_store, "delete_account", boom)

    with pytest.raises(RuntimeError, match="boom"):
        await purge_account(sm=db_session_factory, account_id=account.id)

    # All original rows must still exist — full transaction rollback.
    await db_session.rollback()  # clear any aborted state on the shared connection

    assert (
        await db_session.execute(select(Account).where(Account.id == account.id))
    ).scalar_one_or_none() is not None, "account survives rollback"
    assert (
        await db_session.execute(select(CliPrincipal).where(CliPrincipal.id == cli.id))
    ).scalar_one_or_none() is not None, "cli principal survives rollback"
    assert (
        await db_session.execute(select(PlatformPrincipal).where(PlatformPrincipal.id == pp.id))
    ).scalar_one_or_none() is not None, "platform principal survives rollback"
    surviving_routines = (
        (await db_session.execute(select(Routine).where(Routine.created_by_user_id == "user-rb")))
        .scalars()
        .all()
    )
    assert len(surviving_routines) == 1, "routine survives rollback"


async def test_purge_does_not_touch_tenant_scoped_rows(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Account purge must not delete tenant-scoped config rows (TenantConfig, ChannelConfig).

    These rows are keyed by tenant_id and outlive the individual account — they
    belong to the tenant (guild), not to any single user account.
    """
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await make_cli_principal(db_session, os_user="cli-tenant", tenant=tenant, account=account)

    db_session.add(
        TenantConfig(
            tenant_id=tenant.id,
            agent_name="wa",
            environment_name="we",
        )
    )
    db_session.add(
        ChannelConfig(
            tenant_id=tenant.id,
            channel_id="c-1",
            agent_name="ca",
            environment_name="ce",
        )
    )
    await db_session.flush()
    await db_session.commit()

    await purge_account(sm=db_session_factory, account_id=account.id)

    tc_row = (
        await db_session.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant.id))
    ).scalar_one_or_none()
    assert tc_row is not None, "tenant_config must survive account purge"

    cc_row = (
        await db_session.execute(
            select(ChannelConfig).where(
                ChannelConfig.tenant_id == tenant.id, ChannelConfig.channel_id == "c-1"
            )
        )
    ).scalar_one_or_none()
    assert cc_row is not None, "channel_config must survive account purge"


# ---------------------------------------------------------------------------
# AccountPurgeResult / upstream session deletion tests (Task 2)
# ---------------------------------------------------------------------------


def _make_fake_anthropic_with_sessions(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    sessions_count: int,
) -> Any:
    """Build a fake AsyncAnthropic that serves `sessions_count` sessions tagged
    for `account_id`, all belonging to a single agent tagged for `tenant_id`."""
    from datetime import UTC, datetime
    from typing import Any as _Any

    from anthropic.types.beta import (
        BetaManagedAgentsAgent,
        BetaManagedAgentsModelConfig,
        BetaManagedAgentsSession,
    )
    from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
    from anthropic.types.beta.beta_managed_agents_session_stats import BetaManagedAgentsSessionStats
    from anthropic.types.beta.beta_managed_agents_session_usage import BetaManagedAgentsSessionUsage

    now = datetime.now(UTC)

    agent = BetaManagedAgentsAgent(
        id="agent_test1",
        archived_at=None,
        created_at=now,
        description=None,
        mcp_servers=[],
        metadata={"daimon_tenant": str(tenant_id)},
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
        name="test-agent",
        skills=[],
        system=None,
        tools=[],
        type="agent",
        updated_at=now,
        version=1,
    )
    agent_dict: dict[str, _Any] = agent.model_dump(mode="json")

    session_dicts: list[dict[str, _Any]] = []
    for i in range(sessions_count):
        sid = f"sesn_target{i}"
        s = BetaManagedAgentsSession(
            id=sid,
            agent=BetaManagedAgentsSessionAgent(
                id="agent_test1",
                description=None,
                mcp_servers=[],
                model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
                name="test-agent",
                skills=[],
                system=None,
                tools=[],
                type="agent",
                version=1,
            ),
            archived_at=None,
            created_at=now,
            environment_id="env_test1",
            metadata={"daimon_account": str(account_id)},
            resources=[],
            stats=BetaManagedAgentsSessionStats(),
            status="idle",
            title=None,
            type="session",
            updated_at=now,
            usage=BetaManagedAgentsSessionUsage(),
            vault_ids=[],
        )
        session_dicts.append(s.model_dump(mode="json"))

    router = MARouter()

    def handle_agents_list(request: httpx.Request, match: Any) -> httpx.Response:
        return list_response([agent_dict])

    def handle_sessions_list(request: httpx.Request, match: Any) -> httpx.Response:
        return list_response(session_dicts)

    def handle_session_delete(request: httpx.Request, match: Any) -> httpx.Response:
        session_id = match.group(1)
        return httpx.Response(200, json={"id": session_id, "type": "session_deleted"})

    router.add("GET", r"/v1/agents", handle_agents_list)
    router.add("GET", r"/v1/sessions", handle_sessions_list)
    router.add("DELETE", r"/v1/sessions/([^/]+)", handle_session_delete)
    return build_fake_anthropic(router.dispatch)


async def test_purge_account_with_anthropic_returns_account_purge_result_with_sessions_deleted(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """purge_account with an injected AsyncAnthropic returns AccountPurgeResult
    whose .sessions.deleted reflects the number of tagged MA sessions deleted."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await make_cli_principal(db_session, os_user="cli-up", tenant=tenant, account=account)
    await db_session.commit()

    client = _make_fake_anthropic_with_sessions(tenant.id, account.id, sessions_count=2)

    result = await purge_account(sm=db_session_factory, account_id=account.id, anthropic=client)

    assert isinstance(result, AccountPurgeResult), "must return AccountPurgeResult"
    assert result.db.accounts == 1, "DB purge must still delete the account row"
    assert result.db.cli_principals == 1, "DB purge must still delete the cli principal"
    assert result.sessions.deleted == 2, "2 tagged MA sessions must be deleted upstream"
    assert result.sessions.failed == 0, "no upstream failures expected"


async def test_purge_account_without_anthropic_returns_account_purge_result_sessions_zero(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """purge_account without anthropic (default None) does DB-only purge and returns
    AccountPurgeResult with sessions.deleted==0, sessions.failed==0."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await make_cli_principal(db_session, os_user="cli-noup", tenant=tenant, account=account)
    await db_session.commit()

    result = await purge_account(sm=db_session_factory, account_id=account.id)

    assert isinstance(result, AccountPurgeResult), (
        "must return AccountPurgeResult even without anthropic"
    )
    assert result.db.accounts == 1, "DB purge must delete the account row"
    assert result.db.cli_principals == 1, "DB purge must delete the cli principal"
    assert result.sessions.deleted == 0, "no upstream attempt when anthropic=None"
    assert result.sessions.failed == 0, "no upstream attempt when anthropic=None"


async def test_purge_account_upstream_failure_after_commit_reports_upstream_error(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An APIError during upstream session enumeration must NOT propagate.

    The DB transaction has already committed; raising would make the adapter
    misreport a completed, irreversible erasure as failed. purge_account
    returns normally with sessions.upstream_error=True instead.
    """
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    cli = await make_cli_principal(db_session, os_user="cli-uperr", tenant=tenant, account=account)
    await db_session.commit()

    def raise_connect_error(request: httpx.Request, match: Any) -> httpx.Response:
        raise httpx.ConnectError("upstream unreachable", request=request)

    router = MARouter()
    router.add("GET", r"/v1/agents", raise_connect_error)
    client = build_fake_anthropic(router.dispatch)

    result = await purge_account(sm=db_session_factory, account_id=account.id, anthropic=client)

    assert result.db.accounts == 1, "DB purge must commit despite the upstream failure"
    assert result.sessions.upstream_error is True, (
        "post-commit upstream APIError must be folded into sessions.upstream_error"
    )
    assert result.sessions.deleted == 0, "no sessions were deleted before the failure"

    # DB-level assertion: the purge really committed.
    assert (
        await db_session.execute(select(Account).where(Account.id == account.id))
    ).scalar_one_or_none() is None, "account row must be gone — upstream failure never rolls back"
    assert (
        await db_session.execute(select(CliPrincipal).where(CliPrincipal.id == cli.id))
    ).scalar_one_or_none() is None, "cli principal must be gone"


async def test_purge_account_deletes_sessions_across_all_principal_tenants(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Linked principals spanning tenants: upstream deletion loops every tenant.

    An account with a CLI principal in tenant A and a platform principal in
    tenant B (principal_links permits this) must have its tagged MA sessions
    deleted under BOTH tenants' agents — not just the first principal's tenant.
    """
    from datetime import UTC, datetime
    from typing import Any as _Any

    from anthropic.types.beta import (
        BetaManagedAgentsAgent,
        BetaManagedAgentsModelConfig,
        BetaManagedAgentsSession,
    )
    from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
    from anthropic.types.beta.beta_managed_agents_session_stats import BetaManagedAgentsSessionStats
    from anthropic.types.beta.beta_managed_agents_session_usage import BetaManagedAgentsSessionUsage

    tenant_a = await make_tenant(db_session, workspace_id="mt-guild-a")
    tenant_b = await make_tenant(db_session, workspace_id="mt-guild-b")
    account = await make_account(db_session, tenant=tenant_a)
    await make_cli_principal(db_session, os_user="mt-cli", tenant=tenant_a, account=account)
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="mt-user",
        tenant=tenant_b,
        account=account,
    )
    await db_session.commit()

    # Fake MA: one agent per tenant; tagged sessions under each agent.
    now = datetime.now(UTC)
    agent_dicts: list[dict[str, _Any]] = []
    session_dicts_by_agent: dict[str, list[dict[str, _Any]]] = {}
    for agent_id, tenant_uuid, session_count in (
        ("agent_tenantA", tenant_a.id, 1),
        ("agent_tenantB", tenant_b.id, 2),
    ):
        agent = BetaManagedAgentsAgent(
            id=agent_id,
            archived_at=None,
            created_at=now,
            description=None,
            mcp_servers=[],
            metadata={"daimon_tenant": str(tenant_uuid)},
            model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
            name=f"test-{agent_id}",
            skills=[],
            system=None,
            tools=[],
            type="agent",
            updated_at=now,
            version=1,
        )
        agent_dicts.append(agent.model_dump(mode="json"))
        sessions: list[dict[str, _Any]] = []
        for i in range(session_count):
            s = BetaManagedAgentsSession(
                id=f"sesn_{agent_id}_{i}",
                agent=BetaManagedAgentsSessionAgent(
                    id=agent_id,
                    description=None,
                    mcp_servers=[],
                    model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
                    name=f"test-{agent_id}",
                    skills=[],
                    system=None,
                    tools=[],
                    type="agent",
                    version=1,
                ),
                archived_at=None,
                created_at=now,
                environment_id="env_test1",
                metadata={"daimon_account": str(account.id)},
                resources=[],
                stats=BetaManagedAgentsSessionStats(),
                status="idle",
                title=None,
                type="session",
                updated_at=now,
                usage=BetaManagedAgentsSessionUsage(),
                vault_ids=[],
            )
            sessions.append(s.model_dump(mode="json"))
        session_dicts_by_agent[agent_id] = sessions

    deleted_session_ids: list[str] = []
    router = MARouter()

    def handle_agents_list(request: httpx.Request, match: Any) -> httpx.Response:
        return list_response(agent_dicts)

    def handle_sessions_list(request: httpx.Request, match: Any) -> httpx.Response:
        requested_agent_id = request.url.params.get("agent_id")
        return list_response(session_dicts_by_agent.get(requested_agent_id, []))

    def handle_session_delete(request: httpx.Request, match: Any) -> httpx.Response:
        session_id = match.group(1)
        deleted_session_ids.append(session_id)
        return httpx.Response(200, json={"id": session_id, "type": "session_deleted"})

    router.add("GET", r"/v1/agents", handle_agents_list)
    router.add("GET", r"/v1/sessions", handle_sessions_list)
    router.add("DELETE", r"/v1/sessions/([^/]+)", handle_session_delete)
    client = build_fake_anthropic(router.dispatch)

    result = await purge_account(sm=db_session_factory, account_id=account.id, anthropic=client)

    assert result.sessions.deleted == 3, (
        "sessions under BOTH tenants' agents must be deleted (1 in tenant A + 2 in tenant B)"
    )
    assert result.sessions.upstream_error is False, "no upstream failure expected"
    assert set(deleted_session_ids) == {
        "sesn_agent_tenantA_0",
        "sesn_agent_tenantB_0",
        "sesn_agent_tenantB_1",
    }, "each tenant's tagged sessions must receive a DELETE — not just the first tenant's"


# ---------------------------------------------------------------------------
# New-table behavioral tests (user_skills, github_credentials,
# github_oauth_states)
# ---------------------------------------------------------------------------


async def _seed_user_skill(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    name: str = "brainstorming",
) -> None:
    await user_skills_store.upsert_user_skill(
        session,
        tenant_id=tenant_id,
        principal_id=principal_id,
        agent_name="dev",
        name=name,
        source_repo_url="https://github.com/o/r",
        source_repo_branch="main",
        source_path="",
        content_hash="hash",
        anthropic_id=None,
        anthropic_latest_version=None,
    )


async def test_purge_principal_platform_deletes_all_three_new_tables(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Platform principal purge removes user_skills, github_credentials, and oauth-state rows."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    pp = await make_platform_principal(
        db_session,
        platform="discord",
        external_id="user-new",
        tenant=tenant,
        account=account,
    )
    # Seed all three new tables for the target principal.
    await _seed_user_skill(db_session, tenant_id=tenant.id, principal_id=pp.id)
    await github_credentials_store.upsert_credential(
        db_session,
        principal_id=pp.id,
        github_login="octocat",
        encrypted_token=b"tok",
        scopes=("repo",),
    )
    await make_oauth_state(
        db_session,
        platform="discord",
        platform_user_id="user-new",
        scopes=("repo",),
        tenant_id=tenant.id,
    )
    # Seed an unrelated platform principal to verify survival.
    pp_other = await make_platform_principal(
        db_session,
        platform="discord",
        external_id="user-other-new",
        tenant=tenant,
    )
    await _seed_user_skill(
        db_session, tenant_id=tenant.id, principal_id=pp_other.id, name="other-skill"
    )
    await github_credentials_store.upsert_credential(
        db_session,
        principal_id=pp_other.id,
        github_login="other-octocat",
        encrypted_token=b"other-tok",
        scopes=("repo",),
    )
    await db_session.commit()

    report = await purge_principal(sm=db_session_factory, principal_id=pp.id, kind="platform")

    assert report.user_skills == 1, "must delete the user_skills row"
    assert report.github_credentials == 1, "must delete the github_credentials row"
    assert report.github_oauth_states == 1, "must delete the oauth-state row"

    # Other principal's rows must survive.
    other_login = await github_credentials_store.get_credential_login_by_principal(
        db_session, principal_id=pp_other.id
    )
    assert other_login == "other-octocat", "other principal's github_credentials must survive"
    other_skills = await user_skills_store.count_user_skills_for_principal(
        db_session, principal_id=pp_other.id
    )
    assert other_skills == 1, "other principal's user_skills must survive"


async def test_purge_principal_ghost_row_deleted_across_tenant_ids(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ghost rows under a stale tenant_id are still purged (Pitfall 3)."""
    tenant_a = await make_tenant(db_session, workspace_id="ghost-guild-a")
    tenant_b = await make_tenant(db_session, workspace_id="ghost-guild-b")
    account = await make_account(db_session, tenant=tenant_a)
    pp = await make_platform_principal(
        db_session,
        platform="discord",
        external_id="user-ghost",
        tenant=tenant_a,
        account=account,
    )
    # Seed user_skills row under a DIFFERENT tenant_id (the ghost-row scenario).
    await user_skills_store.upsert_user_skill(
        db_session,
        tenant_id=tenant_b.id,  # stale tenant_id — the key difference
        principal_id=pp.id,
        agent_name="dev",
        name="ghost-skill",
        source_repo_url="https://github.com/o/r",
        source_repo_branch="main",
        source_path="",
        content_hash="ghost-hash",
        anthropic_id=None,
        anthropic_latest_version=None,
    )
    await db_session.commit()

    report = await purge_principal(sm=db_session_factory, principal_id=pp.id, kind="platform")

    assert report.user_skills == 1, (
        "ghost row under stale tenant_id must be deleted — tenant-agnostic predicate"
    )


async def test_purge_principal_cli_deletes_all_three_new_tables(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CLI principal purge removes user_skills, github_credentials, and ("cli", os_user) oauth-state rows (Pitfall 6)."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    cli = await make_cli_principal(
        db_session, os_user="alice-purge", tenant=tenant, account=account
    )
    await _seed_user_skill(db_session, tenant_id=tenant.id, principal_id=cli.id)
    await github_credentials_store.upsert_credential(
        db_session,
        principal_id=cli.id,
        github_login="alice-gh",
        encrypted_token=b"cli-tok",
        scopes=("repo",),
    )
    await make_oauth_state(
        db_session,
        platform="cli",
        platform_user_id="alice-purge",
        scopes=("repo",),
        tenant_id=tenant.id,
    )
    await db_session.commit()

    report = await purge_principal(sm=db_session_factory, principal_id=cli.id, kind="cli")

    assert report.user_skills == 1, "CLI principal's user_skills must be deleted"
    assert report.github_credentials == 1, "CLI principal's github_credentials must be deleted"
    assert report.github_oauth_states == 1, (
        "CLI principal's ('cli', os_user) oauth-state row must be deleted"
    )
    assert report.cli_principals == 1, "CLI principal row itself must be deleted"


async def test_purge_principal_cli_oauth_states_does_not_delete_same_os_user_in_other_tenant(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """os_user is not globally unique — the CLI oauth-state delete is tenant-scoped.

    Two unrelated people on two machines can share an os_user (e.g. both
    `ubuntu`). Purging one account must not erase the other person's in-flight
    handshake rows in a different tenant.
    """
    tenant_a = await make_tenant(db_session, workspace_id="osuser-guild-a")
    tenant_b = await make_tenant(db_session, workspace_id="osuser-guild-b")
    account_a = await make_account(db_session, tenant=tenant_a)
    cli_a = await make_cli_principal(
        db_session, os_user="ubuntu", tenant=tenant_a, account=account_a
    )
    # A DIFFERENT person, same os_user, in tenant B.
    await make_cli_principal(db_session, os_user="ubuntu", tenant=tenant_b)
    await make_oauth_state(
        db_session,
        platform="cli",
        platform_user_id="ubuntu",
        scopes=("repo",),
        tenant_id=tenant_a.id,
    )
    await make_oauth_state(
        db_session,
        platform="cli",
        platform_user_id="ubuntu",
        scopes=("repo",),
        tenant_id=tenant_b.id,
    )
    await db_session.commit()

    report = await purge_principal(sm=db_session_factory, principal_id=cli_a.id, kind="cli")

    assert report.github_oauth_states == 1, (
        "only tenant A's ('cli', 'ubuntu') handshake row must be deleted"
    )
    surviving = await github_oauth_states_store.count_states_for_platform_user(
        db_session, platform="cli", platform_user_id="ubuntu", tenant_id=tenant_b.id
    )
    assert surviving == 1, "the other person's same-os_user handshake row in tenant B must survive"


async def test_purge_account_merge_sums_new_fields_across_principals(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """purge_account sums new-table counts across all principals (Pitfall 2 / merge())."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    cli = await make_cli_principal(db_session, os_user="merge-cli", tenant=tenant, account=account)
    pp = await make_platform_principal(
        db_session,
        platform="discord",
        external_id="user-merge",
        tenant=tenant,
        account=account,
    )
    # Seed new-table rows for BOTH principals.
    await _seed_user_skill(db_session, tenant_id=tenant.id, principal_id=cli.id, name="cli-skill")
    await _seed_user_skill(db_session, tenant_id=tenant.id, principal_id=pp.id, name="pp-skill")
    await github_credentials_store.upsert_credential(
        db_session,
        principal_id=cli.id,
        github_login="cli-gh",
        encrypted_token=b"cli-tok",
        scopes=("repo",),
    )
    await github_credentials_store.upsert_credential(
        db_session,
        principal_id=pp.id,
        github_login="pp-gh",
        encrypted_token=b"pp-tok",
        scopes=("repo",),
    )
    await make_oauth_state(
        db_session,
        platform="cli",
        platform_user_id="merge-cli",
        scopes=("repo",),
        tenant_id=tenant.id,
    )
    await make_oauth_state(
        db_session,
        platform="discord",
        platform_user_id="user-merge",
        scopes=("repo",),
        tenant_id=tenant.id,
    )
    await db_session.commit()

    result = await purge_account(sm=db_session_factory, account_id=account.id)

    assert result.db.user_skills == 2, "purge_account must sum user_skills across both principals"
    assert result.db.github_credentials == 2, (
        "purge_account must sum github_credentials across both principals"
    )
    assert result.db.github_oauth_states == 2, (
        "purge_account must sum github_oauth_states across both principals"
    )


async def test_purge_account_rolls_back_new_table_rows_on_failure(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New-table rows survive a mid-purge rollback — single transaction holds."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    cli = await make_cli_principal(db_session, os_user="rb-new", tenant=tenant, account=account)
    pp = await make_platform_principal(
        db_session,
        platform="discord",
        external_id="user-rb-new",
        tenant=tenant,
        account=account,
    )
    await _seed_user_skill(db_session, tenant_id=tenant.id, principal_id=cli.id, name="rb-skill")
    await github_credentials_store.upsert_credential(
        db_session,
        principal_id=pp.id,
        github_login="rb-gh",
        encrypted_token=b"rb-tok",
        scopes=("repo",),
    )
    await make_oauth_state(
        db_session,
        platform="discord",
        platform_user_id="user-rb-new",
        scopes=("repo",),
        tenant_id=tenant.id,
    )
    await db_session.commit()

    async def boom(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(purge_module.accounts_store, "delete_account", boom)

    with pytest.raises(RuntimeError, match="boom"):
        await purge_account(sm=db_session_factory, account_id=account.id)

    await db_session.rollback()

    # All new-table rows must survive the rollback.
    surviving_skills = await user_skills_store.count_user_skills_for_principal(
        db_session, principal_id=cli.id
    )
    assert surviving_skills == 1, "user_skills row must survive rolled-back purge"

    surviving_login = await github_credentials_store.get_credential_login_by_principal(
        db_session, principal_id=pp.id
    )
    assert surviving_login == "rb-gh", "github_credentials row must survive rolled-back purge"

    surviving_oauth_states = await github_oauth_states_store.count_states_for_platform_user(
        db_session, platform="discord", platform_user_id="user-rb-new"
    )
    assert surviving_oauth_states == 1, "oauth-state row must survive rolled-back purge"
