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


class AgentAvatar(Base):
    __tablename__ = "agent_avatars"
    __table_args__ = (
        CheckConstraint("source IN ('default', 'upload')", name="ck_agent_avatars_source"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    agent_name: Mapped[str] = mapped_column(Text, primary_key=True)
    token: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    png: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    png_128: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    png_512: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    previous_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_png: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    previous_png_128: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    previous_png_512: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    face_combo: Mapped[str | None] = mapped_column(Text, nullable=True)
    face_thumbnail: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_tenants_platform_external_id"),
        CheckConstraint(
            "funding_mode IN ('prepaid', 'operator_funded')", name="ck_tenants_funding_mode"
        ),
        CheckConstraint("turn_cap IS NULL OR turn_cap > 0", name="ck_tenants_turn_cap"),
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
    funding_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'prepaid'")
    )
    turn_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    # The platform roles the member held on their last chat turn, refreshed
    # with `role`, so MCP calls can match a channel admin role grant without a
    # live platform lookup. Empty on platforms without roles.
    platform_role_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    # A person from another organisation (a Teams shared channel's external
    # participant), refreshed with `role`; such an account is never an admin.
    is_external: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
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


class TenantAccessPolicyRecord(Base):
    """A tenant's access policy (`daimon.core.access_policy.TenantAccessPolicy` as JSON).

    No row means the open default, so tenants that never set one are unchanged.
    """

    __tablename__ = "tenant_access_policies"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", name="pk_tenant_access_policies"),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_tenant_access_policies_tenants",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


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
    # A server admin set `agent_name`, as decided when it was set: that makes the
    # agent this channel's admins' to administer (`daimon.core.authz.channel_admin_holds`).
    agent_name_set_by_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'agent'"))


class ChannelAdmin(Base):
    """The roles and users who administer one channel, on top of server admins.

    No row means nobody beyond the server or workspace admins, which is every
    tenant's starting state.
    """

    __tablename__ = "channel_admins"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "platform", "channel_id", name="pk_channel_admins"),
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_channel_admins_tenants"
        ),
        ForeignKeyConstraint(
            ["updated_by_account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
            name="fk_channel_admins_updated_by_account_id",
        ),
        CheckConstraint(
            "platform IN ('discord', 'slack', 'teams')", name="ck_channel_admins_platform"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    platform: Mapped[str] = mapped_column(Text)
    channel_id: Mapped[str] = mapped_column(Text)
    role_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    user_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    updated_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Routine(Base):
    __tablename__ = "routines"
    __table_args__ = (
        Index(
            "routines_due_idx",
            "next_fire_at",
            postgresql_where=text("enabled AND next_fire_at IS NOT NULL"),
        ),
        Index("routines_tenant_idx", "tenant_id"),
        CheckConstraint(
            "catch_up_policy IN ('skip', 'run-once')", name="ck_routines_catch_up_policy"
        ),
        CheckConstraint(
            "destination_kind IS NULL OR destination_kind IN ('channel', 'thread')",
            name="ck_routines_destination_kind",
        ),
        CheckConstraint(
            "(destination_kind IS NULL) = (destination_id IS NULL)",
            name="ck_routines_destination_pair",
        ),
        CheckConstraint(
            "delivery_status IS NULL OR delivery_status IN "
            "('pending', 'claimed', 'delivered', 'skipped')",
            name="ck_routines_delivery_status",
        ),
        Index(
            "routines_delivery_due_idx",
            "delivery_status",
            postgresql_where=text("delivery_status IN ('pending', 'claimed')"),
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
    created_by_user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    cron_expr: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="UTC")
    trigger_message: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    catch_up_policy: Mapped[str] = mapped_column(Text, nullable=False, server_default="skip")
    last_skipped_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_skipped_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_result_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # FEAT-085: optional place the result goes, and the outbox that posts it.
    destination_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    destination_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivery_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The text a pending post carries: that fire's result, copied so a writer
    # that only knows `last_result_tail` cannot change what gets posted.
    delivery_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Channel the routine's spend is attributed to and budget-gated by: its
    # destination's parent channel, resolved when the destination is set.
    channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    # The channel the session runs for, recorded at creation: a thread's parent,
    # or the channel a DM was moved from (`Admission.budget_channel_id`). NULL
    # when unknown; agent reach then counts the session as possibly anywhere.
    channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    seal_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
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
    github_key_restart_notice: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
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


class TurnCardIntent(Base):
    """Durable intent for a turn's initial status card."""

    __tablename__ = "turn_card_intents"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "turn_token",
            name="uq_turn_card_intents_tenant_token",
        ),
        CheckConstraint(
            "status IN ('prepared', 'posted', 'retired', 'unrecoverable')",
            name="ck_turn_card_intents_status",
        ),
        CheckConstraint(
            "(status = 'prepared' AND message_id IS NULL) OR "
            "(status = 'posted' AND message_id IS NOT NULL AND message_id <> '') OR "
            "(status IN ('retired', 'unrecoverable') AND (message_id IS NULL OR message_id <> ''))",
            name="ck_turn_card_intents_message_state",
        ),
        Index("ix_turn_card_intents_recovery", "platform", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)
    turn_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Slack addresses a message by (channel, timestamp); Discord needs no channel.
    channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # NULL while prepared: the platform accepted no response yet, or the
    # process died before it could persist the response's message identifier.
    message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'prepared'"))
    recovery_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
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
    - source is `repo` for a skill-repo sync and `upload` for one skill added by hand.
      An upload keeps `source_repo_url` empty so repo orphan and removal passes never
      touch it; `origin` says where it came from instead.
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
        ForeignKeyConstraint(
            ["added_by_account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
            name="fk_user_skills_added_by_account_id",
        ),
        CheckConstraint("source IN ('repo', 'upload')", name="ck_user_skills_source"),
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
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="repo")
    origin: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    added_by_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
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
    # Parent channel of the turn or tool call that spent it; NULL when unknown.
    channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)


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
        Index(
            "tenant_ledger_tenant_channel_idx",
            "tenant_id",
            "channel_id",
            "occurred_at",
            postgresql_where=text("channel_id IS NOT NULL"),
        ),
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
    # topup|manual_credit|trial|promo_credit|promo_expiry|promo_expiry_refund|*_debit|charge.*;
    # see billing.md
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    payment_event_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("payment_events.id", ondelete="SET NULL"), nullable=True
    )
    payment_intent: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Set on debits only: the parent channel whose budget the spend counts against.
    channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)


