"""Domain factories for Daimon test suites.

Provides cross-package factories for creating tenants, accounts, principals.
Kept as free async functions (no class, no factory_boy dependency) — factories
are called directly in tests, making setup explicit and debuggable.

These import daimon.core._models directly. daimon.testing is intentionally
excluded from the ORM contract's source_modules — factories legitimately need
ORM access for test setup.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core._models import (
    Account,
    AgentMemoryStore,
    CliPrincipal,
    PlatformPrincipal,
    PrincipalLink,
    SecurityAuditEvent,
    TaskContinuation,
    Tenant,
    TenantLedger,
    UsageEvent,
)
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import TenantConfigRow, TenantScopeRef
from daimon.core.stores import (
    agent_github_binding,
    agent_repo_binding,
    channel_budgets,
    mcp_tokens,
    routines,
    scoped_config_read,
    scoped_config_write,
    slack_user_tokens,
    tenant_user_caps,
    thread_sessions,
    usage_events,
)
from daimon.core.stores import (
    wizard_session as wizard_session_store,
)
from daimon.core.stores.agent_memory_stores import insert_memory_store
from daimon.core.stores.direct_messages import (
    DirectMessageRow,
    set_dm_enabled,
    start_conversation,
)
from daimon.core.stores.domain import (
    AccountRow,
    AgentGithubBindingRow,
    AgentMemoryStoreRow,
    AgentRepoBindingRow,
    BudgetWindow,
    ChannelBudgetRow,
    CliPrincipalRow,
    ContinuationReason,
    McpTokenRow,
    Platform,
    PlatformPrincipalRow,
    PrincipalLinkRow,
    RepoAccessProof,
    RoutineRow,
    SlackUserTokenRow,
    TaskContinuationRow,
    TenantLedgerRow,
    TenantRow,
    TenantUserCapRow,
    ThreadSessionRow,
    UsageEventRow,
    WizardSessionRow,
)
from daimon.core.stores.security_audit import SecurityAuditRow
from daimon.core.stores.tenant_ledger import insert_entry
from daimon.core.wizard.spec import Option, Step, StepKind, WizardSpec
from daimon.core.wizard.state import new_short_id
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession


async def clear_task_continuations(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    """Remove a tenant's queued continuations so a test can replay the same requests."""
    await session.execute(delete(TaskContinuation).where(TaskContinuation.tenant_id == tenant_id))
    await session.flush()


async def make_task_continuation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    parent_channel_id: str,
    thread_id: str,
    requester_account_id: uuid.UUID,
    requester_external_user_id: str,
    target_ma_agent_id: str,
    target_name: str,
    reason: ContinuationReason,
    idempotency_key: uuid.UUID,
    requested_work: str | None = None,
    available_at: datetime | None = None,
    created_at: datetime | None = None,
) -> TaskContinuationRow:
    """Insert a pending continuation, optionally with a fixed creation time."""
    orm = TaskContinuation(
        tenant_id=tenant_id,
        platform=platform,
        parent_channel_id=parent_channel_id,
        thread_id=thread_id,
        requester_account_id=requester_account_id,
        requester_external_user_id=requester_external_user_id,
        target_ma_agent_id=target_ma_agent_id,
        target_name=target_name,
        requested_work=requested_work,
        reason=reason,
        idempotency_key=idempotency_key,
        available_at=available_at,
    )
    if created_at is not None:
        orm.created_at = created_at
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return TaskContinuationRow.model_validate(orm)


async def make_tenant(
    session: AsyncSession,
    *,
    platform: Platform = "discord",
    workspace_id: str | None = None,
    id: uuid.UUID | None = None,  # mirrors the Tenant.id column name
) -> TenantRow:
    """Create a Tenant row and flush it into the session.

    WARNING: If you call make_tenant() multiple times within the same test schema
    without providing distinct workspace_id values, the UNIQUE(platform, external_id)
    constraint will raise. Pass explicit workspace_id="guild-1", workspace_id="guild-2",
    etc. when a test needs multiple tenants.

    When workspace_id is None (the default), a random UUID is used so default
    calls never collide with each other.

    `id`, when given, overrides the derived tenant id (tests that need a fixed,
    pre-known tenant_id — e.g. shared across fixtures in the same module).
    """
    wid = workspace_id if workspace_id is not None else str(uuid.uuid4())
    tenant_id = id if id is not None else derive_tenant_uuid(platform=platform, workspace_id=wid)
    orm = Tenant(id=tenant_id, platform=platform, external_id=wid)
    session.add(orm)
    await session.flush()
    return TenantRow.model_validate(orm)


