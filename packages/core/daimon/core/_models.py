"""SQLAlchemy 2.0 ORM for the tenant-scoped schema.

This module owns the schema. Alembic's `env.py` reads `Base.metadata` from here.
Stores map these ORM objects to Pydantic at their boundary — callers of stores
never see ORM instances.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
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
    last_reconcile_error: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    ma_agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    watermark_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Untyped Text on purpose — no CHECK, so widening the vocabulary never needs
    # a lock on a hot table. Values:
    #   'live'       the caller's current session (the only status the
    #                caller-scoped lookup ever returns)
    #   'dead'       the MA session is gone (404 / archived); written only by
    #                the dead-session recovery path
    #   'superseded' replaced by a newer session carrying this one's work;
    #                `replaced_by_id` names the successor
    #   'retired'    deliberately abandoned on an explicit fresh start
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'live'"))
    # The configuration this session is actually running: an MA session freezes
    # its agent at create time, so the agent spec read at admission is not what
    # executes. A `daimon.core.session_snapshot.SessionSnapshot` serialized to
    # JSON. NULL on rows written before continuity existed.
    effective_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    identity_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    mutable_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Lineage of one continuing task across session replacements.
    predecessor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("thread_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("thread_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The workspace bundle mounted into this session, and how complete it was:
    # 'full' (checkpointed workspace), 'transcript' (conversation only) or
    # 'history' (platform reseed only). No CHECK; the vocabulary is pinned by
    # `stores.domain.TransferKind` so adapters render honest copy from it.
    transfer_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    transfer_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when the caller explicitly asked to start over; the next bind creates
    # the replacement first and only then retires this row.
    fresh_start_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # What this caller answered when asked whether uncommitted repository
    # changes should be copied into the successor's working files ('copy') or
    # left in the old checkout ('leave'). Held here because the question is
    # asked in one turn and the replacement it governs happens in a later one;
    # cleared when the row stops being live. No CHECK; the vocabulary is pinned
    # by `stores.domain.UnsavedWorkChoice`.
    pending_unsaved_work: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set while a turn is running, cleared when it reaches a terminal state.
    # Distinct from `status`, which is about the session mapping and stays
    # 'live' on every healthy thread forever. Slack needs the channel because a
    # Slack message is addressed by (channel, ts); Discord leaves it NULL since
    # a Discord message id is globally addressable on its own.
    active_turn_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_turn_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    active_turn_channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class SeededSkill(Base):
    """Content fingerprint for one seeded (`defaults/skills/**`) skill per tenant.

    Exists because MA gives skills no idempotence carrier of its own: they hold
    no metadata field, `latest_version` is an opaque counter rather than a
    content hash, and the API rejects any zip whose top-level directory differs
    from the SKILL.md `name:` — so the folder name cannot carry a digest either.
    Without a local fingerprint, `defaults apply` had no way to tell an edited
    skill from an unchanged one and adopted every MA match as-is, which left
    every `defaults/skills/**` edit undeliverable to an already-provisioned
    install.

    Distinct from `user_skills`, which fingerprints repo-synced skills per
    principal and per agent. Seeded skills have neither: they are one row per
    (tenant, skill name).
    """

    __tablename__ = "seeded_skills"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "name", name="pk_seeded_skills"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    anthropic_id: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AgentGithubBinding(Base):
    """Per-agent GitHub credential overlay. Day-1 always empty; populated by
    the working-repo binding path (`request_repo_binding`). Single credential
    per principal day-1, so no github_login discriminator.
    """

    __tablename__ = "agent_github_binding"

    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)


class AgentGoogleBinding(Base):
    """Per-agent Google Workspace identity overlay.

    Empty day-1; populated by an operator running `daimon agents bind-google
    <agent> <email> --scopes <scope>...`. Holds the email + scope set the
    token broker mints credentials for via domain-wide delegation against
    the tenant service account.
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