class ChannelSkill(Base):
    """One extra skill a channel's sessions add to the agent's own, at a pinned version.

    `owner_agent_name` is set for a skill uploaded to one agent: it applies
    only while that agent answers. No rows means nothing extra.
    """

    __tablename__ = "channel_skills"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "platform", "channel_id", "skill_id", name="pk_channel_skills"
        ),
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_channel_skills_tenants"
        ),
        ForeignKeyConstraint(
            ["added_by_account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
            name="fk_channel_skills_added_by_account_id",
        ),
        CheckConstraint(
            "platform IN ('discord', 'slack', 'teams')", name="ck_channel_skills_platform"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    platform: Mapped[str] = mapped_column(Text)
    channel_id: Mapped[str] = mapped_column(Text)
    skill_id: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    owner_agent_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_by_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AgentCreationChannel(Base):
    """The channel an agent was created for, by a channel admin of it, from there.

    Its admins administer the agent while it stays local to their channels
    (`daimon.core.authz.channel_admin_holds`). No row: nobody's but server admins'.
    """

    __tablename__ = "agent_creation_channels"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "ma_agent_id", name="pk_agent_creation_channels"),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_agent_creation_channels_tenants",
        ),
        CheckConstraint(
            "platform IN ('discord', 'slack', 'teams')", name="ck_agent_creation_channels_platform"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    ma_agent_id: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DiscordAgentRole(Base):
    """A role created by this bot for one tenant agent; role names are not identity."""

    __tablename__ = "discord_agent_roles"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "ma_agent_id", name="pk_discord_agent_roles"),
        UniqueConstraint("tenant_id", "role_id", name="uq_discord_agent_roles_role"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    ma_agent_id: Mapped[str] = mapped_column(Text)
    role_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)


