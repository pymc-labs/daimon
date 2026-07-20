"""SQLAlchemy 2.0 ORM for the tenant-scoped schema.

This module owns the schema. Alembic's `env.py` reads `Base.metadata` from here.
Stores map these ORM objects to Pydantic at their boundary — callers of stores
never see ORM instances.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all daimon-core ORM models."""


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_tenants_platform_external_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    provision_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'ready'")
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="user")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CliPrincipal(Base):
    __tablename__ = "cli_principals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "os_user", name="uq_cli_principals_tenant_os_user"),
        Index("ix_cli_principals_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    os_user: Mapped[str] = mapped_column(Text)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlatformPrincipal(Base):
    __tablename__ = "platform_principals"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "platform",
            "external_id",
            name="uq_platform_principal",
        ),
        Index("ix_platform_principals_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    platform: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    active_agent_name: Mapped[str | None] = mapped_column(Text, nullable=True)


class PrincipalLink(Base):
    __tablename__ = "principal_links"
    __table_args__ = (
        PrimaryKeyConstraint(
            "cli_principal_id", "platform_principal_id", name="pk_principal_links"
        ),
    )

    cli_principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cli_principals.id", ondelete="CASCADE"),
    )
    platform_principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("platform_principals.id", ondelete="CASCADE"),
    )
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserConfig(Base):
    __tablename__ = "user_config"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    agent_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment_name: Mapped[str | None] = mapped_column(Text, nullable=True)