async def make_account(
    session: AsyncSession,
    *,
    tenant: TenantRow | None = None,
    id: uuid.UUID | None = None,  # mirrors the Account.id column name
) -> AccountRow:
    tenant = tenant or await make_tenant(session)
    orm = Account(id=id, tenant_id=tenant.id) if id is not None else Account(tenant_id=tenant.id)
    session.add(orm)
    await session.flush()
    return AccountRow.model_validate(orm)


async def make_cli_principal(
    session: AsyncSession,
    *,
    os_user: str = "test",
    tenant: TenantRow | None = None,
    account: AccountRow | None = None,
) -> CliPrincipalRow:
    tenant = tenant or await make_tenant(session)
    account = account or await make_account(session, tenant=tenant)
    orm = CliPrincipal(tenant_id=tenant.id, os_user=os_user, account_id=account.id)
    session.add(orm)
    await session.flush()
    return CliPrincipalRow.model_validate(orm)


async def make_platform_principal(
    session: AsyncSession,
    *,
    platform: str,
    external_id: str,
    tenant: TenantRow | None = None,
    account: AccountRow | None = None,
) -> PlatformPrincipalRow:
    tenant = tenant or await make_tenant(session)
    account = account or await make_account(session, tenant=tenant)
    orm = PlatformPrincipal(
        tenant_id=tenant.id,
        platform=platform,
        external_id=external_id,
        account_id=account.id,
    )
    session.add(orm)
    await session.flush()
    return PlatformPrincipalRow.model_validate(orm)


async def link_principals(
    session: AsyncSession,
    *,
    cli: CliPrincipalRow,
    platform: PlatformPrincipalRow,
) -> PrincipalLinkRow:
    orm = PrincipalLink(
        cli_principal_id=cli.id,
        platform_principal_id=platform.id,
    )
    session.add(orm)
    await session.flush()
    return PrincipalLinkRow.model_validate(orm)


async def make_routine(
    session: AsyncSession,
    *,
    tenant: TenantRow | None = None,
    created_by_user_id: str | None = None,
    agent_id: str = "agent-1",
    agent_name: str = "Agent One",
    cron_expr: str = "0 * * * *",
    timezone_: str = "UTC",
    trigger_message: str = "hello",
    enabled: bool = True,
    next_fire_at: datetime | None = None,
    channel_id: str | None = None,
) -> RoutineRow:
    """Create a Routine row via the real routines store, returning RoutineRow."""
    tenant = tenant or await make_tenant(session)
    return await routines.create_routine(
        session,
        tenant_id=tenant.id,
        created_by_user_id=created_by_user_id,
        agent_id=agent_id,
        agent_name=agent_name,
        cron_expr=cron_expr,
        timezone_=timezone_,
        trigger_message=trigger_message,
        enabled=enabled,
        next_fire_at=next_fire_at,
        channel_id=channel_id,
    )


async def make_tenant_config(
    session: AsyncSession,
    *,
    tenant: TenantRow | None = None,
    agent_name: str | None = "agent-1",
    environment_name: str | None = None,
    mode: scoped_config_write.PropagationMode | None = None,
    actor_account_id: uuid.UUID | None = None,
) -> TenantConfigRow:
    """Upsert a tenant-scope config row via `scoped_config_write.set_fields` (D-07).

    Defaults `agent_name` so a bare call always has at least one field set
    (set_fields raises if none are given). Read back via `scoped_config_read.get_scope`
    rather than raw ORM, since set_fields itself returns None.
    """
    tenant = tenant or await make_tenant(session)
    await scoped_config_write.set_fields(
        session,
        scope=TenantScopeRef(tenant_id=tenant.id),
        tenant_id=tenant.id,
        agent_name=agent_name,
        environment_name=environment_name,
        mode=mode,
        actor_account_id=actor_account_id,
    )
    row = await scoped_config_read.get_scope(session, scope=TenantScopeRef(tenant_id=tenant.id))
    assert isinstance(row, TenantConfigRow), (
        "set_fields on a TenantScopeRef must upsert TenantConfig"
    )
    return row