class ChannelBudget(Base):
    """A spend limit on one channel. No row = no limit.

    Spend is the channel's ledger debits inside the window: `monthly` is the
    current UTC calendar month, `total` everything since `starts_at` (or ever),
    `fixed` the `[starts_at, ends_at)` range, outside which it does not gate.
    """

    __tablename__ = "channel_budgets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "platform", "channel_id", name="uq_channel_budgets_tenant_channel"
        ),
        CheckConstraint("limit_usd >= 0", name="ck_channel_budgets_limit"),
        CheckConstraint(
            "\"window\" IN ('monthly', 'total', 'fixed')", name="ck_channel_budgets_window"
        ),
        CheckConstraint(
            "(\"window\" = 'fixed' AND starts_at IS NOT NULL AND ends_at IS NOT NULL "
            "AND starts_at < ends_at) "
            "OR (\"window\" = 'total' AND ends_at IS NULL) "
            "OR (\"window\" = 'monthly' AND starts_at IS NULL AND ends_at IS NULL)",
            name="ck_channel_budgets_bounds",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    limit_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    window: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    set_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    # The window whose exhausted notice went out; cleared when the budget is set or raised.
    exhausted_notice_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PromoCode(Base):
    """Deployment-level promo code an admin redeems for tenant credit.

    Only the sha256 of the normalized code is stored: the operator sees the
    code once, at creation. A `timed` code's credit exists only inside its
    credit window; a `credit` code's does not expire.
    """

    __tablename__ = "promo_codes"
    __table_args__ = (
        Index("promo_codes_code_hash_idx", "code_hash", unique=True),
        CheckConstraint(
            "amount_usd > 0 AND amount_usd <= 999999.99", name="ck_promo_codes_amount_range"
        ),
        CheckConstraint("kind IN ('credit', 'timed')", name="ck_promo_codes_kind"),
        CheckConstraint(
            "(kind = 'credit' AND credit_starts_at IS NULL AND credit_ends_at IS NULL)"
            " OR (kind = 'timed' AND credit_starts_at < credit_ends_at)",
            name="ck_promo_codes_credit_window",
        ),
        CheckConstraint(
            "redeem_starts_at IS NULL OR redeem_ends_at IS NULL"
            " OR redeem_starts_at < redeem_ends_at",
            name="ck_promo_codes_redeem_window",
        ),
        CheckConstraint(
            "max_redemptions IS NULL OR max_redemptions > 0",
            name="ck_promo_codes_max_redemptions",
        ),
        CheckConstraint(
            "redeemed_count >= 0"
            " AND (max_redemptions IS NULL OR redeemed_count <= max_redemptions)",
            name="ck_promo_codes_redeemed_count",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    credit_starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    credit_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redeem_starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    redeem_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_redemptions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    redeemed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PromoRedemption(Base):
    """One tenant's redemption of one promo code, and its grant/expiry state.

    `granted_at` is when the credit reached the ledger (a timed code redeemed
    before its window waits for the scheduler); `expired_at`/`expired_usd`
    record the unspent remainder a timed code removed at its window's end,
    and `reconciled_at` when late-recorded spend inside the window was
    credited back out of that remainder.
    The redeeming account is attribution only, severed by account erasure.
    """

    __tablename__ = "promo_redemptions"
    __table_args__ = (
        UniqueConstraint("promo_code_id", "tenant_id", name="uq_promo_redemptions_code_tenant"),
        Index("promo_redemptions_tenant_idx", "tenant_id"),
        CheckConstraint(
            "expired_usd IS NULL OR expired_usd >= 0", name="ck_promo_redemptions_expired_usd"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    promo_code_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("promo_codes.id", ondelete="RESTRICT"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    redeemed_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    redeemed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PromoRedeemFailure(Base):
    """One refused redemption attempt, counted to throttle guessing per tenant."""

    __tablename__ = "promo_redeem_failures"
    __table_args__ = (Index("promo_redeem_failures_tenant_idx", "tenant_id", "attempted_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentFile(Base):
    """Per-(tenant, agent, key) encrypted environment value storage."""

    __tablename__ = "agent_files"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id", "key", name="pk_agent_files"),
        CheckConstraint("encoding IN ('plain', 'fernet_v1')", name="ck_agent_files_encoding"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    encoding: Mapped[str] = mapped_column(Text, nullable=False, server_default="plain")
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
    __table_args__ = (
        CheckConstraint("app IN ('legacy', 'github_app')", name="ck_github_app_installations_app"),
    )

    installation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_login: Mapped[str] = mapped_column(Text, nullable=False)
    repo_full_names: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    app: Mapped[str] = mapped_column(Text, nullable=False, server_default="legacy")
    account_id: Mapped[int | None] = mapped_column(BigInteger)
    account_type: Mapped[str | None] = mapped_column(Text)
    repository_selection: Mapped[str | None] = mapped_column(Text)
    permissions: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class GitHubInstallationReconciliation(Base):
    """Coalesced refresh work and generation fence per GitHub installation."""

    __tablename__ = "github_installation_reconciliations"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'running', 'done')",
            name="ck_github_installation_reconciliations_state",
        ),
        Index("ix_github_installation_reconciliations_due", "state", "available_at", "created_at"),
    )

    installation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    claimed_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class GitHubInstallationDelivery(Base):
    """Finite idempotency receipts for installation webhooks."""

    __tablename__ = "github_installation_deliveries"

    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GitHubPushResync(Base):
    """Coalesced durable work for one GitHub repository ref."""

    __tablename__ = "github_push_resyncs"
    __table_args__ = (
        UniqueConstraint("repo_full_name", "ref", name="uq_github_push_resyncs_repo_ref"),
        CheckConstraint(
            "state IN ('pending', 'running', 'done')",
            name="ck_github_push_resyncs_state",
        ),
        Index("ix_github_push_resyncs_due", "state", "available_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    repo_full_name: Mapped[str] = mapped_column(Text, nullable=False)
    ref: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_id: Mapped[str] = mapped_column(Text, nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    claimed_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class GitHubPushDelivery(Base):
    """Finite-retention idempotency receipts for verified push deliveries."""

    __tablename__ = "github_push_deliveries"
    __table_args__ = (Index("ix_github_push_deliveries_received_at", "received_at"),)

    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)
    repo_full_name: Mapped[str] = mapped_column(Text, nullable=False)
    ref: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class McpToken(Base):
    """JTI registry for MCP JWTs that can be revoked: agent keys, operator and CLI tokens.

    Each minted token has one row. `revoked_at` is NULL while the token is
    live; `revoke_mcp_token` sets it atomically via UPDATE…RETURNING.

    `agent_id` is Text, not UUID — it stores the stringified derived UUID (A2)
    so the column matches the JWT claim shape exactly and stays decoupled from
    the UUID type constraint. Only `kind = 'agent'` rows carry one.

    Private to `daimon.core.stores.**` per the import-linter contract.
    """

    __tablename__ = "mcp_tokens"
    __table_args__ = (
        CheckConstraint("kind IN ('agent', 'operator', 'cli')", name="ck_mcp_tokens_kind"),
        CheckConstraint("(kind = 'agent') = (agent_id IS NOT NULL)", name="ck_mcp_tokens_agent_id"),
        CheckConstraint(
            "kind = 'operator' OR (scopes = '{}' AND max_issued_usd IS NULL)",
            name="ck_mcp_tokens_operator_fields",
        ),
        CheckConstraint(
            "issued_usd >= 0 AND (max_issued_usd IS NULL OR max_issued_usd > 0)",
            name="ck_mcp_tokens_issued",
        ),
        CheckConstraint(
            "(platform IS NULL) = (channel_id IS NULL) AND (channel_id IS NULL OR kind = 'agent')",
            name="ck_mcp_tokens_channel",
        ),
    )

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
    agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'agent'"))
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_issued_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    issued_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default=text("0")
    )
    # The channel the token was minted in, whose calls then run inside it; both
    # NULL for a token bound to no channel.
    platform: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)


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


class TeamsInstallation(Base):
    """A team daimon's bot is installed in, and the Entra group Graph names it by.

    Recorded from the team's activities and install events; deleted when the
    bot is removed from the team. The MCP server lists and resolves channels
    through it, since nothing app-only lists the teams an app is in.
    """

    __tablename__ = "teams_installations"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "team_id", name="pk_teams_installations"),
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_teams_installations_tenants"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    #: The Bot Framework team id, which is also the General channel's id.
    team_id: Mapped[str] = mapped_column(Text)
    group_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TeamsChannelSite(Base):
    """A Teams channel's Files folder, on a SharePoint site granted to daimon.

    Written when an admin turns files on from the channel: their sign-in finds
    the folder, which `Sites.Selected` cannot. A private or shared channel's
    folder is on a site of its own, so this row is the only way to it.
    """

    __tablename__ = "teams_channel_sites"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "channel_id", name="pk_teams_channel_sites"),
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_teams_channel_sites_tenants"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    channel_id: Mapped[str] = mapped_column(Text)
    group_id: Mapped[str] = mapped_column(Text, nullable=False)
    site_id: Mapped[str] = mapped_column(Text, nullable=False)
    drive_id: Mapped[str] = mapped_column(Text, nullable=False)
    folder_id: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


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
    # Reason codes picked in the "What went wrong?" form
    # (`daimon.core.message_feedback.FEEDBACK_REASONS`); NULL when none were.
    feedback_reasons: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
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
    # Answered as from another organisation, stored or for this turn only.
    is_external: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set by `archive_thread` on this turn's own thread; the adapter archives
    # the thread after the turn's last post.
    archive_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    """A queued turn owed to a thread: a handoff's first turn, or a later wake.

    `idempotency_key` plus the `status` ladder is the at-most-once guarantee:
    a dispatcher claims a row with a single conditional UPDATE, so a restart
    mid-dispatch can never post the same continuation twice.

    The lease columns make it a durable wake queue (`daimon.core.continuity.
    wakes`): a claim carries an owner and an expiry, and `started_at` is the
    fence committed just before the turn begins. An expired claim is retried
    only while `started_at` is NULL; once it is set, nobody can tell whether
    the turn ran, so the row is settled instead of run again.
    """

    __tablename__ = "task_continuations"
    __table_args__ = (
        CheckConstraint(
            "reason IN ('task_handoff', 'private_input_applied', 'timer', 'github_access_ready')",
            name="ck_task_continuations_reason",
        ),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'delivered', 'skipped', 'cancelled')",
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
        Index(
            "task_continuations_due_idx",
            "platform",
            "status",
            "available_at",
            postgresql_where=text("available_at IS NOT NULL"),
        ),
        # Agent reach reads the wakes still owed to an agent.
        Index(
            "task_continuations_waiting_idx",
            "tenant_id",
            "target_name",
            postgresql_where=text("status IN ('pending', 'claimed')"),
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
    # NULL: dispatched only at the tail of the next turn in the thread (a
    # handoff). Set: also polled, and not claimable before this instant.
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class TurnOutcome(Base):
    """Content-free terminal diagnostics; one UUID per logical turn."""

    __tablename__ = "turn_outcomes"
    __table_args__ = (
        CheckConstraint(
            "origin IN ('chat', 'routine', 'relay', 'handoff')", name="ck_turn_outcomes_origin"
        ),
        CheckConstraint(
            "reason IN ('completed', 'interrupted', 'interrupt_timeout', "
            "'connection_lost', 'upstream', 'rate_limited', 'session_terminated', "
            "'mcp_degraded_empty', 'retrying_unsettled', 'requires_action', 'ceiling', "
            "'recovery_cancelled', 'recovery_failed', 'reducer_bug', "
            "'admission_balance_depleted', 'admission_cap_exceeded', "
            "'admission_channel_budget_exceeded', 'admission_channel_protected', "
            "'admission_agent_pinned_elsewhere', 'admission_channel_isolated', "
            "'admission_denied', "
            "'admission_concurrency_shed', 'missing_config', 'resolver_miss', "
            "'session_preparation_failed', 'session_busy', 'session_agent_mismatch', "
            "'unknown')",
            name="ck_turn_outcomes_reason",
        ),
        CheckConstraint("duration_ms >= 0", name="ck_turn_outcomes_duration"),
        Index("ix_turn_outcomes_tenant_started", "tenant_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str | None] = mapped_column(Text)
    thread_id: Mapped[str | None] = mapped_column(Text)
    agent_id: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recovered: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_class: Mapped[str | None] = mapped_column(Text)
    release: Mapped[str] = mapped_column(Text, nullable=False)
    usage_refs: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False)

    # SYS-066: NULL distinguishes historical outcomes from measured zero usage.
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_read_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_creation_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    model_calls: Mapped[int | None] = mapped_column(Integer)
    model_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    unpriced_calls: Mapped[int | None] = mapped_column(Integer)
    billing_posture: Mapped[str | None] = mapped_column(Text)


class TenantGitHubRepo(Base):
    __tablename__ = "tenant_github_repos"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "repo_id"),
        CheckConstraint("max_access IN ('read', 'write')"),
        CheckConstraint("status IN ('active', 'suspended', 'revoked')"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    repo_id: Mapped[int] = mapped_column(BigInteger)
    owner_id: Mapped[int] = mapped_column(BigInteger)
    installation_id: Mapped[int] = mapped_column(BigInteger)
    repo_full_name: Mapped[str] = mapped_column(Text)
    max_access: Mapped[str] = mapped_column(Text)
    authorized_by_github_user_id: Mapped[int] = mapped_column(BigInteger)
    authorized_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )
    authorized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[str] = mapped_column(Text, server_default="active")
    status_reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, server_default="1")


class GitHubConnectInvitation(Base):
    __tablename__ = "github_connect_invitations"
    __table_args__ = (
        CheckConstraint(
            "activation_status IS NULL OR activation_status IN ('activated', 'update_pending')",
            name="ck_github_connect_invitation_activation",
        ),
    )
    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    requester_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE")
    )
    workspace_label: Mapped[str] = mapped_column(Text)
    requester_label: Mapped[str] = mapped_column(Text)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    agent_name: Mapped[str | None] = mapped_column(Text)
    operator_issued: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    activation_status: Mapped[str | None] = mapped_column(Text)
    connected_repo_count: Mapped[int | None] = mapped_column(Integer)
    encrypted_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GitHubConnectFlow(Base):
    __tablename__ = "github_connect_flows"
    state_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    invitation_hash: Mapped[str] = mapped_column(
        Text, ForeignKey("github_connect_invitations.token_hash", ondelete="CASCADE")
    )
    cookie_hash: Mapped[str] = mapped_column(Text)
    encrypted_verifier: Mapped[bytes] = mapped_column(LargeBinary)
    encrypted_invitation_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    encrypted_user_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    github_user_id: Mapped[int | None] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GitHubConnectRequest(Base):
    __tablename__ = "github_connect_requests"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "requester_account_id", "agent_id", name="uq_github_connect_request"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    requester_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AgentGitHubGrant(Base):
    __tablename__ = "agent_github_grants"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id", "repo_id"),
        ForeignKeyConstraint(
            ["tenant_id", "repo_id"],
            ["tenant_github_repos.tenant_id", "tenant_github_repos.repo_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("baseline_access IN ('none', 'read', 'write')"),
        CheckConstraint("ceiling_access IN ('read', 'write')"),
        CheckConstraint(
            "baseline_access = 'none' OR baseline_access = 'read' OR ceiling_access = 'write'"
        ),
    )
    # max_access belongs to tenant_github_repos, so the grant-writing store
    # must enforce ceiling_access <= max_access in the same transaction.
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    repo_id: Mapped[int] = mapped_column(BigInteger)
    baseline_access: Mapped[str] = mapped_column(Text)
    ceiling_access: Mapped[str] = mapped_column(Text)
    staged: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    mount_path: Mapped[str | None] = mapped_column(Text)
    is_working_repo: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    granted_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    version: Mapped[int] = mapped_column(Integer, server_default="1")


class AgentGitHubGrantDraft(Base):
    __tablename__ = "agent_github_grant_drafts"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id", "repo_id"),
        ForeignKeyConstraint(
            ["tenant_id", "repo_id"],
            ["tenant_github_repos.tenant_id", "tenant_github_repos.repo_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("operation IN ('upsert', 'remove')"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    repo_id: Mapped[int] = mapped_column(BigInteger)
    operation: Mapped[str] = mapped_column(Text)
    baseline_access: Mapped[str | None] = mapped_column(Text)
    ceiling_access: Mapped[str | None] = mapped_column(Text)
    is_working_repo: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    granted_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )


class GitHubNewRepoNotice(Base):
    """A new installation repository awaiting a workspace-admin announcement."""

    __tablename__ = "github_new_repo_notices"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "installation_id", "repo_full_name"),)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    installation_id: Mapped[int] = mapped_column(BigInteger)
    repo_full_name: Mapped[str] = mapped_column(Text)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GitHubRemovalNotice(Base):
    """One admin notice after GitHub removes an installation."""

    __tablename__ = "github_removal_notices"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "installation_id"),)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    installation_id: Mapped[int] = mapped_column(BigInteger)
    account_login: Mapped[str] = mapped_column(Text)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GitHubAccessRequest(Base):
    """One unfinished GitHub access decision for an asker in a thread."""

    __tablename__ = "github_access_requests"
    __table_args__ = (
        Index(
            "uq_github_access_request_open_thread_asker_agent",
            "tenant_id",
            "platform",
            "thread_id",
            "requester_account_id",
            "agent_id",
            unique=True,
            postgresql_where=text("status IN ('open', 'waiting_github')"),
        ),
        CheckConstraint(
            "status IN ('open', 'waiting_github', 'ready', 'cancelled', 'declined', 'expired')"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    requester_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE")
    )
    requester_platform_user_id: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    parent_channel_id: Mapped[str] = mapped_column(Text)
    thread_id: Mapped[str] = mapped_column(Text)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    ma_agent_id: Mapped[str] = mapped_column(Text)
    agent_name: Mapped[str] = mapped_column(Text)
    repo_names: Mapped[list[str]] = mapped_column(JSONB)
    required_ability: Mapped[str] = mapped_column(Text, server_default="read")
    approved_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )
    requested_work: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    admin_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiry_notice_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GitHubAccessRequestDelivery(Base):
    """One private card per request and recipient, editable on later repo needs."""

    __tablename__ = "github_access_request_deliveries"
    __table_args__ = (PrimaryKeyConstraint("request_id", "recipient_account_id"),)
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("github_access_requests.id", ondelete="CASCADE")
    )
    recipient_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE")
    )
    platform_user_id: Mapped[str] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentGitHubMode(Base):
    __tablename__ = "agent_github_mode"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id"),
        CheckConstraint("mode IN ('legacy', 'app')"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    mode: Mapped[str] = mapped_column(Text, server_default="legacy")


class GitHubUserLink(Base):
    __tablename__ = "github_user_links"
    __table_args__ = (CheckConstraint("status IN ('active', 'broken')"),)
    github_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    login: Mapped[str] = mapped_column(Text)
    encrypted_access_token: Mapped[bytes] = mapped_column(LargeBinary)
    encrypted_refresh_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_generation: Mapped[int] = mapped_column(Integer, server_default="1")
    link_generation: Mapped[int] = mapped_column(Integer, server_default="1")
    status: Mapped[str] = mapped_column(Text, server_default="active")
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccountGitHubLink(Base):
    __tablename__ = "account_github_links"
    __table_args__ = (
        CheckConstraint("verified_via IN ('discord_oauth', 'slack_oidc', 'auto_attach_discord')"),
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    github_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("github_user_links.github_user_id", ondelete="CASCADE")
    )
    platform: Mapped[str] = mapped_column(Text)
    platform_user_id: Mapped[str] = mapped_column(Text)
    verified_via: Mapped[str] = mapped_column(Text)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GitHubPersonalLinkIntent(Base):
    __tablename__ = "github_personal_link_intents"
    __table_args__ = (
        CheckConstraint("platform IN ('discord', 'slack')"),
        CheckConstraint("phase IN ('new', 'platform', 'github', 'used')"),
        Index("ix_github_personal_link_intents_expires_at", "expires_at"),
        Index("uq_github_personal_link_intents_platform_state", "platform_state", unique=True),
        Index("uq_github_personal_link_intents_github_state", "github_state", unique=True),
    )
    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    platform: Mapped[str] = mapped_column(Text)
    platform_user_id: Mapped[str] = mapped_column(Text)
    platform_workspace_id: Mapped[str] = mapped_column(Text)
    platform_state: Mapped[str | None] = mapped_column(Text)
    github_state: Mapped[str | None] = mapped_column(Text)
    browser_cookie_hash: Mapped[str | None] = mapped_column(Text)
    encrypted_verifier: Mapped[bytes | None] = mapped_column(LargeBinary)
    phase: Mapped[str] = mapped_column(Text, server_default="new")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GitHubIssuedToken(Base):
    __tablename__ = "github_issued_tokens"
    __table_args__ = (CheckConstraint("status IN ('pending', 'stored', 'delivered', 'revoked')"),)
    token_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    # Tenant deletion must revoke live installation tokens before this row cascades.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    session_id: Mapped[str] = mapped_column(Text)
    installation_id: Mapped[int] = mapped_column(BigInteger)
    repo_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    permissions: Mapped[dict[str, str]] = mapped_column(JSONB)
    grant_versions: Mapped[dict[str, int]] = mapped_column(JSONB)
    requester_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )
    github_user_id: Mapped[int | None] = mapped_column(BigInteger)
    link_generation: Mapped[int | None] = mapped_column(Integer)
    encrypted_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GitHubAppSessionVault(Base):
    __tablename__ = "github_app_session_vaults"
    __table_args__ = (Index("ix_github_app_session_vaults_mcp_open", "is_mcp", "closed_at"),)

    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    vault_id: Mapped[str] = mapped_column(Text)
    is_unmapped: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_mcp: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )
    repo_urls: Mapped[list[str] | None] = mapped_column(JSONB)
    repo_resource_ids: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SecurityAuditEvent(Base):
    """Append-only security metadata with dedicated erasure and retention maintenance."""

    __tablename__ = "security_audit_events"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('allowed', 'denied', 'error')", name="ck_security_audit_outcome"
        ),
        Index("ix_security_audit_tenant_time", "tenant_id", "occurred_at", "id"),
        # Only the channel tidy tools set turn_ref; their limit counts read this.
        Index(
            "ix_security_audit_tidy",
            "tenant_id",
            "agent_id",
            "occurred_at",
            postgresql_where=text("turn_ref IS NOT NULL"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    agent_name: Mapped[str | None] = mapped_column(Text)
    platform: Mapped[str | None] = mapped_column(Text)
    platform_user_id: Mapped[str | None] = mapped_column(Text)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    token_kind: Mapped[str | None] = mapped_column(Text)
    token_jti: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    scope: Mapped[str | None] = mapped_column(Text)
    github_token_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    github_session_id: Mapped[str | None] = mapped_column(Text)
    github_installation_id: Mapped[int | None] = mapped_column(BigInteger)
    github_repo_ids: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    github_permissions: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    github_grant_versions: Mapped[dict[str, int] | None] = mapped_column(JSONB)
    github_turn_origin_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    github_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set only by the channel tidy tools: which message an edit or delete
    # touched, a keyed HMAC of the text it replaced, and the turn it ran in.
    target_channel_id: Mapped[str | None] = mapped_column(Text)
    target_message_id: Mapped[str | None] = mapped_column(Text)
    content_hmac: Mapped[str | None] = mapped_column(Text)
    turn_ref: Mapped[str | None] = mapped_column(Text)


class AgentPostedMessage(Base):
    """A message or thread an agent posted.

    Written at send time by `send_message` and `create_thread` (`source='tool'`),
    and by chat adapters for a turn's status cards, answers and notices
    (`source='turn'`) and the thread it opens from a mention
    (`source='auto_thread'`). The channel tidy tools edit or delete only what
    this table says the calling agent posted. Holds ids and a keyed HMAC of
    the text, never the text itself.
    `channel_id` is where the message lives (a Discord thread id for a
    message in a thread); a Discord thread is its own row with
    `kind='thread'`, the parent channel as `channel_id` and the thread id as
    `message_id`.
    """

    __tablename__ = "agent_posted_messages"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "platform",
            "channel_id",
            "message_id",
            name="uq_agent_posted_messages_target",
        ),
        CheckConstraint("kind IN ('message', 'thread')", name="ck_agent_posted_messages_kind"),
        CheckConstraint(
            "source IN ('tool', 'turn', 'auto_thread')", name="ck_agent_posted_messages_source"
        ),
        CheckConstraint(
            "source <> 'turn' OR turn_card_intent_id IS NOT NULL",
            name="ck_agent_posted_messages_turn_intent",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[str] = mapped_column(Text, nullable=False)
    parent_channel_id: Mapped[str | None] = mapped_column(Text)
    thread_ts: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    content_hmac: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'tool'"))
    # The person whose message started the turn (`turn`) or whose mention
    # opened the thread (`auto_thread`). NULL for tool posts.
    requester_platform_user_id: Mapped[str | None] = mapped_column(Text)
    # The turn that posted a `turn` row; no FK, intents are pruned once retired.
    turn_card_intent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DirectMessagePolicy(Base):
    """Explicit tenant opt-in; absence leaves existing DM behavior unchanged."""

    __tablename__ = "direct_message_policies"
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class DirectMessageConversation(Base):
    """One user's selected workspace for a private platform conversation."""

    __tablename__ = "direct_message_conversations"
    platform: Mapped[str] = mapped_column(Text, primary_key=True)
    route_key: Mapped[str] = mapped_column(Text, primary_key=True)
    external_user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    # The parent channel (and thread) /dm was run in; re-checked against seals
    # each turn, and the DM's spend counts toward that channel's budget.
    source_channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Slack: channel:thread_ts of every copied message, so later thread seals match.
    source_thread_keys: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    context: Mapped[str] = mapped_column(Text, nullable=False)
    memory_read_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    history: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False)
    recent_message_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    active_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlatformUserName(Base):
    """The last name a chat platform gave for one of a tenant's people.

    Written whenever an adapter already holds it (an inbound message, a click,
    a lookup that succeeded), so the billing panel can name someone the
    platform no longer answers for. No accounts FK: a name may be seen before
    the person has a principal. A privacy purge deletes the row by
    (tenant, platform, platform user); a tenant's deletion cascades to it.
    """

    __tablename__ = "platform_user_names"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "platform", "platform_user_id", name="pk_platform_user_names"
        ),
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_platform_user_names_tenants"
        ),
        CheckConstraint(
            "display_name IS NOT NULL OR handle IS NOT NULL",
            name="ck_platform_user_names_some_name",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    platform: Mapped[str] = mapped_column(Text)
    platform_user_id: Mapped[str] = mapped_column(Text)
    #: What the platform shows for them: a Discord server nickname or global
    #: name, a Slack display or real name, a Teams name.
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Their account handle: a Discord username, a Slack username.
    handle: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlatformChannelName(Base):
    """The last name a chat platform gave for one of a tenant's channels.

    Teams shows channels by name only from a live listing; this keeps the last
    one seen, so the billing panel never shows a raw `19:…` id.
    """

    __tablename__ = "platform_channel_names"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "platform", "channel_id", name="pk_platform_channel_names"
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_platform_channel_names_tenants",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    platform: Mapped[str] = mapped_column(Text)
    channel_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