class AgentMcpCredential(Base):
    """Agent-scoped bearer token for an external MCP server attached to an agent.

    The MCP server itself is attached to the agent (`mcp_attach`), so every
    caller who mentions the agent gets its toolset. MA resolves the credential
    from the vault mounted on the session, and that vault is per
    (account, agent) — so a token written into only the attacher's vault leaves
    every other caller's turn failing at MCP init. Keeping the token here, at
    (tenant, agent), lets `create_session` mirror it into whichever vault the
    current caller has, the same way the per-agent PAT reaches the Copilot
    credential. MA credentials are write-only, so this is the only place the
    token can be re-read from.

    Encrypted with the same MultiFernet as the GitHub PAT.
    """

    __tablename__ = "agent_mcp_credentials"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "agent_id",
            "mcp_server_url",
            name="uq_agent_mcp_credentials_tenant_agent_url",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # No FK: agents live in MA, not the local DB (same rationale as UserSkill.agent_name).
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    mcp_server_url: Mapped[str] = mapped_column(Text, nullable=False)
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


class PendingPaymentClawback(Base):
    """A verified refund/dispute awaiting its Checkout credit row.

    These rows deliberately have no tenant foreign key: the tenant can only be
    resolved after the matching payment intent's topup has committed.
    """

    __tablename__ = "pending_payment_clawbacks"
    __table_args__ = (
        Index("pending_payment_clawbacks_intent_idx", "payment_intent", "received_at"),
        Index("pending_payment_clawbacks_received_idx", "received_at"),
    )

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    payment_intent: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_amount_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
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
    # Attribution, not authorization: who first created the key and who last
    # replaced its value. No FK to accounts.id, matching CredentialRequest's
    # rationale — these rows are erased by the platform-user-scoped helper,
    # not by an accounts.id cascade. Both stay NULL for pre-migration rows and
    # for writes with no acting person (a self-edit tool run headless).
    created_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    last_set_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
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
    proof_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    proof_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    proof_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AgentSkillRepoCredential(Base):
    """Per-(tenant, agent, repo) token for importing skills from a git repo.

    Deliberately NOT a column on `AgentRepoBinding`. That table is keyed
    (tenant_id, agent_id) because an agent has exactly one working repo — the
    one it clones — whereas it may import skills from any number of repos. If
    the skill token lived on the binding, enrolling a skill repo would
    re-point the working repo (and enrolling a second skill repo would evict
    the first). The two must never move each other, so they are two tables and
    this one carries `repo_url` in its primary key.

    `repo_url` is the canonical `owner/repo` form; the store normalizes it on
    every write and every read key. `path` is the in-repo subdirectory skills
    are read from, empty string for the repo root. The three proof columns
    mirror `AgentRepoBinding`'s: what was established about read access at
    enrollment time, and by whom.
    """

    __tablename__ = "agent_skill_repo_credentials"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "agent_id", "repo_url", name="pk_agent_skill_repo_credentials"
        ),
        Index("ix_agent_skill_repo_credentials_tenant_repo", "tenant_id", "repo_url"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    default_branch: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    ma_secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    proof_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    proof_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    proof_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
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
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "agent_id", name="pk_agent_memory_store"),)

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