async def make_usage_event(
    session: AsyncSession,
    *,
    tenant: TenantRow | None = None,
    platform_user_id: str | None = "test-user",
    managed_session_id: str | None = None,
    model: str = "claude-sonnet-5",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    event_id: str | None = None,
    channel_id: str | None = None,
) -> UsageEventRow:
    """Record a usage event via `usage_events.record`, wrapping raw token ints into
    the SDK-typed `BetaManagedAgentsSpanModelUsage` (constructed inline, no
    model_construct per guideline:testing). Supports `platform_user_id=None`
    for the NULL-attribution edge case.

    `record` is idempotent-insert-only (no RETURNING), so the row is read back
    by (managed_session_id, event_id) — the same uniqueness key the store upserts on.
    """
    tenant = tenant or await make_tenant(session)
    managed_session_id = managed_session_id or f"sess_{uuid.uuid4()}"
    event_id = event_id or f"evt_{uuid.uuid4()}"
    model_usage = BetaManagedAgentsSpanModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )
    await usage_events.record(
        session,
        tenant_id=tenant.id,
        platform_user_id=platform_user_id,
        managed_session_id=managed_session_id,
        model=model,
        model_usage=model_usage,
        event_id=event_id,
        channel_id=channel_id,
    )
    orm = (
        await session.execute(
            select(UsageEvent).where(
                UsageEvent.managed_session_id == managed_session_id,
                UsageEvent.event_id == event_id,
            )
        )
    ).scalar_one()
    return UsageEventRow.model_validate(orm, from_attributes=True)


async def make_ledger_entry(
    session: AsyncSession,
    *,
    tenant: TenantRow | None = None,
    delta_usd: Decimal = Decimal("10"),
    reason: str = "topup",
    idempotency_key: str | None = None,
    payment_event_id: str | None = None,
    payment_intent: str | None = None,
    channel_id: str | None = None,
    occurred_at: datetime | None = None,
) -> TenantLedgerRow:
    """Insert a tenant_ledger row via `tenant_ledger.insert_entry`.

    `insert_entry` returns only a bool (inserted vs conflict no-op), so the row
    is read back by `idempotency_key`, the natural-identity uniqueness key.
    """
    tenant = tenant or await make_tenant(session)
    idempotency_key = idempotency_key or f"test-{uuid.uuid4()}"
    await insert_entry(
        session,
        tenant_id=tenant.id,
        delta_usd=delta_usd,
        reason=reason,
        idempotency_key=idempotency_key,
        payment_event_id=payment_event_id,
        payment_intent=payment_intent,
        channel_id=channel_id,
        occurred_at=occurred_at,
    )
    orm = (
        await session.execute(
            select(TenantLedger).where(TenantLedger.idempotency_key == idempotency_key)
        )
    ).scalar_one()
    return TenantLedgerRow.model_validate(orm, from_attributes=True)


async def make_slack_user_token(
    session: AsyncSession,
    *,
    team_id: str | None = None,
    slack_user_id: str = "U123",
    encrypted_token: bytes = b"encrypted-token",
    scopes: str = "chat:write",
    expires_at: datetime | None = None,
    encrypted_refresh_token: bytes | None = None,
) -> SlackUserTokenRow:
    """Upsert a slack_user_tokens row via `slack_user_tokens.upsert_slack_user_token`."""
    team_id = team_id if team_id is not None else str(uuid.uuid4())
    return await slack_user_tokens.upsert_slack_user_token(
        session,
        team_id=team_id,
        slack_user_id=slack_user_id,
        encrypted_token=encrypted_token,
        scopes=scopes,
        expires_at=expires_at,
        encrypted_refresh_token=encrypted_refresh_token,
    )


async def make_tenant_user_cap(
    session: AsyncSession,
    *,
    tenant: TenantRow | None = None,
    user_id: str | None = None,
    amount: Decimal = Decimal("5"),
) -> TenantUserCapRow:
    """Upsert a tenant_user_caps row via `tenant_user_caps.set_default` /
    `set_override`. `user_id=None` (the default) creates the tenant-wide
    default cap; passing `user_id` creates a per-user override.
    """
    tenant = tenant or await make_tenant(session)
    if user_id is None:
        return await tenant_user_caps.set_default(session, tenant_id=tenant.id, amount=amount)
    return await tenant_user_caps.set_override(
        session, tenant_id=tenant.id, user_id=user_id, amount=amount
    )