class TenantConfig(Base):
    __tablename__ = "tenant_config"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", name="pk_tenant_config"),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_tenant_config_tenants",
        ),
        ForeignKeyConstraint(
            ["agent_name_set_by_account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
            name="fk_tenant_config_accounts",
        ),
        CheckConstraint("mode IN ('agent', 'user_active')", name="ck_tenant_config_mode"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    agent_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_name_set_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    agent_name_set_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'agent'"))


class ChannelConfig(Base):
    __tablename__ = "channel_config"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "channel_id", name="pk_channel_config"),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_channel_config_tenants",
        ),
        ForeignKeyConstraint(
            ["agent_name_set_by_account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
            name="fk_channel_config_agent_name_set_by_account_id",
        ),
        CheckConstraint("mode IN ('agent', 'user_active')", name="ck_channel_config_mode"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    channel_id: Mapped[str] = mapped_column(Text)
    agent_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_name_set_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    agent_name_set_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'agent'"))


class Routine(Base):
    __tablename__ = "routines"
    __table_args__ = (
        Index(
            "routines_due_idx",
            "next_fire_at",
            postgresql_where=text("enabled AND next_fire_at IS NOT NULL"),
        ),
        Index("routines_tenant_idx", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by_user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    cron_expr: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="UTC")
    trigger_message: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_result_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ThreadSession(Base):
    __tablename__ = "thread_sessions"
    __table_args__ = (
        Index("thread_sessions_lookup_idx", "tenant_id", "platform", "thread_id"),
        Index("thread_sessions_tenant_idx", "tenant_id"),
        Index(
            "thread_sessions_caller_lookup_idx",
            "tenant_id",
            "platform",
            "thread_id",
            "account_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    ma_session_id: Mapped[str] = mapped_column(Text, nullable=False)
    watermark_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'live'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GitHubOauthState(Base):
    __tablename__ = "github_oauth_states"

    state: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    platform_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)  # snapshot of scopes
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class GitHubCredential(Base):
    __tablename__ = "github_credentials"

    # principal_id is an untyped UUID PK — no FK constraint. Rationale:
    # principals are split across cli_principals and platform_principals tables
    # (polymorphic). The resolver signature `get_pat(principal_id, agent_id)`
    # accepts whichever principal type the caller has. A FK to a single principal
    # table would over-constrain. Mirrors the project's existing polymorphic
    # principal handling (PrincipalLink uses two FK columns; we don't have a
    # unified principals table). If/when one is added, this is a mechanical
    # migration.
    principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    github_login: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class UserSkill(Base):
    """User-managed skill row.

    Tracks content_hash dedup + MA id per (tenant, principal, agent_name, name).

    - principal_id is an untyped UUID with no FK constraint (same polymorphic rationale as
      GitHubCredential.principal_id: principals are split across cli_principals and
      platform_principals; FK at this layer would require a polymorphic discriminator).
    - agent_name is a free-form Text string with NO FK — agents are not in the local DB
      (migration 0003 dropped the agents cache table; MA is source of truth). Two principals
      can hold the same (agent_name, name) without colliding because principal_id is in the PK.
    """

    __tablename__ = "user_skills"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "principal_id",
            "agent_name",
            "name",
            name="pk_user_skills",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    source_repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_repo_branch: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    source_path: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    anthropic_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    anthropic_latest_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AgentGithubBinding(Base):
    """Per-agent GitHub credential overlay. Day-1 always empty; populated by
    Discord agent-setup panel. Single credential per principal day-1,
    so no github_login discriminator.
    """

    __tablename__ = "agent_github_binding"

    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)


class AgentGoogleBinding(Base):
    """Per-agent Google Workspace identity overlay.

    Empty day-1; populated by the agent-setup panel. Holds the email
    + scope set the token broker mints credentials for via domain-wide
    delegation against the tenant service account.
    """

    __tablename__ = "agent_google_binding"

    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class UsageEvent(Base):
    """Per-turn token row for billing/observability.

    UNIQUE (managed_session_id, event_id) ensures SSE-replay idempotency:
    `replay_events` (turn/driver.py) refolds events on reconnect; without
    this constraint, `record(...)` would double-insert.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint(
            "managed_session_id",
            "event_id",
            name="uq_usage_events_managed_session_event",
        ),
        Index("usage_events_user_tenant_idx", "tenant_id", "platform_user_id"),
        Index("usage_events_occurred_idx", "occurred_at"),
        Index("usage_events_tenant_idx", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    platform_user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    managed_session_id: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cache_read_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cache_creation_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    event_id: Mapped[str] = mapped_column(Text, nullable=False)


class TenantUserCap(Base):
    """Per-(tenant, user) cap. NULL platform_user_id row = tenant-wide default.

    NULLS NOT DISTINCT on the UNIQUE means the default row collides with itself
    on upsert (one default per tenant). Postgres 15+.
    """

    __tablename__ = "tenant_user_caps"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "platform_user_id",
            name="uq_tenant_user_caps_tenant_user",
            postgresql_nulls_not_distinct=True,
        ),
        Index("tenant_user_caps_tenant_idx", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    platform_user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    cap_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class PaymentEvent(Base):
    """Stripe webhook dedup row. NOT a ledger.

    PK is the Stripe event id (text), not a surrogate UUID — see RESEARCH
    Pitfall 7. The compare-and-set in `try_claim_credit` relies on this.
    """

    __tablename__ = "payment_events"
    __table_args__ = (Index("payment_events_tenant_idx", "tenant_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    # payment_events.tenant_id is NOT NULL (migration applied).
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    credited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TenantLedger(Base):
    """Append-only per-tenant USD ledger. Balance = SUM(delta_usd).

    NEVER a mutable balance column — every credit (topup/trial) and debit
    (turn/clawback) is one immutable row. Idempotency_key is the on_conflict
    target so webhook replays / SSE replays never double-write.
    """

    __tablename__ = "tenant_ledger"
    __table_args__ = (
        Index("tenant_ledger_tenant_idx", "tenant_id"),
        Index("tenant_ledger_idem_idx", "idempotency_key", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    delta_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    reason: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # topup|trial|turn_debit|charge.refunded|charge.dispute.created
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    payment_event_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("payment_events.id", ondelete="SET NULL"), nullable=True
    )
    payment_intent: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AgentFile(Base):
    """Per-(tenant, agent, key) text blob storage."""

    __tablename__ = "agent_files"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "agent_id", "key", name="pk_agent_files"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class PendingFileDelete(Base):
    """Files-API object TTL queue.

    Records which MA-side Files-API objects to delete and when. The durable
    copy of a secret lives in `agent_files`; the uploaded Files-API object is
    disposable per session. No FK to tenants — deletion needs no tenant context.
    """

    __tablename__ = "pending_file_deletes"
    __table_args__ = (PrimaryKeyConstraint("file_id", name="pk_pending_file_deletes"),)

    file_id: Mapped[str] = mapped_column(Text, nullable=False)
    delete_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AgentRepoBinding(Base):
    """Per-(tenant, agent) git repo overlay binding."""

    __tablename__ = "agent_repo_binding"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "agent_id", name="pk_agent_repo_binding"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    default_branch: Mapped[str] = mapped_column(Text, nullable=False)
    ma_secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AgentMemoryStore(Base):
    """Per-(tenant, agent) MA memory store binding (agent memory feature)."""

    __tablename__ = "agent_memory_store"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id", name="pk_agent_memory_store"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    memory_store_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GitHubAppInstallation(Base):
    """GitHub App installation record.

    Tracks installation_id -> (account_login, repo_full_names) for minting
    installation tokens. Install-agnostic routing by repo.full_name.
    """

    __tablename__ = "github_app_installations"

    installation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_login: Mapped[str] = mapped_column(Text, nullable=False)
    repo_full_names: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class McpToken(Base):
    """JTI registry for per-agent MCP JWTs.

    Each minted token has one row. `revoked_at` is NULL while the token is
    live; `revoke_mcp_token` sets it atomically via UPDATE…RETURNING.

    `agent_id` is Text, not UUID — it stores the stringified derived UUID (A2)
    so the column matches the JWT claim shape exactly and stays decoupled from
    the UUID type constraint.

    Private to `daimon.core.stores.**` per the import-linter contract.
    """

    __tablename__ = "mcp_tokens"

    jti: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SlackBotToken(Base):
    """Encrypted per-workspace Slack bot token.

    PK is `team_id` (Slack workspace ID, e.g. "T0123456789"). The store layer
    takes/returns pre-encrypted `bytes` — it never sees the Fernet key.
    Private to `daimon.core.stores.**` per the import-linter contract.
    """

    __tablename__ = "slack_bot_tokens"

    team_id: Mapped[str] = mapped_column(Text, primary_key=True)
    encrypted_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_token: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)


class SlackUserToken(Base):
    """Encrypted per-(workspace, user) Slack xoxp token (user-token hybrid model).

    Composite PK (team_id, slack_user_id). The store layer takes/returns
    pre-encrypted bytes — it never sees the Fernet key. Private to
    `daimon.core.stores.**` per the import-linter contract.
    """

    __tablename__ = "slack_user_tokens"

    team_id: Mapped[str] = mapped_column(Text, primary_key=True)
    slack_user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    encrypted_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    scopes: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    encrypted_refresh_token: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SlackConnectPrompt(Base):
    """Once-ever marker: the first-mention connect nudge was shown to this user."""

    __tablename__ = "slack_connect_prompts"

    team_id: Mapped[str] = mapped_column(Text, primary_key=True)
    slack_user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    prompted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SlackTurnContext(Base):
    """Live "this account is running a turn in this channel" row.

    Inserted by the Slack adapter before run_turn, deleted in its finally.
    Read by MCP tool impls for the leak-policy destination check; readers
    ignore rows older than their TTL so a crashed process cannot poison the
    policy open — only closed.
    """

    __tablename__ = "slack_turn_contexts"
    __table_args__ = (Index("ix_slack_turn_contexts_tenant_account", "tenant_id", "account_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    thread_ts: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SlackEventDedup(Base):
    """Exactly-once gate for inbound Slack events.

    Composite natural key (team_id, channel, event_ts) — the logical Slack
    event key. Dedup MUST be on this key, NOT envelope_id: reconnect redelivers
    the same logical event with a NEW envelope_id.

    Unbounded for v1 (rows are tiny, no pruning job, no TTL, no delete
    cost on the ack path).

    Private to `daimon.core.stores.**` per the import-linter ORM contract.
    """

    __tablename__ = "slack_event_dedup"

    team_id: Mapped[str] = mapped_column(Text, primary_key=True)
    channel: Mapped[str] = mapped_column(Text, primary_key=True)
    event_ts: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