class CredentialRequest(Base):
    """Short-lived handshake row backing the chat-initiated credential button.

    A row is minted when the agent posts a credential-request button in a
    thread; `used_at` plus `expires_at` make a click single-use and
    TTL-bounded — the atomic consume in `daimon.core.stores.credential_requests`
    is the actual single-use gate, this column is just its durable marker.
    `tenant_id` carries isolation and cascades on tenant teardown. `account_id`
    intentionally has no FK to `accounts.id`: these rows are erased through the
    platform-user-scoped erasure helper (mirroring how the OAuth handshake
    table `github_oauth_states` is erased), not through an accounts.id
    cascade.

    The provenance columns (`target_ma_agent_id`, `target_name`,
    `responder_name`, `requested_work`) record who the control was minted for
    and what was waiting on it, so a card rehydrated long after its turn still
    names its target. `replaces_updated_at` is the compare-and-set
    precondition the card promised; `outcome` is how the click actually ended.
    """

    __tablename__ = "credential_requests"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('env', 'env_file', 'mcp', 'mcp_oauth', 'repo', 'skill_repo')",
            name="ck_credential_requests_kind",
        ),
        UniqueConstraint("idempotency_key", name="uq_credential_requests_idempotency_key"),
    )

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    # Constrained by ck_credential_requests_kind; the vocabulary itself is
    # `daimon.core.credential_requests.CredentialRequestKind`.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    mcp_server_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    requester_platform_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    requested_work: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_ma_agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    responder_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    replaces_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Untyped Text with no CHECK, like `thread_sessions.pending_unsaved_work`:
    # the vocabulary is pinned by `CredentialRequestOutcome` in
    # `daimon.core.credential_requests`.
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class McpOAuthFlow(Base):
    """One in-flight MCP OAuth authorization, keyed by its `state`.

    Minted when the requester clicks an `mcp_oauth` card: the row holds the
    PKCE verifier the callback needs and, once `/oauth/mcp/start` has run
    discovery and dynamic client registration, the client Anthropic will
    refresh with. Person-scoped like `credential_requests`; it cascades from
    its request row, so the platform-user erasure that deletes the request
    takes the flow with it, and `tenant_id` cascades on tenant teardown.
    `client_secret_encrypted` is Fernet ciphertext and only ever set for a
    server that offers no public-client registration.
    """

    __tablename__ = "mcp_oauth_flows"
    # Every turn asks who in the tenant has signed in to the caller's server
    # URLs (`list_completed_grants`); the partial expression index is that
    # read's, the (tenant, agent) one predates it and stays for the older
    # per-agent lookups.
    __table_args__ = (
        Index("ix_mcp_oauth_flows_tenant_agent", "tenant_id", "agent_id"),
        Index(
            "ix_mcp_oauth_flows_tenant_url_completed",
            "tenant_id",
            text("rtrim(mcp_server_url, '/')"),
            postgresql_where=text("completed_at IS NOT NULL"),
        ),
    )

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    request_token: Mapped[str] = mapped_column(
        Text,
        ForeignKey("credential_requests.token", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    server_name: Mapped[str] = mapped_column(Text, nullable=False)
    mcp_server_url: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_endpoint_auth_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    authorization_endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # `used_at` is the replay gate, stamped before the callback knows whether
    # the person approved; `completed_at` is written only once their grant is
    # in the vault, which is what "connected" has to mean.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MessageFeedback(Base):
    """One row per (tenant, message, voter) thumbs-up/down reaction vote.

    `platform_user_id` is the real identity key, not `account_id`: a person
    who reacts without ever having taken a turn has no `accounts` row, so
    `account_id` resolves to NULL, and NULL cannot serve as an upsert conflict
    target. `account_id` is kept as a nullable best-effort foreign key so
    later reads can join, and so an account-scoped erasure reaches these rows
    directly.

    `ma_session_id` is a hint, not a join key. It is resolved at write time
    from the most recent live session in the reacted-to channel. When one
    caller owns the thread — the ordinary case, because threads are created
    per mention — it is exact. When several callers hold live sessions in the
    same thread it names the most recently created one, which may not be the
    session that authored the specific message. Any future analysis must
    treat it accordingly.

    `tenant_id` is NOT NULL and cascades on tenant teardown; a reaction with
    no resolvable tenant is dropped upstream rather than stored with a null
    isolation key.
    """

    __tablename__ = "message_feedback"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "message_id",
            "platform_user_id",
            name="uq_message_feedback_tenant_message_user",
        ),
        Index("message_feedback_tenant_idx", "tenant_id"),
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
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=True,
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    platform_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    ma_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    vote: Mapped[str] = mapped_column(Text, nullable=False)
    feedback_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WizardSession(Base):
    """Durable state for one multi-step wizard form (`daimon.core.wizard`).

    The process that posts a wizard's first screen (the MCP tool handler) is
    never the process that receives its taps (the Discord bot's interaction
    dispatcher), and a bot restart can land between any two taps on the same
    form — so `answers` and `current_step` are read from and written to this
    row on every tap. They are never derived from the message content and
    never held in process memory; the row IS the state.

    `account_id` deliberately carries a real (nullable) FK to `accounts.id`,
    unlike the neighbouring handshake table `credential_requests`, whose
    `account_id` has no FK at all. The schema-reflecting purge drift guard
    (`test_purge_covers_every_account_or_principal_scoped_table`) only
    notices a table if it has an `accounts.id` FK or a `principal_id`
    column — being invisible to it means nothing mechanically forces a
    future refactor to keep erasing these rows. Nullability does not exempt
    a column from being a foreign key for that check. `ondelete="SET NULL"`
    rather than `CASCADE` because erasure is actually driven by
    `delete_wizard_sessions_for_platform_user` (a platform-user-scoped
    delete, mirroring `credential_requests`), not by an accounts.id cascade;
    the FK exists for the drift guard's visibility, not as the deletion
    mechanism.

    `requester_platform_user_id` is the actual authorization key for every
    tap: the dispatcher compares it exactly against the tapping user's
    platform id before honouring any button or select on this row.
    """

    __tablename__ = "wizard_session"
    __table_args__ = (
        Index(
            "ix_wizard_session_tenant_requester",
            "tenant_id",
            "requester_platform_user_id",
        ),
        Index("ix_wizard_session_status_expires_at", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    requester_platform_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[str] = mapped_column(Text, nullable=False)
    # dict[str, Any]: a serialized WizardSpec, validated back into the real
    # Pydantic model at read time — not a typing shortcut.
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    answers: Mapped[dict[str, list[str]]] = mapped_column(JSONB, nullable=False)
    current_step: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SlackEventDedup(Base):
    """Exactly-once gate for inbound Slack events.

    Composite natural key (team_id, channel, event_ts) — the logical Slack
    event key. Dedup MUST be on this key, NOT envelope_id: reconnect redelivers
    the same logical event with a NEW envelope_id.

    Rows are pruned by age on a schedule: `daimon.core.slack_event_dedup_sweep`
    deletes rows older than a 7-day retention window on every scheduler tick.
    The ack path still pays no delete cost — pruning is out-of-band,
    never per-turn. The window is chosen to outlive Slack's own
    redelivery schedule by a wide margin, so a prune can never re-admit a
    duplicate event.

    Private to `daimon.core.stores.**` per the import-linter ORM contract.
    """

    __tablename__ = "slack_event_dedup"

    team_id: Mapped[str] = mapped_column(Text, primary_key=True)
    channel: Mapped[str] = mapped_column(Text, primary_key=True)
    event_ts: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FileUpload(Base):
    """A file the agent produced, staged for delivery as a chat attachment.

    Rows are created empty by the mint step and filled by a single PUT from the
    agent's sandbox, so the bytes never transit the model's token stream. The
    row lives in Postgres rather than on an instance's disk because ``mcp``
    runs multiple Cloud Run instances with no session affinity: the PUT and the
    later read are separate requests and routinely land on different instances.

    ``upload_token`` is the single-use capability that authorizes the PUT and is
    cleared once the bytes land; ``content`` is NULL until then.

    Private to `daimon.core.stores.**` per the import-linter ORM contract.
    """

    __tablename__ = "file_uploads"
    __table_args__ = (
        Index("ix_file_uploads_upload_token", "upload_token", unique=True),
        Index("ix_file_uploads_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    upload_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    display_filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SupportEscalation(Base):
    """One row per human-support request. The rows ARE the credit ledger.

    There is no counter column and no `credits_remaining`: remaining is the
    configured allowance minus the number of rows for that (tenant,
    platform_user_id). A counter would be a second source of truth that can
    drift from the rows it summarises, and it would need to be written in the
    same transaction as the insert anyway -- which counting already is.

    `platform_user_id` is the identity key for the same reason it is on
    `message_feedback`: somebody can ask for help without ever having taken a
    turn, so `account_id` may be NULL. Credits are per user WITHIN a tenant,
    so the count predicate is always both columns together.

    `delivered_at` is NULL until an operator DM actually lands. The row is
    committed BEFORE any delivery is attempted, so a request whose every
    operator DM bounces -- closed DMs are the ordinary failure -- survives as
    an undelivered row instead of vanishing. That ordering is the whole point
    of the column: it is the difference between a support request that can be
    swept up later and one that was silently dropped on a paying trial.

    Deliberately NO uniqueness constraint on (tenant, message, user): asking
    for help twice about the same answer is legitimate and each request spends
    its own credit.
    """

    __tablename__ = "support_escalations"
    __table_args__ = (
        Index(
            "support_escalations_tenant_user_idx",
            "tenant_id",
            "platform_user_id",
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
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=True,
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    platform_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[str] = mapped_column(Text, nullable=False)
    ma_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HubOAuthKv(Base):
    """Backing table for the hub login proxies' key-value store.

    Column shape is dictated by ``key_value.aio.stores.postgresql.PostgreSQLStore``,
    which reads and writes this table directly over asyncpg; daimon only ever
    deletes expired rows through ``stores.hub_oauth_kv``. Collections are
    prefixed per platform (``slack__``, ``discord__``) by the adapter.

    Rows hold encrypted upstream access tokens but are keyed by the proxy's
    own identifiers, not by account, so an account purge cannot address them.
    Retention is bounded instead: every row carries a TTL no longer than the
    upstream token's lifetime and the scheduled sweep deletes it once expired.
    """

    __tablename__ = "hub_oauth_kv"

    collection: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    ttl: Mapped[float | None] = mapped_column(Double, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class ThreadParticipationScope(Base):
    """An explicit organic-participation setting at one scope.

    One row per (tenant, platform, scope, scope_id); the workspace row uses an
    empty scope_id because tenant_id already names it. No row means "inherit",
    so the table only ever holds deliberate choices, and a deployment where
    nobody asked stays empty. `daimon.core.thread_participation.resolve_participation`
    turns these rows into an effective mode.
    """

    __tablename__ = "thread_participation_scopes"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "platform", "scope", "scope_id", name="pk_thread_participation_scopes"
        ),
        CheckConstraint(
            "scope IN ('workspace', 'channel', 'thread')",
            name="ck_thread_participation_scopes_scope",
        ),
        CheckConstraint(
            "mode IN ('on', 'off', 'disabled')", name="ck_thread_participation_scopes_mode"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    platform: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    scope_id: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ThreadAutoResponse(Base):
    """One row per reply the agent posted in a thread unprompted.

    The rows ARE the rate-limit ledger, the same way `support_escalations`
    rows are the credit ledger: the hourly cap counts rows in the window.
    There is no counter column to drift.
    """

    __tablename__ = "thread_auto_responses"
    __table_args__ = (
        Index(
            "thread_auto_responses_thread_idx",
            "tenant_id",
            "platform",
            "thread_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ThreadAgentBinding(Base):
    """Shared conversation routing, independent of each participant's session."""

    __tablename__ = "thread_agent_bindings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "platform",
            "parent_channel_id",
            "thread_id",
            name="uq_thread_agent_bindings_location",
        ),
        CheckConstraint("kind IN ('setup', 'handoff')", name="ck_thread_agent_bindings_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    parent_channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'setup'"))
    responder_ma_agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    responder_name: Mapped[str] = mapped_column(Text, nullable=False)
    configuration_target_ma_agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    configuration_target_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    creator_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TurnOrigin(Base):
    """Expiring control authority and target snapshot for one caller's running turn."""

    __tablename__ = "turn_origins"
    __table_args__ = (Index("turn_origins_expiry_idx", "expires_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    parent_channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    responder_ma_agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    responder_name: Mapped[str] = mapped_column(Text, nullable=False)
    configuration_target_ma_agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    configuration_target_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_setup: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SessionPreparation(Base):
    """One in-flight attempt to move a caller's task onto a new configuration.

    The row is the resume point: a replacement spans a billed checkpoint turn,
    a file upload and a session create, and a process that dies between them
    must not repeat the billed part. `stage` says how far the last attempt got;
    `UNIQUE(mapping_id, target_fingerprint)` means retrying the *same* target
    finds the same row, while a target that changed under us starts a new one.
    """

    __tablename__ = "session_preparations"
    __table_args__ = (
        UniqueConstraint(
            "mapping_id",
            "target_fingerprint",
            name="uq_session_preparations_mapping_fingerprint",
        ),
        CheckConstraint(
            "stage IN ('decided', 'checkpointed', 'uploaded', 'created', 'completed', 'failed')",
            name="ck_session_preparations_stage",
        ),
        Index("session_preparations_mapping_idx", "mapping_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    mapping_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("thread_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    transfer_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    transfer_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_mapping_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("thread_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TaskContinuation(Base):
    """A queued first turn for an agent a task was just handed to.

    `idempotency_key` plus the `status` ladder is the at-most-once guarantee:
    a dispatcher claims a row with a single conditional UPDATE, so a restart
    mid-dispatch can never post the same continuation twice.
    """

    __tablename__ = "task_continuations"
    __table_args__ = (
        CheckConstraint(
            "reason IN ('task_handoff', 'private_input_applied')",
            name="ck_task_continuations_reason",
        ),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'delivered', 'skipped')",
            name="ck_task_continuations_status",
        ),
        UniqueConstraint("idempotency_key", name="uq_task_continuations_idempotency_key"),
        Index(
            "task_continuations_thread_idx",
            "tenant_id",
            "platform",
            "thread_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    parent_channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    requester_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    requester_external_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    target_ma_agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    target_name: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL means the handoff carried no work to continue: the switch is
    # recorded, and nothing is ever dispatched (never a billed turn).
    requested_work: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