async def make_channel_budget(
    session: AsyncSession,
    *,
    tenant: TenantRow | None = None,
    platform: str | None = None,
    channel_id: str = "chan-1",
    limit_usd: Decimal = Decimal("5"),
    window: BudgetWindow = "monthly",
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> ChannelBudgetRow:
    """Upsert a channel_budgets row via `channel_budgets.set_channel_budget`."""
    tenant = tenant or await make_tenant(session)
    return await channel_budgets.set_channel_budget(
        session,
        tenant_id=tenant.id,
        platform=platform or tenant.platform,
        channel_id=channel_id,
        limit_usd=limit_usd,
        window=window,
        starts_at=starts_at,
        ends_at=ends_at,
        set_by_account_id=None,
    )


async def make_dm_conversation(
    session: AsyncSession,
    *,
    tenant: TenantRow,
    account_id: uuid.UUID,
    route_key: str = "dm-chan",
    external_user_id: str = "u1",
    workspace_id: str = "g1",
    scope_id: str = "dm:1",
    source_channel_id: str | None = "chan-1",
) -> DirectMessageRow:
    """A DM moved from `source_channel_id` (None: started before sources), with DMs enabled."""
    await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
    conversation = DirectMessageRow(
        platform=tenant.platform,
        route_key=route_key,
        external_user_id=external_user_id,
        tenant_id=tenant.id,
        account_id=account_id,
        workspace_id=workspace_id,
        channel_id=route_key,
        source_channel_id=source_channel_id,
        scope_id=scope_id,
        source_url="https://example.com/source",
        context="",
        memory_read_only=False,
        history=[],
        recent_message_ids=[],
        active_until=None,
    )
    await start_conversation(session, conversation=conversation, now=datetime.now(UTC))
    return conversation


async def make_agent_memory_store(
    session: AsyncSession,
    *,
    tenant: TenantRow | None = None,
    agent_id: uuid.UUID | None = None,
    memory_store_id: str | None = None,
) -> AgentMemoryStoreRow:
    """Bind an agent memory store via the race-safe `insert_memory_store`.

    `insert_memory_store` returns only the winning `memory_store_id` string
    (by design — it's the race-safe half of lazy provisioning), so the full
    row is read back via a PK lookup on the same composite key.
    """
    tenant = tenant or await make_tenant(session)
    agent_id = agent_id if agent_id is not None else uuid.uuid4()
    memory_store_id = memory_store_id or f"memstore_{uuid.uuid4().hex[:12]}"
    await insert_memory_store(
        session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        memory_store_id=memory_store_id,
    )
    orm = await session.get(AgentMemoryStore, (tenant.id, agent_id))
    assert orm is not None, "insert_memory_store must leave a resolvable binding row"
    return AgentMemoryStoreRow.model_validate(orm, from_attributes=True)


async def make_agent_github_binding(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID | None = None,
    principal_id: uuid.UUID | None = None,
) -> AgentGithubBindingRow:
    """Upsert an agent_github_binding row. Defaults `principal_id=agent_id` per
    the per-agent credential model (each agent gets its own isolated row).
    """
    agent_id = agent_id if agent_id is not None else uuid.uuid4()
    principal_id = principal_id if principal_id is not None else agent_id
    return await agent_github_binding.set_agent_github_binding(
        session, agent_id=agent_id, principal_id=principal_id
    )


async def make_agent_repo_binding(
    session: AsyncSession,
    *,
    tenant: TenantRow | None = None,
    agent_id: uuid.UUID | None = None,
    repo_url: str = "owner/repo",
    default_branch: str = "main",
    ma_secret_ref: str | None = None,
    proof: RepoAccessProof | None = None,
) -> AgentRepoBindingRow:
    """Upsert an agent_repo_binding row via `agent_repo_binding.set_binding`."""
    tenant = tenant or await make_tenant(session)
    agent_id = agent_id if agent_id is not None else uuid.uuid4()
    ma_secret_ref = ma_secret_ref or f"secret-{uuid.uuid4().hex[:8]}"
    return await agent_repo_binding.set_binding(
        session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url=repo_url,
        default_branch=default_branch,
        ma_secret_ref=ma_secret_ref,
        proof=proof,
    )


async def make_thread_session(
    session: AsyncSession,
    *,
    tenant: TenantRow | None = None,
    account: AccountRow | None = None,
    platform: str = "discord",
    thread_id: str | None = None,
    ma_session_id: str | None = None,
    ma_agent_id: str | None = None,
    channel_id: str | None = None,
    watermark_message_id: str | None = None,
    created_at: datetime | None = None,
) -> ThreadSessionRow:
    """Insert a thread_sessions row via `thread_sessions.create_thread_session`."""
    tenant = tenant or await make_tenant(session)
    account = account or await make_account(session, tenant=tenant)
    thread_id = thread_id if thread_id is not None else str(uuid.uuid4())
    ma_session_id = ma_session_id or f"sess_{uuid.uuid4()}"
    return await thread_sessions.create_thread_session(
        session,
        tenant_id=tenant.id,
        platform=platform,
        thread_id=thread_id,
        account_id=account.id,
        ma_session_id=ma_session_id,
        ma_agent_id=ma_agent_id,
        channel_id=channel_id,
        watermark_message_id=watermark_message_id,
        created_at=created_at,
    )


async def make_mcp_token(
    session: AsyncSession,
    *,
    tenant: TenantRow | None = None,
    account: AccountRow | None = None,
    agent_id: str = "agent-1",
    label: str | None = None,
    jti: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> McpTokenRow:
    """Insert an mcp_tokens row via `mcp_tokens.create_mcp_token_row`.

    `create_mcp_token_row` returns None, so the row is read back via
    `mcp_tokens.get_mcp_token` on the same `jti` PK.
    """
    tenant = tenant or await make_tenant(session)
    account = account or await make_account(session, tenant=tenant)
    jti = jti if jti is not None else uuid.uuid4()
    created_at = created_at if created_at is not None else datetime.now(UTC)
    await mcp_tokens.create_mcp_token_row(
        session,
        jti=jti,
        account_id=account.id,
        tenant_id=tenant.id,
        agent_id=agent_id,
        label=label,
        created_at=created_at,
    )
    row = await mcp_tokens.get_mcp_token(session, jti=jti)
    assert row is not None, "create_mcp_token_row must leave a resolvable row"
    return row


async def make_wizard_session(
    session: AsyncSession,
    *,
    tenant: TenantRow | None = None,
    account: AccountRow | None = None,
    short_id: str | None = None,
    requester_platform_user_id: str = "U_WIZ",
    channel_id: str | None = None,
    message_id: str | None = None,
    spec: dict[str, Any] | None = None,
    answers: dict[str, list[str]] | None = None,
    current_step: int = 0,
    status: str = "open",
    expires_at: datetime | None = None,
    now: datetime | None = None,
) -> WizardSessionRow:
    """Insert a wizard_session row via `wizard_session.create_wizard_session`."""
    tenant = tenant or await make_tenant(session)
    account = account or await make_account(session, tenant=tenant)
    short_id = short_id or new_short_id()
    channel_id = channel_id if channel_id is not None else f"chan-{uuid.uuid4().hex[:8]}"
    message_id = message_id if message_id is not None else f"msg-{uuid.uuid4().hex[:8]}"
    now = now if now is not None else datetime.now(UTC)
    expires_at = expires_at if expires_at is not None else now + timedelta(hours=1)
    if spec is None:
        default_spec = WizardSpec(
            prompt="Pick one",
            steps=[
                Step(
                    key="choice",
                    question="Pick an option",
                    kind=StepKind.CHOICE,
                    options=[Option(label="A", value="a"), Option(label="B", value="b")],
                )
            ],
        )
        spec = default_spec.model_dump(mode="json")
    return await wizard_session_store.create_wizard_session(
        session,
        short_id=short_id,
        tenant_id=tenant.id,
        account_id=account.id,
        requester_platform_user_id=requester_platform_user_id,
        channel_id=channel_id,
        message_id=message_id,
        spec=spec,
        answers=answers if answers is not None else {},
        current_step=current_step,
        status=status,
        expires_at=expires_at,
        now=now,
    )


async def make_security_audit_event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> SecurityAuditRow:
    """Seed metadata at a chosen time for audit retention and privacy tests."""
    event = SecurityAuditEvent(
        tenant_id=tenant_id,
        account_id=account_id,
        platform="slack",
        platform_user_id="U123",
        tool_name="read",
        outcome="allowed",
        reason="completed",
        occurred_at=occurred_at or datetime.now(UTC),
    )
    session.add(event)
    await session.flush()
    return SecurityAuditRow.model_validate(event)
