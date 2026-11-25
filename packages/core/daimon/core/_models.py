"""SQLAlchemy 2.0 ORM for the tenant-scoped schema.

This module owns the schema. Alembic's `env.py` reads `Base.metadata` from here.
Stores map these ORM objects to Pydantic at their boundary — callers of stores
never see ORM instances.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, ClassVar

from daimon.core import _erasure_guards
from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    PrimaryKeyConstraint,
    Sequence,
    SmallInteger,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeEngine

# Repeated column shapes. A bare `Mapped[T]` gets its SQL type from
# `Base.type_annotation_map` and its nullability from `| None`.
TsNow = Annotated[datetime, mapped_column(server_default=func.now())]
TsNowOnUpdate = Annotated[datetime, mapped_column(server_default=func.now(), onupdate=func.now())]
UuidPk = Annotated[
    uuid.UUID, mapped_column(primary_key=True, server_default=func.gen_random_uuid())
]
TenantFK = Annotated[uuid.UUID, mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))]
TenantPk = Annotated[
    uuid.UUID, mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
]
AccountFK = Annotated[uuid.UUID, mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))]
AccountSetNull = Annotated[
    uuid.UUID | None, mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
]
FalseFlag = Annotated[bool, mapped_column(server_default=text("false"))]


class Base(DeclarativeBase):
    """Declarative base for all daimon-core ORM models."""

    type_annotation_map: ClassVar[dict[Any, TypeEngine[Any]]] = {
        str: Text(),
        uuid.UUID: UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
        int: Integer(),
        bool: Boolean(),
        bytes: LargeBinary(),
        dict[str, Any]: JSONB(),
        list[str]: ARRAY(Text),
        date: Date(),
    }


class AgentAvatar(Base):
    __tablename__ = "agent_avatars"
    __table_args__ = (
        CheckConstraint("source IN ('default', 'upload')", name="ck_agent_avatars_source"),
    )

    tenant_id: Mapped[TenantPk]
    agent_name: Mapped[str] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(unique=True)
    sha256: Mapped[str]
    png: Mapped[bytes]
    png_128: Mapped[bytes | None]
    png_512: Mapped[bytes | None]
    previous_sha256: Mapped[str | None]
    previous_png: Mapped[bytes | None]
    previous_png_128: Mapped[bytes | None]
    previous_png_512: Mapped[bytes | None]
    face_combo: Mapped[str | None]
    face_thumbnail: Mapped[bytes | None]
    source: Mapped[str]
    updated_by_account_id: Mapped[AccountSetNull]
    updated_at: Mapped[TsNow]


# Skill titles name a tenant by the first 8 hex chars of its id, so no two
# tenants may share them (migration 0091 creates this index).
TENANT_ID_PREFIX_INDEX = "uq_tenants_id_prefix"


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_tenants_platform_external_id"),
        CheckConstraint(
            "funding_mode IN ('prepaid', 'operator_funded')", name="ck_tenants_funding_mode"
        ),
        CheckConstraint("turn_cap IS NULL OR turn_cap > 0", name="ck_tenants_turn_cap"),
        Index(TENANT_ID_PREFIX_INDEX, text("left(CAST(id AS TEXT), 8)"), unique=True),
    )

    id: Mapped[UuidPk]
    platform: Mapped[str]
    external_id: Mapped[str]
    provision_status: Mapped[str] = mapped_column(server_default=text("'ready'"))
    funding_mode: Mapped[str] = mapped_column(server_default=text("'prepaid'"))
    turn_cap: Mapped[int | None]
    # Read by nothing: typed mentions are the `plain_text_mentions` feature now (migration
    # 0189 copied this value). Kept so an older build still starts.
    plain_text_mentions: Mapped[FalseFlag]
    # Bumped by every feature_values write: the feature resolver's cache key.
    feature_revision: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    last_reconcile_error: Mapped[str | None]
    archived_at: Mapped[datetime | None]
    registered_at: Mapped[TsNow]
    created_at: Mapped[TsNow]


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[UuidPk]
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(server_default="user")
    # Legacy role ids retained with the existing account column.
    platform_role_ids: Mapped[list[str]] = mapped_column(server_default=text("'{}'::text[]"))
    created_at: Mapped[TsNow]


class CliPrincipal(Base):
    __tablename__ = "cli_principals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "os_user", name="uq_cli_principals_tenant_os_user"),
        Index("ix_cli_principals_tenant_id", "tenant_id"),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    os_user: Mapped[str]
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    created_at: Mapped[TsNow]


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
        # Who an account runs as, read on every web request.
        Index("ix_platform_principals_account_id", "account_id"),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    platform: Mapped[str]
    external_id: Mapped[str]
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    created_at: Mapped[TsNow]
    active_agent_name: Mapped[str | None]
    # Home organisation id of a person verified to belong to another organisation.
    # Sticky: written once by admission, never cleared by code.
    foreign_home_id: Mapped[str | None]


class PrincipalLink(Base):
    __tablename__ = "principal_links"
    __table_args__ = (
        PrimaryKeyConstraint(
            "cli_principal_id", "platform_principal_id", name="pk_principal_links"
        ),
    )

    cli_principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cli_principals.id", ondelete="CASCADE")
    )
    platform_principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform_principals.id", ondelete="CASCADE")
    )
    linked_at: Mapped[TsNow]


class UserConfig(Base):
    __tablename__ = "user_config"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    agent_name: Mapped[str | None]
    environment_name: Mapped[str | None]


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

    tenant_id: Mapped[uuid.UUID]
    agent_name: Mapped[str | None]
    environment_name: Mapped[str | None]
    agent_name_set_by_account_id: Mapped[uuid.UUID | None]
    agent_name_set_at: Mapped[datetime | None]
    mode: Mapped[str] = mapped_column(server_default=text("'agent'"))


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

    tenant_id: Mapped[uuid.UUID]
    policy: Mapped[dict[str, Any]]
    enabled_backends: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[\"anthropic\"]'::jsonb")
    )
    updated_at: Mapped[TsNow]
    # False: an admin never chose; the tenant follows the deployment default
    # (`default_enabled_backends`) and `enabled_backends` keeps Anthropic only,
    # which older builds still read safely.
    enabled_backends_explicit: Mapped[FalseFlag]


class AgentToolRuleRecord(Base):
    __tablename__ = "agent_tool_rules"
    __table_args__ = (
        Index(
            "uq_agent_tool_rules_global",
            "tenant_id",
            "agent_id",
            "tool_key",
            unique=True,
            postgresql_where=text("channel_id IS NULL"),
        ),
        Index(
            "uq_agent_tool_rules_channel",
            "tenant_id",
            "agent_id",
            "tool_key",
            "channel_id",
            unique=True,
            postgresql_where=text("channel_id IS NOT NULL"),
        ),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        CheckConstraint("rule IS NULL OR rule IN ('allow', 'ask', 'deny')"),
        CheckConstraint("scope IN ('chat', 'chat_and_routines')"),
        CheckConstraint("revision > 0"),
        CheckConstraint("tool_key LIKE '%/%'"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[uuid.UUID]
    agent_id: Mapped[str]
    agent_name: Mapped[str]
    tool_key: Mapped[str]
    channel_id: Mapped[str | None]
    server_url: Mapped[str]
    rule: Mapped[str | None]
    scope: Mapped[str]
    revision: Mapped[int]
    set_by: Mapped[str]
    set_at: Mapped[datetime]


class AgentToolRuleHistoryRecord(Base):
    __tablename__ = "agent_tool_rule_history"
    __table_args__ = (ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[uuid.UUID]
    agent_id: Mapped[str]
    tool_key: Mapped[str]
    channel_id: Mapped[str | None]
    revision: Mapped[int]
    set_by: Mapped[str]
    set_at: Mapped[datetime]
    before_rule: Mapped[str | None]
    before_scope: Mapped[str | None]
    before_agent_name: Mapped[str | None]
    before_server_url: Mapped[str | None]
    after_rule: Mapped[str | None]
    after_scope: Mapped[str | None]
    after_agent_name: Mapped[str]
    after_server_url: Mapped[str]


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

    tenant_id: Mapped[uuid.UUID]
    channel_id: Mapped[str]
    agent_name: Mapped[str | None]
    environment_name: Mapped[str | None]
    agent_name_set_by_account_id: Mapped[uuid.UUID | None]
    agent_name_set_at: Mapped[datetime | None]
    # A server admin set `agent_name`, as decided when it was set: that makes the
    agent_name_set_by_admin: Mapped[FalseFlag]
    mode: Mapped[str] = mapped_column(server_default=text("'agent'"))


class DaimonAdmin(Base):
    """A tenant-wide user grant, or a Discord role grant; never platform authority."""

    __tablename__ = "daimon_admins"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "platform", "subject_kind", "subject_id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        CheckConstraint("platform IN ('discord', 'slack', 'teams')"),
        CheckConstraint("subject_kind IN ('user', 'role')"),
        CheckConstraint("subject_kind = 'user' OR platform = 'discord'"),
        CheckConstraint("length(subject_id) > 0"),
    )
    tenant_id: Mapped[uuid.UUID]
    platform: Mapped[str]
    subject_kind: Mapped[str]
    subject_id: Mapped[str]
    granted_by: Mapped[str | None]
    granted_at: Mapped[TsNow]


class Routine(Base):
    __tablename__ = "routines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_routine_id", name="uq_routines_tenant_source_id"),
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
            "destination_kind IS NULL OR destination_kind IN ('channel', 'thread', 'dm')",
            name="ck_routines_destination_kind",
        ),
        CheckConstraint(
            "destination_kind IS DISTINCT FROM 'dm' OR "
            "(created_by_user_id IS NOT NULL AND destination_id = created_by_user_id)",
            name="ck_routines_dm_owner",
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

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    created_by_user_id: Mapped[str | None]
    agent_id: Mapped[str]
    agent_name: Mapped[str]
    name: Mapped[str | None]
    cron_expr: Mapped[str]
    timezone: Mapped[str] = mapped_column(server_default="UTC")
    trigger_message: Mapped[str]
    enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    catch_up_policy: Mapped[str] = mapped_column(server_default="skip")
    last_skipped_from: Mapped[datetime | None]
    last_skipped_until: Mapped[datetime | None]
    last_skip_reason: Mapped[str | None]
    next_fire_at: Mapped[datetime | None]
    source_routine_id: Mapped[uuid.UUID | None]
    seeded_after: Mapped[datetime | None]
    last_fired_at: Mapped[datetime | None]
    last_fired_slot: Mapped[datetime | None]
    last_finished_at: Mapped[datetime | None]
    handback_paused_at: Mapped[datetime | None]
    handback_enabled: Mapped[bool | None]
    handback_next_fire_at: Mapped[datetime | None]
    last_finished_claim_at: Mapped[datetime | None]
    last_error: Mapped[str | None]
    last_result_tail: Mapped[str | None]
    # Optional place the result goes, and the outbox that posts it.
    destination_kind: Mapped[str | None]
    destination_id: Mapped[str | None]
    delivery_status: Mapped[str | None]
    delivery_lease_owner: Mapped[str | None]
    delivery_lease_expires_at: Mapped[datetime | None]
    delivery_note: Mapped[str | None]
    # The text a pending post carries: that fire's result, copied so a writer
    # that only knows `last_result_tail` cannot change what gets posted.
    delivery_payload: Mapped[str | None]
    delivered_at: Mapped[datetime | None]
    # Channel the routine's spend is attributed to and budget-gated by: its
    # destination's parent channel, resolved when the destination is set.
    channel_id: Mapped[str | None]
    # The channel the routine was created from; None from the web or the CLI.
    made_in_channel_id: Mapped[str | None]
    # Immutable creation-time provenance, independent of mutable delivery/budget placement.
    source_provenance_known: Mapped[FalseFlag]
    source_channel_id: Mapped[str | None]
    source_thread_id: Mapped[str | None]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNow]
    # A failure notice for a Slack or Teams owner, until that adapter sends it (0224).
    # Mapped before `delivery_hold`: 0221's down/up test re-adds that column last.
    owner_notice: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    # The hold the fire of that pending result ran under (`daimon.core.routine_hold`):
    # NULL for a legacy row, else `{"held": false}` or `{"held": true, "channel_id": ...}`.
    delivery_hold: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    # 0212 leaves the old `sandbox_*` columns and `routine_sandbox_history` unmapped for the
    # release still running during a deploy; a later release drops them.


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

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    platform: Mapped[str]
    thread_id: Mapped[str]
    account_id: Mapped[uuid.UUID | None]
    ma_session_id: Mapped[str]
    ma_agent_id: Mapped[str | None]
    # The channel the session runs for, recorded at creation: a thread's parent,
    # or the channel a DM was moved from (`Admission.budget_channel_id`). NULL
    # when unknown; agent reach then counts the session as possibly anywhere.
    channel_id: Mapped[str | None]
    # Immutable copy of the MA billing stamp. Empty means a legacy row whose
    # stamp is unknown; preparation replaces it before the next billed turn.
    budget_channel_id: Mapped[str | None]
    seal_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    watermark_message_id: Mapped[str | None]
    delivered_answer_chunk_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    # Untyped Text on purpose — no CHECK, so widening the vocabulary never needs
    # a lock on a hot table. Values:
    #   'live'       the caller's current session (the only status the
    #                caller-scoped lookup ever returns)
    #   'dead'       the MA session is gone (404 / archived); written only by
    #                the dead-session recovery path
    #   'superseded' replaced by a newer session carrying this one's work;
    #                `replaced_by_id` names the successor
    #   'retired'    deliberately abandoned on an explicit fresh start
    status: Mapped[str] = mapped_column(server_default=text("'live'"))
    # The configuration this session is actually running: an MA session freezes
    # its agent at create time, so the agent spec read at admission is not what
    # executes. A `daimon.core.session_snapshot.SessionSnapshot` serialized to
    # JSON. NULL on rows written before continuity existed.
    effective_config: Mapped[dict[str, Any] | None]
    identity_fingerprint: Mapped[str | None]
    mutable_fingerprint: Mapped[str | None]
    # What the session was created with (`EvalKey`); NULL on older rows.
    eval_key: Mapped[dict[str, Any] | None]
    # The provider-neutral binding this row backs (`provider_binding`), and its
    # generation. NULL until `stores.mux_state.link_legacy_thread_sessions`
    # fills backfilled rows (in batches, never under the migration's lock) or
    # the mux turn path writes them. No existing query reads them.
    binding_id: Mapped[str | None]
    binding_generation: Mapped[int | None]
    # Lineage of one continuing task across session replacements.
    predecessor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("thread_sessions.id", ondelete="SET NULL")
    )
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("thread_sessions.id", ondelete="SET NULL")
    )
    # The workspace bundle mounted into this session, and how complete it was:
    # 'full' (checkpointed workspace), 'transcript' (conversation only) or
    # 'history' (platform reseed only). No CHECK; the vocabulary is pinned by
    # `stores.domain.TransferKind` so adapters render honest copy from it.
    transfer_file_id: Mapped[str | None]
    transfer_kind: Mapped[str | None]
    # Set when the caller explicitly asked to start over; the next bind creates
    # the replacement first and only then retires this row.
    fresh_start_requested_at: Mapped[datetime | None]
    github_key_restart_notice: Mapped[FalseFlag]
    # What this caller answered when asked whether uncommitted repository
    # changes should be copied into the successor's working files ('copy') or
    # left in the old checkout ('leave'). Held here because the question is
    # asked in one turn and the replacement it governs happens in a later one;
    # cleared when the row stops being live. No CHECK; the vocabulary is pinned
    # by `stores.domain.UnsavedWorkChoice`.
    pending_unsaved_work: Mapped[str | None]
    # Set while a turn is running, cleared when it reaches a terminal state.
    # Distinct from `status`, which is about the session mapping and stays
    # 'live' on every healthy thread forever. Slack needs the channel because a
    # Slack message is addressed by (channel, ts); Discord leaves it NULL since
    # a Discord message id is globally addressable on its own.
    active_turn_owner_key: Mapped[int | None] = mapped_column(BigInteger)
    active_turn_message_id: Mapped[str | None]
    active_turn_started_at: Mapped[datetime | None]
    active_turn_channel_id: Mapped[str | None]
    # The running turn's own deadline, so a turn allowed longer than the standard window
    # is not read as abandoned. NULL keeps the standard window.
    active_turn_deadline: Mapped[datetime | None]
    # The model and effort emoji a turn on this session last showed in its thread.
    model_marks: Mapped[list[str] | None]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNow]


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

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    platform: Mapped[str]
    thread_id: Mapped[str]
    turn_token: Mapped[uuid.UUID]
    # Slack addresses a message by (channel, timestamp); Discord needs no channel.
    channel_id: Mapped[str | None]
    # NULL while prepared: the platform accepted no response yet, or the
    # process died before it could persist the response's message identifier.
    message_id: Mapped[str | None]
    owner_key: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(server_default=text("'prepared'"))
    recovery_failures: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNow]


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

    tenant_id: Mapped[TenantFK]
    principal_id: Mapped[uuid.UUID]
    agent_name: Mapped[str]
    name: Mapped[str]
    source_repo_url: Mapped[str]
    source_repo_branch: Mapped[str] = mapped_column(server_default="")
    source_path: Mapped[str] = mapped_column(server_default="")
    content_hash: Mapped[str]
    anthropic_id: Mapped[str | None]
    anthropic_latest_version: Mapped[str | None]
    source: Mapped[str] = mapped_column(server_default="repo")
    origin: Mapped[str] = mapped_column(server_default="")
    added_by_account_id: Mapped[uuid.UUID | None]
    updated_at: Mapped[TsNowOnUpdate]


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

    tenant_id: Mapped[TenantFK]
    name: Mapped[str]
    content_hash: Mapped[str]
    anthropic_id: Mapped[str]
    updated_at: Mapped[TsNowOnUpdate]


class AgentGoogleBinding(Base):
    """Per-agent Google Workspace identity overlay.

    Empty day-1; populated by an operator running `daimon agents bind-google
    <agent> <email> --scopes <scope>...`. Holds the email + scope set the
    token broker mints credentials for: by refreshing the operator's
    authorized-user credential when the email matches
    ``credentials.google_user_email``, otherwise via domain-wide delegation
    against the tenant service account.
    """

    __tablename__ = "agent_google_binding"

    agent_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    email: Mapped[str]
    scopes: Mapped[list[str]]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNowOnUpdate]


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

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[TenantFK]
    # No FK: agents live in MA, not the local DB (same rationale as UserSkill.agent_name).
    agent_id: Mapped[uuid.UUID]
    mcp_server_url: Mapped[str]
    encrypted_token: Mapped[bytes]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNowOnUpdate]


class UsageEvent(Base):
    """Per-turn token row for billing/observability.

    UNIQUE (managed_session_id, event_id) ensures SSE-replay idempotency:
    the turn I/O replay (turn/driver.py) refolds events on reconnect; without
    this constraint, `record(...)` would double-insert.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint(
            "managed_session_id",
            "event_id",
            name="uq_usage_events_managed_session_event",
        ),
        # The cap gate's per-member window sum is a range scan on this.
        Index(
            "usage_events_tenant_user_occurred_idx",
            "tenant_id",
            "platform_user_id",
            "occurred_at",
        ),
        Index("usage_events_occurred_idx", "occurred_at"),
        Index("usage_events_tenant_idx", "tenant_id"),
        # The per-agent spend reads (a person's month by agent, a tenant's by agent).
        Index("usage_events_tenant_agent_occurred_idx", "tenant_id", "agent_id", "occurred_at"),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    occurred_at: Mapped[TsNow]
    platform_user_id: Mapped[str | None]
    managed_session_id: Mapped[str]
    model: Mapped[str] = mapped_column(server_default=text("''"))
    input_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    cache_read_input_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    cache_creation_input_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    event_id: Mapped[str]
    observation_revision: Mapped[int | None]
    # Parent channel of the turn or tool call that spent it; NULL when unknown.
    channel_id: Mapped[str | None]
    # The agent whose turn or tool call spent it; NULL when unknown, and on rows
    # written before spend was attributed to agents.
    agent_id: Mapped[str | None]
    # Web searches: the most any revision reported, those the free allowance
    # covered, and the latest revision's billed ones (`stores.usage_events.SearchTally`).
    web_searches: Mapped[int] = mapped_column(server_default=text("0"))
    free_web_searches: Mapped[int] = mapped_column(server_default=text("0"))
    paid_web_searches: Mapped[int] = mapped_column(server_default=text("0"))


class WebSearchAllowance(Base):
    """Searches counted against a provider's free monthly allowance.

    One row per provider account, allowance pool and calendar month, shared by
    every tenant on that account. Private to `daimon.core.stores.**`.
    """

    __tablename__ = "web_search_allowances"
    __table_args__ = (CheckConstraint("used >= 0", name="ck_web_search_allowances_used"),)

    provider: Mapped[str] = mapped_column(primary_key=True)
    account_scope_id: Mapped[str] = mapped_column(primary_key=True)
    pool: Mapped[str] = mapped_column(primary_key=True)
    period: Mapped[date] = mapped_column(primary_key=True)
    used: Mapped[int]


class TenantUserCap(Base):
    """Legacy per-(tenant, user) monthly cap, folded into `SpendAllowance` rows.

    Nothing reads it now; the rows record what was converted. NULL platform_user_id
    row = tenant-wide default.

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

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    platform_user_id: Mapped[str | None]
    cap_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    updated_at: Mapped[TsNowOnUpdate]


class PaymentEvent(Base):
    """Stripe webhook dedup row. NOT a ledger.

    PK is the Stripe event id (text), not a surrogate UUID — see RESEARCH
    Pitfall 7. The compare-and-set in `try_claim_credit` relies on this.
    """

    __tablename__ = "payment_events"
    __table_args__ = (Index("payment_events_tenant_idx", "tenant_id"),)

    id: Mapped[str] = mapped_column(primary_key=True)
    # payment_events.tenant_id is NOT NULL (migration applied).
    tenant_id: Mapped[TenantFK]
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    source: Mapped[str]
    credited_at: Mapped[datetime | None]
    occurred_at: Mapped[TsNow]


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

    event_id: Mapped[str] = mapped_column(primary_key=True)
    payment_intent: Mapped[str]
    event_type: Mapped[str]
    target_amount_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    received_at: Mapped[TsNow]


class TenantLedger(Base):
    """Append-only per-tenant USD ledger. Balance = SUM(delta_usd).

    NEVER a mutable balance column — every credit (topup/trial) and debit
    (turn/clawback) is one immutable row. Idempotency_key is the on_conflict
    target so webhook replays / SSE replays never double-write.
    """

    __tablename__ = "tenant_ledger"
    __table_args__ = (
        # INCLUDE lets the per-turn balance SUM(delta_usd) run index-only.
        Index("tenant_ledger_tenant_delta_idx", "tenant_id", postgresql_include=["delta_usd"]),
        Index("tenant_ledger_idem_idx", "idempotency_key", unique=True),
        Index(
            "tenant_ledger_tenant_channel_idx",
            "tenant_id",
            "channel_id",
            "occurred_at",
            postgresql_where=text("channel_id IS NOT NULL"),
        ),
        CheckConstraint(
            "estimate IS NULL OR estimate IN ('cache_writes_unreported')",
            name="ck_tenant_ledger_estimate",
        ),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    delta_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    # topup|manual_credit|trial|promo_credit|promo_expiry|promo_expiry_refund|*_debit|charge.*;
    # see billing.md
    reason: Mapped[str]
    idempotency_key: Mapped[str]
    # Who made a manual credit from the web; NULL for every other row.
    actor_account_id: Mapped[AccountSetNull]
    payment_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("payment_events.id", ondelete="SET NULL")
    )
    payment_intent: Mapped[str | None]
    occurred_at: Mapped[TsNow]
    # On a debit: the parent channel whose budget the spend counts against. On a
    # channel promo credit: the channel it was redeemed for (budgets read debits only).
    channel_id: Mapped[str | None]
    # A debit priced from incomplete provider counts (a `LedgerEstimate`); NULL when
    # fully metered. Never changes the amount: it says how the amount was reached.
    estimate: Mapped[str | None]


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

    tenant_id: Mapped[uuid.UUID]
    platform: Mapped[str]
    channel_id: Mapped[str]
    skill_id: Mapped[str]
    version: Mapped[str]
    name: Mapped[str]
    owner_agent_name: Mapped[str | None]
    added_by_account_id: Mapped[uuid.UUID | None]
    added_at: Mapped[TsNow]


class SkillSize(Base):
    """The measured bundle of one source skill version, so a turn reads no bytes twice."""

    __tablename__ = "skill_sizes"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "skill_id", "version", name="pk_skill_sizes"),
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_skill_sizes_tenants"
        ),
    )

    tenant_id: Mapped[uuid.UUID]
    skill_id: Mapped[str]
    version: Mapped[str]
    name: Mapped[str]
    total_bytes: Mapped[int] = mapped_column(BigInteger)
    largest_file_bytes: Mapped[int] = mapped_column(BigInteger)
    file_count: Mapped[int]
    text_only: Mapped[bool]
    # The SKILL.md's bytes; -1: not text; NULL: measured before this column, or unreadable.
    manifest_bytes: Mapped[int | None] = mapped_column(BigInteger)
    measured_at: Mapped[TsNow]


class SkillCopy(Base):
    """A source skill version uploaded once to a provider project, reused by every session."""

    __tablename__ = "skill_copies"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id",
            "provider",
            "account_scope_id",
            "skill_id",
            "version",
            name="pk_skill_copies",
        ),
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_skill_copies_tenants"
        ),
    )

    tenant_id: Mapped[uuid.UUID]
    provider: Mapped[str]
    account_scope_id: Mapped[str]
    skill_id: Mapped[str]
    version: Mapped[str]
    copy_id: Mapped[str]
    copy_version: Mapped[str]
    created_at: Mapped[TsNow]


class AgentCreationChannel(Base):
    """The legacy channel recorded when an agent was created.

    Rows remain for a later data cleanup.
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

    tenant_id: Mapped[uuid.UUID]
    ma_agent_id: Mapped[str]
    platform: Mapped[str]
    channel_id: Mapped[str]
    created_at: Mapped[TsNow]


class SpendAllowance(Base):
    """What one person, a group or an invite may spend on agents, per person.

    `subject_kind` names whose it is: a `person` (their platform user id), a `group`
    (a group name; `everyone` is built in) or an `invite` (a visitor's invite id).
    `agent_id` narrows it to one agent, else it covers every agent. The amount is each
    person's own limit, counted in billed dollars inside the window: `month` is the
    current UTC calendar month, `total` everything since `starts_at`. A row with `is_cap`
    is a monthly ceiling for every agent, for `everyone` or one person, that holds on top
    of whichever allowance governs; it is never one of the allowances to choose among.
    """

    __tablename__ = "spend_allowances"
    __table_args__ = (
        CheckConstraint(
            "subject_kind IN ('person', 'group', 'invite')", name="ck_spend_allowances_subject"
        ),
        CheckConstraint("\"window\" IN ('month', 'total')", name="ck_spend_allowances_window"),
        CheckConstraint("amount_usd >= 0", name="ck_spend_allowances_amount"),
        Index("ix_spend_allowances_tenant_subject", "tenant_id", "subject_kind", "subject_id"),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    subject_kind: Mapped[str]
    subject_id: Mapped[str]
    agent_id: Mapped[str | None]
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    window: Mapped[str]
    starts_at: Mapped[TsNow]
    expires_at: Mapped[datetime | None]
    created_by_account_id: Mapped[AccountSetNull]
    created_at: Mapped[TsNow]
    is_cap: Mapped[FalseFlag]


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
        CheckConstraint("on_limit IN ('stop', 'alert')", name="ck_channel_budgets_on_limit"),
        CheckConstraint(
            "alert_channel_id IS NULL OR on_limit = 'alert'",
            name="ck_channel_budgets_alert_channel",
        ),
        CheckConstraint(
            "(\"window\" = 'fixed' AND starts_at IS NOT NULL AND ends_at IS NOT NULL "
            "AND starts_at < ends_at) "
            "OR (\"window\" = 'total' AND ends_at IS NULL) "
            "OR (\"window\" = 'monthly' AND starts_at IS NULL AND ends_at IS NULL)",
            name="ck_channel_budgets_bounds",
        ),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    platform: Mapped[str]
    channel_id: Mapped[str]
    limit_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    window: Mapped[str]
    starts_at: Mapped[datetime | None]
    ends_at: Mapped[datetime | None]
    set_by_account_id: Mapped[AccountSetNull]
    # The window whose exhausted notice went out; cleared when the budget is set or raised.
    exhausted_notice_key: Mapped[str | None]
    # `stop` refuses turns at the limit; `alert` never does and posts to `alert_channel_id`
    # (NULL: the support destination) at each full multiple of the limit.
    on_limit: Mapped[str] = mapped_column(server_default=text("'stop'"))
    alert_channel_id: Mapped[str | None]
    # The window and highest multiple already alerted; cleared when the budget is set or raised.
    alert_window_key: Mapped[str | None]
    alert_level: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNowOnUpdate]


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
        CheckConstraint(
            "channel_cap_usd IS NULL OR (kind = 'credit' AND channel_cap_usd >= amount_usd)",
            name="ck_promo_codes_channel_cap",
        ),
    )

    id: Mapped[UuidPk]
    code_hash: Mapped[str]
    note: Mapped[str | None]
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    kind: Mapped[str]
    credit_starts_at: Mapped[datetime | None]
    credit_ends_at: Mapped[datetime | None]
    redeem_starts_at: Mapped[datetime | None]
    redeem_ends_at: Mapped[datetime | None]
    max_redemptions: Mapped[int | None]
    redeemed_count: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[TsNow]
    revoked_at: Mapped[datetime | None]
    # Set on a channel code: any member may redeem it into their own channel's
    # budget, and one channel takes at most this much from it.
    channel_cap_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))


class PromoRedemption(Base):
    """One tenant's redemption of one promo code, and its grant/expiry state.

    `granted_at` is when the credit reached the ledger (a timed code redeemed
    before its window waits for the scheduler); `expired_at`/`expired_usd`
    record the unspent remainder a timed code removed at its window's end,
    and `reconciled_at` when late-recorded spend inside the window was
    credited back out of that remainder.
    The redeeming account is attribution only, severed by account erasure.
    A channel code's redemptions live in `promo_channel_redemptions`.
    """

    __tablename__ = "promo_redemptions"
    __table_args__ = (
        UniqueConstraint("promo_code_id", "tenant_id", name="uq_promo_redemptions_code_tenant"),
        Index("promo_redemptions_tenant_idx", "tenant_id"),
        CheckConstraint(
            "expired_usd IS NULL OR expired_usd >= 0", name="ck_promo_redemptions_expired_usd"
        ),
    )

    id: Mapped[UuidPk]
    promo_code_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("promo_codes.id", ondelete="RESTRICT")
    )
    tenant_id: Mapped[TenantFK]
    redeemed_by_account_id: Mapped[AccountSetNull]
    redeemed_at: Mapped[datetime]
    granted_at: Mapped[datetime | None]
    expired_at: Mapped[datetime | None]
    expired_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    reconciled_at: Mapped[datetime | None]


class PromoChannelRedemption(Base):
    """One person's redemption of a channel code into one channel of their tenant.

    Once per code, tenant, channel and person; the code's `channel_cap_usd`
    bounds how many rows one channel may have. The redeeming account is
    attribution (severed by account erasure), and a severed row still counts
    toward the channel's cap.
    """

    __tablename__ = "promo_channel_redemptions"
    __table_args__ = (
        UniqueConstraint(
            "promo_code_id",
            "tenant_id",
            "channel_id",
            "redeemed_by_account_id",
            name="uq_promo_channel_redemptions_person",
        ),
        Index("promo_channel_redemptions_channel_idx", "promo_code_id", "tenant_id", "channel_id"),
        Index("promo_channel_redemptions_tenant_idx", "tenant_id"),
    )

    id: Mapped[UuidPk]
    promo_code_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("promo_codes.id", ondelete="RESTRICT")
    )
    tenant_id: Mapped[TenantFK]
    channel_id: Mapped[str]
    redeemed_by_account_id: Mapped[AccountSetNull]
    redeemed_at: Mapped[datetime]


class PromoRedeemFailure(Base):
    """One refused redemption attempt, counted to throttle guessing per person.

    `account_id` is who tried; rows without one (written before it existed,
    or by a caller with no account) form one shared bucket per tenant.
    """

    __tablename__ = "promo_redeem_failures"
    __table_args__ = (
        Index("promo_redeem_failures_tenant_idx", "tenant_id", "attempted_at"),
        Index("promo_redeem_failures_account_idx", "tenant_id", "account_id", "attempted_at"),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    attempted_at: Mapped[datetime]
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE")
    )


class AgentFile(Base):
    """Per-(tenant, agent, key) encrypted environment value storage."""

    __tablename__ = "agent_files"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id", "key", name="pk_agent_files"),
        CheckConstraint("encoding IN ('plain', 'fernet_v1')", name="ck_agent_files_encoding"),
    )

    tenant_id: Mapped[TenantFK]
    agent_id: Mapped[uuid.UUID]
    key: Mapped[str]
    content: Mapped[str]
    encoding: Mapped[str] = mapped_column(server_default="plain")
    # Attribution, not authorization: who first created the key and who last
    # replaced its value. No FK to accounts.id, matching CredentialRequest's
    # rationale — these rows are erased by the platform-user-scoped helper,
    # not by an accounts.id cascade. Both stay NULL for pre-migration rows and
    # for writes with no acting person (a self-edit tool run headless).
    created_by_account_id: Mapped[uuid.UUID | None]
    last_set_by_account_id: Mapped[uuid.UUID | None]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNowOnUpdate]


class PendingFileDelete(Base):
    """Files-API object TTL queue.

    Records which MA-side Files-API objects to delete and when. The durable
    copy of a secret lives in `agent_files`; the uploaded Files-API object is
    disposable per session. No FK to tenants — deletion needs no tenant context.
    """

    __tablename__ = "pending_file_deletes"
    __table_args__ = (PrimaryKeyConstraint("file_id", name="pk_pending_file_deletes"),)

    file_id: Mapped[str]
    delete_after: Mapped[datetime]
    created_at: Mapped[TsNow]


class PrivacySessionDelete(Base):
    """Durable MA erasure target; deliberately survives account and tenant removal."""

    __tablename__ = "privacy_session_deletes"

    account_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_ids: Mapped[list[str]] = mapped_column(JSONB)
    pending_session_ids: Mapped[dict[str, list[str]]] = mapped_column(JSONB)
    created_at: Mapped[TsNow]


class AgentMemoryStore(Base):
    """Per-(tenant, agent) MA memory store binding (agent memory feature)."""

    __tablename__ = "agent_memory_store"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "agent_id", name="pk_agent_memory_store"),)

    tenant_id: Mapped[TenantFK]
    agent_id: Mapped[uuid.UUID]
    memory_store_id: Mapped[str]
    created_at: Mapped[TsNow]


class AgentChannelMemoryStore(Base):
    """Per-(tenant, agent, platform, channel) MA memory store binding: a held channel's memory.

    A held turn (another organisation's requester, or a shared channel) mounts
    this store instead of the agent-wide one and no personal store. `channel_id`
    is the parent channel; threads under it share the row.
    """

    __tablename__ = "agent_channel_memory_store"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "agent_id", "platform", "channel_id", name="pk_agent_channel_memory_store"
        ),
    )

    tenant_id: Mapped[TenantFK]
    agent_id: Mapped[uuid.UUID]
    platform: Mapped[str]
    channel_id: Mapped[str]
    memory_store_id: Mapped[str]
    created_at: Mapped[TsNow]


class AgentPersonalMemoryStore(Base):
    """Per-(tenant, agent, account) MA memory store binding: one person's own memory.

    `account_id` deliberately has no foreign key. Account erasure deletes the
    account row first and the MA store afterwards (`purge`), so the binding has
    to outlive the account: it is the record of which store is still to delete.
    For the same reason archiving the agent keeps the row and only stamps
    `archived_at`: an archived store still holds the person's memories, and
    the row is what erasure deletes it by. An archived row is never mounted.
    """

    __tablename__ = "agent_personal_memory_store"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "agent_id", "account_id", name="pk_agent_personal_memory_store"
        ),
        Index("ix_agent_personal_memory_store_account_id", "account_id"),
    )

    tenant_id: Mapped[TenantFK]
    agent_id: Mapped[uuid.UUID]
    account_id: Mapped[uuid.UUID]
    memory_store_id: Mapped[str]
    created_at: Mapped[TsNow]
    archived_at: Mapped[datetime | None]


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
    account_login: Mapped[str]
    repo_full_names: Mapped[list[str]]
    github_repo_full_names: Mapped[list[str] | None]
    github_refreshed_at: Mapped[datetime | None]
    github_removed_at: Mapped[datetime | None]
    app: Mapped[str] = mapped_column(server_default="legacy")
    account_id: Mapped[int | None] = mapped_column(BigInteger)
    account_type: Mapped[str | None]
    repository_selection: Mapped[str | None]
    permissions: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    suspended_at: Mapped[datetime | None]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNowOnUpdate]


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
    generation: Mapped[int] = mapped_column(BigInteger, server_default="1")
    claimed_generation: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(server_default="0")
    available_at: Mapped[TsNow]
    lease_owner: Mapped[uuid.UUID | None]
    lease_expires_at: Mapped[datetime | None]
    last_error: Mapped[str | None]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNowOnUpdate]


class GitHubInstallationDelivery(Base):
    """Finite idempotency receipts for installation webhooks."""

    __tablename__ = "github_installation_deliveries"

    delivery_id: Mapped[str] = mapped_column(primary_key=True)
    installation_id: Mapped[int] = mapped_column(BigInteger)
    event: Mapped[str]
    received_at: Mapped[TsNow]


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

    id: Mapped[UuidPk]
    repo_full_name: Mapped[str]
    ref: Mapped[str]
    delivery_id: Mapped[str]
    generation: Mapped[int] = mapped_column(BigInteger, server_default="1")
    claimed_generation: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(server_default="0")
    available_at: Mapped[TsNow]
    lease_owner: Mapped[uuid.UUID | None]
    lease_expires_at: Mapped[datetime | None]
    last_error: Mapped[str | None]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNowOnUpdate]


class GitHubPushDelivery(Base):
    """Finite-retention idempotency receipts for verified push deliveries."""

    __tablename__ = "github_push_deliveries"
    __table_args__ = (Index("ix_github_push_deliveries_received_at", "received_at"),)

    delivery_id: Mapped[str] = mapped_column(primary_key=True)
    repo_full_name: Mapped[str]
    ref: Mapped[str]
    received_at: Mapped[TsNow]


class McpToken(Base):
    """JTI registry for MCP JWTs that can be revoked: agent keys, operator and CLI tokens.

    Each minted token has one row. `revoked_at` is NULL while the token is
    live; `revoke_mcp_token` sets it atomically via UPDATE…RETURNING.

    `agent_id` is Text, not UUID — it stores the stringified derived UUID
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
        CheckConstraint(
            "surface IS NULL OR (surface = 'provider_turn' AND kind = 'agent')",
            name="ck_mcp_tokens_surface",
        ),
        CheckConstraint(
            "skill_bytes_read >= 0 AND skill_calls >= 0", name="ck_mcp_tokens_skill_reads"
        ),
        CheckConstraint(
            "purpose IS NULL OR purpose IN ('coding', 'report', 'internal')",
            name="ck_mcp_tokens_purpose",
        ),
        CheckConstraint(
            "purpose IS DISTINCT FROM 'coding' OR (kind = 'agent' AND surface IS NULL)",
            name="ck_mcp_tokens_coding_purpose",
        ),
    )

    jti: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    account_id: Mapped[AccountFK]
    tenant_id: Mapped[TenantFK]
    agent_id: Mapped[str | None]
    kind: Mapped[str] = mapped_column(server_default=text("'agent'"))
    scopes: Mapped[list[str]] = mapped_column(server_default=text("'{}'::text[]"))
    label: Mapped[str | None]
    created_at: Mapped[TsNow]
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    max_issued_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    issued_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default=text("0"))
    # A provider turn's skill calls and the bytes `skill_read` returned, against
    # its budget (`daimon.adapters.mcp.tools.skill_reads`).
    skill_bytes_read: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    skill_calls: Mapped[int] = mapped_column(server_default=text("0"))
    # The channel the token was minted in, whose calls then run inside it; both
    # NULL for a token bound to no channel.
    platform: Mapped[str | None]
    channel_id: Mapped[str | None]
    # `provider_turn`: a chat-execution credential with no confirmation surface.
    surface: Mapped[str | None]
    # What an agent-kind credential was made for. 'coding': a key a person made from
    # the agent's "coding tools" button; 'report': a published report's seam token;
    # 'internal': a short-lived in-session or provider-turn credential. Not a secret
    # and not in the JWT: the verifier reads it from the row to hold a coding key to
    # its holder's current role or ownership on every request. NULL for operator and
    # CLI tokens, and for an agent row an older process wrote (read as 'coding' unless
    # it is plainly internal; see the verifier).
    purpose: Mapped[str | None]
    # Which coding tool a coding key was made for ('claude-code', 'codex', ..., or
    # 'token' for a key shown once on screen). Display only; NULL for older keys and
    # for every other kind of credential.
    client: Mapped[str | None]


class CodingKeyClaim(Base):
    """A single-use code a person pastes to a coding agent, which trades it for a coding key.

    The code itself is never stored, only its hash. A batch holds one code per
    coding tool: the first one claimed kills the rest. A code that is claimed, or
    killed by a sibling or a newer batch, has `used_at` set; only a claimed one
    carries the `jti` of the key it made.

    Private to `daimon.core.stores.**` per the import-linter contract.
    """

    __tablename__ = "coding_key_claims"
    __table_args__ = (
        Index("ix_coding_key_claims_batch", "batch"),
        Index("ix_coding_key_claims_account_id", "account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    account_id: Mapped[AccountFK]
    agent_id: Mapped[uuid.UUID]
    # The agent's Managed Agents id, to load it live when the code is claimed.
    ma_agent_id: Mapped[str]
    # The agent's name when the code was made: the label its key will carry.
    agent_name: Mapped[str]
    client: Mapped[str]
    batch: Mapped[uuid.UUID]
    code_hash: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    jti: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("mcp_tokens.jti", ondelete="SET NULL"))


class SlackBotToken(Base):
    """Encrypted per-workspace Slack bot token.

    PK is `team_id` (Slack workspace ID, e.g. "T0123456789"). The store layer
    takes/returns pre-encrypted `bytes` — it never sees the Fernet key.
    Private to `daimon.core.stores.**` per the import-linter contract.
    """

    __tablename__ = "slack_bot_tokens"

    team_id: Mapped[str] = mapped_column(primary_key=True)
    encrypted_token: Mapped[bytes]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNowOnUpdate]
    expires_at: Mapped[datetime | None]
    refresh_token: Mapped[bytes | None]


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

    tenant_id: Mapped[uuid.UUID]
    #: The Bot Framework team id, which is also the General channel's id.
    team_id: Mapped[str]
    group_id: Mapped[str]
    name: Mapped[str | None]
    installed_at: Mapped[TsNow]
    updated_at: Mapped[TsNow]


class TeamsChannelSubscription(Base):
    """A Graph change-notification subscription to one private or shared channel.

    Teams sends the bot only the @mentions there, so the adapter subscribes to
    the channel's messages instead. `client_state_sha256` is the hash of the
    secret Graph echoes on every notification; the secret itself is not kept.
    `last_message_*` is the newest message handled, where a `missed` catch-up
    starts. The `team_*`, `channel_*`, `bot_name` and `service_url` columns are
    what the subscribing activity said, which a Graph message does not carry.
    """

    __tablename__ = "teams_channel_subscriptions"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "channel_id", name="pk_teams_channel_subscriptions"),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_teams_channel_subscriptions_tenants",
        ),
        UniqueConstraint("subscription_id", name="uq_teams_channel_subscriptions_subscription_id"),
    )

    tenant_id: Mapped[uuid.UUID]
    channel_id: Mapped[str]
    team_id: Mapped[str]
    group_id: Mapped[str]
    channel_type: Mapped[str]
    channel_name: Mapped[str | None]
    team_name: Mapped[str | None]
    bot_name: Mapped[str | None]
    service_url: Mapped[str | None]
    subscription_id: Mapped[str]
    client_state_sha256: Mapped[bytes]
    expires_at: Mapped[datetime]
    last_message_id: Mapped[str | None]
    last_message_at: Mapped[datetime | None]
    updated_at: Mapped[TsNow]


class TeamsMessageAdmission(Base):
    """One Teams message, admitted at most once across deliveries and processes.

    Keyed by the channel (or chat) id and the message id, which Bot Framework's
    activity id and Graph's chatMessage id share. `owner_key` is the claiming
    process's advisory lock (`worker_ownership`); `handled` is set before the
    turn starts or the message is otherwise answered. `message` (the parsed
    inbound) is kept only while unhandled, so a restart can replay it; a dead
    owner's unhandled row is reclaimed at the next boot. `notice_sent` records
    the restart notice, so no drain repeats it. A privacy purge clears the
    person and the words from their rows by (tenant, user) but keeps each row as
    the duplicate key; a tenant's deletion cascades to them.
    """

    __tablename__ = "teams_message_admissions"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "channel_id", "message_id", name="pk_teams_message_admissions"
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_teams_message_admissions_tenants",
        ),
        Index("ix_teams_message_admissions_created", "tenant_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID]
    channel_id: Mapped[str]
    message_id: Mapped[str]
    #: Where a notice about it goes: the thread or chat.
    conversation_id: Mapped[str]
    #: The sender's Entra object id, the privacy purge's key; None when unverified.
    user_id: Mapped[str | None]
    owner_key: Mapped[int] = mapped_column(BigInteger)
    handled: Mapped[FalseFlag]
    message: Mapped[dict[str, Any] | None]
    notice_sent: Mapped[FalseFlag]
    sent_at: Mapped[datetime]
    created_at: Mapped[TsNow]


class SlackUserToken(Base):
    """Encrypted per-(workspace, user) Slack xoxp token (user-token hybrid model).

    Composite PK (team_id, slack_user_id). The store layer takes/returns
    pre-encrypted bytes — it never sees the Fernet key. Private to
    `daimon.core.stores.**` per the import-linter contract.
    """

    __tablename__ = "slack_user_tokens"

    team_id: Mapped[str] = mapped_column(primary_key=True)
    slack_user_id: Mapped[str] = mapped_column(primary_key=True)
    encrypted_token: Mapped[bytes]
    scopes: Mapped[str]
    expires_at: Mapped[datetime | None]
    encrypted_refresh_token: Mapped[bytes | None]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNow]


class MicrosoftUserToken(Base):
    """One person's Microsoft 365 connection: a Fernet-encrypted refresh token.

    Keyed by (tenant, user): `user_id` is the person's Entra object id, which
    is also their Teams user id, and the sign-in proved the two equal. A
    privacy purge deletes the row by that key; a tenant's deletion cascades.
    """

    __tablename__ = "microsoft_user_tokens"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_microsoft_user_tokens_tenants",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(primary_key=True)
    encrypted_refresh_token: Mapped[bytes]
    scopes: Mapped[str]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class ConnectorToken(Base):
    """An OAuth connector grant whose tokens Daimon holds and refreshes.

    Personal (`account_id` set, the person who signed in) or agent-wide
    (`account_id` NULL). Kept apart from `watch_credentials` on purpose: watches
    read keys by (tenant, owner, URL) and must never reach an OAuth connector
    token. Rotation is a compare-and-set on `updated_at`; no row lock is held
    across the token endpoint call.
    """

    __tablename__ = "connector_tokens"
    __table_args__ = (
        CheckConstraint(
            "refresh_owner IN ('ma', 'daimon')", name="ck_connector_tokens_refresh_owner"
        ),
        Index(
            "uq_connector_tokens_personal",
            "tenant_id",
            "account_id",
            "agent_id",
            "server_url",
            unique=True,
            postgresql_where=text("account_id IS NOT NULL"),
        ),
        Index(
            "uq_connector_tokens_agent_wide",
            "tenant_id",
            "agent_id",
            "server_url",
            unique=True,
            postgresql_where=text("account_id IS NULL"),
        ),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE")
    )
    agent_id: Mapped[uuid.UUID]
    server_url: Mapped[str]
    access_token_encrypted: Mapped[bytes]
    refresh_token_encrypted: Mapped[bytes | None]
    token_endpoint: Mapped[str]
    client_id: Mapped[str]
    client_secret_encrypted: Mapped[bytes | None]
    token_endpoint_auth_method: Mapped[str]
    scope: Mapped[str | None]
    resource: Mapped[str | None]
    expires_at: Mapped[datetime | None]
    refresh_owner: Mapped[str]
    refresh_started_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class MicrosoftSignIn(Base):
    """One Connect Microsoft link, spent by its callback.

    `state` is both the link's handle and the OAuth state; the callback's
    delete is the single-use gate. Bound to the person who asked: the callback
    refuses a sign-in whose object id differs from `user_id`. Lives ten
    minutes and cascades with the account.
    """

    __tablename__ = "microsoft_sign_ins"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_microsoft_sign_ins_tenants"
        ),
        ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            ondelete="CASCADE",
            name="fk_microsoft_sign_ins_accounts",
        ),
        Index("ix_microsoft_sign_ins_user", "tenant_id", "user_id"),
    )

    state: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID]
    account_id: Mapped[uuid.UUID]
    user_id: Mapped[str]
    code_verifier: Mapped[str]
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime]


class SlackConnectPrompt(Base):
    """Once-ever marker: the first-mention connect nudge was shown to this user."""

    __tablename__ = "slack_connect_prompts"

    team_id: Mapped[str] = mapped_column(primary_key=True)
    slack_user_id: Mapped[str] = mapped_column(primary_key=True)
    prompted_at: Mapped[datetime]


class SlackTurnContext(Base):
    """Live "this account is running a turn in this channel" row.

    Inserted by the Slack adapter before run_turn, deleted in its finally.
    Read by MCP tool impls for the leak-policy destination check; readers
    ignore rows older than their TTL so a crashed process cannot poison the
    policy open — only closed.
    """

    __tablename__ = "slack_turn_contexts"
    __table_args__ = (Index("ix_slack_turn_contexts_tenant_account", "tenant_id", "account_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID]
    account_id: Mapped[uuid.UUID]
    channel_id: Mapped[str]
    thread_ts: Mapped[str]
    started_at: Mapped[datetime]


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
            "kind IN ('env', 'env_file', 'mcp', 'mcp_oauth', 'repo', 'skill_repo', "
            "'watch_credential')",
            name="ck_credential_requests_kind",
        ),
        UniqueConstraint("idempotency_key", name="uq_credential_requests_idempotency_key"),
    )

    token: Mapped[str] = mapped_column(primary_key=True)
    # Constrained by ck_credential_requests_kind; the vocabulary itself is
    # `daimon.core.credential_requests.CredentialRequestKind`.
    kind: Mapped[str]
    tenant_id: Mapped[TenantFK]
    agent_id: Mapped[uuid.UUID]
    account_id: Mapped[uuid.UUID]
    target: Mapped[str]
    mcp_server_url: Mapped[str | None]
    requester_platform_user_id: Mapped[str]
    channel_id: Mapped[str]
    platform: Mapped[str | None]
    parent_channel_id: Mapped[str | None]
    origin_thread_id: Mapped[str | None]
    posted_message_id: Mapped[str | None]
    idempotency_key: Mapped[uuid.UUID]
    requested_work: Mapped[str | None]
    target_ma_agent_id: Mapped[str | None]
    target_name: Mapped[str | None]
    responder_name: Mapped[str | None]
    replaces_updated_at: Mapped[datetime | None]
    # Untyped Text with no CHECK, like `thread_sessions.pending_unsaved_work`:
    # the vocabulary is pinned by `CredentialRequestOutcome` in
    # `daimon.core.credential_requests`.
    outcome: Mapped[str | None]
    created_at: Mapped[TsNow]
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]


class SecretShare(Base):
    """One secret a person shared with another (`daimon.core.secret_shares`).

    `content` is Fernet ciphertext, cleared on reveal or expiry; the row stays as the audit
    record. Person-scoped like `credential_requests`: erased by platform user, no account FK.
    `agent_id` is the provider id of the agent whose key it is, `key_name` the key;
    `awaiting_sender` holds a share until its sender confirms it.
    """

    __tablename__ = "secret_shares"
    __table_args__ = (
        Index("secret_shares_sender_idx", "tenant_id", "platform", "sender_platform_user_id"),
        Index("secret_shares_recipient_idx", "tenant_id", "platform", "recipient_platform_user_id"),
        Index("secret_shares_live_idx", "expires_at", postgresql_where=text("content IS NOT NULL")),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    platform: Mapped[str]
    sender_platform_user_id: Mapped[str]
    recipient_platform_user_id: Mapped[str]
    label: Mapped[str]
    content: Mapped[bytes | None]
    agent_id: Mapped[str | None]
    key_name: Mapped[str | None]
    awaiting_sender: Mapped[bool] = mapped_column(server_default=text("false"), default=False)
    channel_id: Mapped[str | None]
    message_id: Mapped[str | None]
    created_at: Mapped[TsNow]
    expires_at: Mapped[datetime]
    revealed_at: Mapped[datetime | None]


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

    state: Mapped[str] = mapped_column(primary_key=True)
    request_token: Mapped[str] = mapped_column(
        ForeignKey("credential_requests.token", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[TenantFK]
    account_id: Mapped[uuid.UUID]
    agent_id: Mapped[uuid.UUID]
    server_name: Mapped[str]
    mcp_server_url: Mapped[str]
    redirect_uri: Mapped[str]
    code_verifier: Mapped[str]
    client_id: Mapped[str | None]
    client_secret_encrypted: Mapped[str | None]
    token_endpoint_auth_method: Mapped[str | None]
    token_endpoint: Mapped[str | None]
    authorization_endpoint: Mapped[str | None]
    resource: Mapped[str | None]
    scope: Mapped[str | None]
    created_at: Mapped[TsNow]
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    # `used_at` is the replay gate, stamped before the callback knows whether
    # the person approved; `completed_at` is written only once their grant is
    # in the vault, which is what "connected" has to mean.
    completed_at: Mapped[datetime | None]


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

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE")
    )
    platform: Mapped[str]
    message_id: Mapped[str]
    channel_id: Mapped[str]
    platform_user_id: Mapped[str]
    ma_session_id: Mapped[str | None]
    vote: Mapped[str]
    feedback_text: Mapped[str | None]
    # Reason codes picked in the "What went wrong?" form
    # (`daimon.core.message_feedback.FEEDBACK_REASONS`); NULL when none were.
    feedback_reasons: Mapped[list[str] | None]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNow]


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

    id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    account_id: Mapped[AccountSetNull]
    requester_platform_user_id: Mapped[str]
    channel_id: Mapped[str]
    message_id: Mapped[str]
    # dict[str, Any]: a serialized WizardSpec, validated back into the real
    # Pydantic model at read time — not a typing shortcut.
    spec: Mapped[dict[str, Any]]
    answers: Mapped[dict[str, list[str]]] = mapped_column(JSONB)
    current_step: Mapped[int]
    status: Mapped[str]
    created_at: Mapped[TsNow]
    updated_at: Mapped[datetime]
    expires_at: Mapped[datetime]


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

    team_id: Mapped[str] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(primary_key=True)
    event_ts: Mapped[str] = mapped_column(primary_key=True)
    created_at: Mapped[TsNow]


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
        Index("ix_file_uploads_person_id", "person_id"),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    # The web person a file belongs to; erasing the person erases the file.
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("web_people.id", ondelete="CASCADE")
    )
    upload_token: Mapped[str | None]
    title: Mapped[str]
    display_filename: Mapped[str]
    content_type: Mapped[str]
    content: Mapped[bytes | None]
    created_at: Mapped[TsNow]


# One counter for the menus, limits and channels of every tenant; it never goes back,
# so deleting and recreating a row cannot repeat a revision an open form carries.
SUPPORT_REVISION_SEQ = Sequence("support_revision_seq", metadata=Base.metadata)

_SUPPORT_SCOPES = "scope IN ('workspace', 'agent', 'channel')"
_SUPPORT_SCOPE_ID = "(scope = 'workspace') = (scope_id = '')"


class SupportMenu(Base):
    """A tenant's menu for Ask a person (`support.MenuSpec`).

    `revision` takes the next number of a global sequence on every set: a menu
    carries it, and a submit against an older one is refused as stale rather than
    charged on terms the person never saw.
    """

    __tablename__ = "support_menus"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_support_menus_tenant_key"),
        CheckConstraint("key ~ '^[a-z0-9-]{1,32}$'", name="ck_support_menus_key"),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    key: Mapped[str]
    name: Mapped[str]
    revision: Mapped[int] = mapped_column(server_default=SUPPORT_REVISION_SEQ.next_value())
    # `SupportOption`s as JSON; the menu is `name`, these and `channel`.
    options: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    # `support.MenuChannel` as JSON; NULL: requests go to the workspace's channel.
    channel: Mapped[dict[str, Any] | None]
    set_by_account_id: Mapped[AccountSetNull]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNowOnUpdate]


class SupportMenuPlace(Base):
    """Which menu a place uses. No row: the next wider place, then the built-in menu."""

    __tablename__ = "support_menu_places"
    __table_args__ = (
        CheckConstraint(_SUPPORT_SCOPES, name="ck_support_menu_places_scope"),
        CheckConstraint(_SUPPORT_SCOPE_ID, name="ck_support_menu_places_scope_id"),
    )

    tenant_id: Mapped[TenantPk]
    scope: Mapped[str] = mapped_column(primary_key=True)
    scope_id: Mapped[str] = mapped_column(primary_key=True)
    menu_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("support_menus.id", ondelete="RESTRICT"))
    set_by_account_id: Mapped[AccountSetNull]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNowOnUpdate]


class SupportLimit(Base):
    """Support credits at a workspace, an agent or a channel, or one person's own limit there.

    A place row (`platform_user_id` NULL) is a ledger: `shared` is spent by
    everyone under it and `each_person` caps each person; the most specific one
    governs and limits never stack. A person row only replaces `each_person`
    for that person (scope workspace: everywhere). Outside its dates a row
    still governs, with nothing available. No place row: the default ledger.
    """

    __tablename__ = "support_limits"
    __table_args__ = (
        Index(
            "uq_support_limits_place",
            "tenant_id",
            "scope",
            "scope_id",
            unique=True,
            postgresql_where=text("platform_user_id IS NULL"),
        ),
        Index(
            "uq_support_limits_person",
            "tenant_id",
            "scope",
            "scope_id",
            "platform_user_id",
            unique=True,
            postgresql_where=text("platform_user_id IS NOT NULL"),
        ),
        CheckConstraint(_SUPPORT_SCOPES, name="ck_support_limits_scope"),
        CheckConstraint(_SUPPORT_SCOPE_ID, name="ck_support_limits_scope_id"),
        CheckConstraint(
            "(platform_user_id IS NULL AND (shared IS NOT NULL OR each_person IS NOT NULL)) "
            "OR (platform_user_id IS NOT NULL AND shared IS NULL AND each_person IS NOT NULL)",
            name="ck_support_limits_amounts",
        ),
        CheckConstraint("shared >= 0 AND each_person >= 0", name="ck_support_limits_positive"),
        CheckConstraint("\"window\" IN ('total', 'monthly')", name="ck_support_limits_window"),
        CheckConstraint(
            "starts_at IS NULL OR ends_at IS NULL OR starts_at < ends_at",
            name="ck_support_limits_dates",
        ),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    scope: Mapped[str]
    scope_id: Mapped[str]
    platform_user_id: Mapped[str | None]
    shared: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    each_person: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    window: Mapped[str]
    starts_at: Mapped[datetime | None]
    ends_at: Mapped[datetime | None]
    revision: Mapped[int] = mapped_column(server_default=SUPPORT_REVISION_SEQ.next_value())
    set_by_account_id: Mapped[AccountSetNull]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNowOnUpdate]


class SupportChannel(Base):
    """A workspace's own channel for requests, used when `support_own_channel` is on."""

    __tablename__ = "support_channels"

    tenant_id: Mapped[TenantPk]
    platform: Mapped[str]
    channel_id: Mapped[str]
    slack_team_id: Mapped[str | None]
    teams_team_id: Mapped[str | None]
    teams_service_url: Mapped[str | None]
    revision: Mapped[int] = mapped_column(server_default=SUPPORT_REVISION_SEQ.next_value())
    set_by_account_id: Mapped[AccountSetNull]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNowOnUpdate]


class SupportEscalation(Base):
    """One row per human-support request. The rows ARE the credit ledger.

    There is no counter column and no `credits_remaining`: spent credit is
    `SUM(COALESCE(charge, hold))` over the rows of one ledger (`charged_to`: the
    default ledger per (tenant, platform_user_id), or one support limit; `exempt`
    rows count nowhere). A counter would be a second source of truth that can
    drift from the rows it summarises. Rows from before limits are charged 1, so
    the default sum equals the old row count.

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
        Index("support_escalations_limit_idx", "tenant_id", "limit_id", "created_at"),
        Index(
            "support_escalations_pending_idx",
            "tenant_id",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        CheckConstraint(
            "charged_to IN ('default', 'limit', 'exempt')",
            name="ck_support_escalations_charged_to",
        ),
        CheckConstraint(
            "charged_to <> 'default' OR limit_id IS NULL", name="ck_support_escalations_default"
        ),
        CheckConstraint(
            "status IN ('pending', 'settled', 'declined')", name="ck_support_escalations_status"
        ),
        CheckConstraint(
            "(status = 'pending') = (charge IS NULL)", name="ck_support_escalations_pending"
        ),
        CheckConstraint(
            "hold >= 0 AND (charge IS NULL OR charge >= 0)", name="ck_support_escalations_amounts"
        ),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE")
    )
    platform: Mapped[str]
    platform_user_id: Mapped[str]
    channel_id: Mapped[str]
    message_id: Mapped[str]
    ma_session_id: Mapped[str | None]
    note: Mapped[str]
    delivered_at: Mapped[datetime | None]
    created_at: Mapped[TsNow]
    # Support limits (0206). The server defaults are a default-ledger row charged
    # 1, which is what a writer that predates limits means.
    charged_to: Mapped[str] = mapped_column(server_default="default")
    # SET NULL: a cleared limit's rows count nowhere, never in the default ledger.
    limit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("support_limits.id", ondelete="SET NULL")
    )
    # The person's own limit that capped the request, so a settle reads the same cap.
    person_limit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("support_limits.id", ondelete="SET NULL")
    )
    option_key: Mapped[str | None]
    # The terms requested on (`support.OptionSnapshot`); settling reads these.
    option: Mapped[dict[str, Any] | None]
    answers: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    hold: Mapped[Decimal] = mapped_column(Numeric(10, 2), server_default="1")
    # NULL while pending; spent credit is COALESCE(charge, hold).
    charge: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), server_default="1")
    status: Mapped[str] = mapped_column(server_default="settled")
    settled_at: Mapped[datetime | None]
    settled_by: Mapped[str | None]
    settled_by_account_id: Mapped[AccountSetNull]
    support_channel_id: Mapped[str | None]
    support_post_id: Mapped[str | None]


class DeploymentIdentity(Base):
    """This install's own id, stamped once on first need where no cloud names it.

    One row (`singleton` is always true). It tells this install's hosted OpenAI
    environments apart from another install's that shares the OpenAI project; on
    GCP the project id plays that part and this row is never read. It holds
    nothing tenant-owned, so it has no `tenant_id`.
    """

    __tablename__ = "deployment_identity"
    __table_args__ = (CheckConstraint("singleton", name="ck_deployment_identity_singleton"),)

    singleton: Mapped[bool] = mapped_column(primary_key=True, server_default=text("true"))
    id: Mapped[uuid.UUID]
    created_at: Mapped[TsNow]


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

    collection: Mapped[str] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[dict[str, object]] = mapped_column(JSONB)
    ttl: Mapped[float | None] = mapped_column(Double)
    created_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None] = mapped_column(index=True)


class FeatureValue(Base):
    """A feature set at one tier of one place; no row means the tier inherits.

    Rows exist for the workspace, agent, channel, thread and routine tiers (the deployment
    tier is the `features` setting). Unused place columns hold '' (`routine_id` NULL). A
    thread row is keyed by its thread key alone; its `channel_id` is the channel the thread
    sits in, '' when unknown. A routine row goes with its routine and holds its id in
    `channel_id` too, so `uq_feature_values_place` serves every tier but the thread. A
    pinned feature's On keeps in `pin` what it covers. `daimon.core.features.resolver`
    combines the rows; `locked` is final for every tier below.
    """

    __tablename__ = "feature_values"
    __table_args__ = (
        CheckConstraint(
            "tier IN ('workspace', 'agent', 'channel', 'thread', 'routine')",
            name="ck_feature_values_tier",
        ),
        CheckConstraint(
            "surface IN ('web', 'chat', 'panel', 'cli', 'system', 'spec', 'migration')",
            name="ck_feature_values_surface",
        ),
        CheckConstraint(
            "NOT (locked AND tier IN ('thread', 'routine'))", name="ck_feature_values_lock"
        ),
        CheckConstraint(
            "(tier = 'routine') = (routine_id IS NOT NULL)"
            " AND (routine_id IS NULL OR channel_id = routine_id::text)",
            name="ck_feature_values_routine",
        ),
        CheckConstraint(
            "key <> 'routine_sandbox_tools' OR value <> 'on'"
            " OR coalesce(jsonb_typeof(pin), '') = 'object'",
            name="ck_feature_values_sandbox_pin",
        ),
        Index(
            "uq_feature_values_place",
            "tenant_id",
            "tier",
            "agent_id",
            "channel_id",
            "key",
            unique=True,
            postgresql_where=text("tier <> 'thread'"),
        ),
        Index(
            "uq_feature_values_thread",
            "tenant_id",
            "agent_id",
            "thread_key",
            "key",
            unique=True,
            postgresql_where=text("tier = 'thread'"),
        ),
        Index("uq_feature_values_routine", "routine_id", "key", unique=True),
        Index("ix_feature_values_channel", "tenant_id", "channel_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[TenantFK]
    tier: Mapped[str]
    agent_id: Mapped[str] = mapped_column(server_default=text("''"))
    channel_id: Mapped[str] = mapped_column(server_default=text("''"))
    thread_key: Mapped[str] = mapped_column(server_default=text("''"))
    routine_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("routines.id", ondelete="CASCADE")
    )
    key: Mapped[str]
    value: Mapped[str]
    locked: Mapped[FalseFlag]
    pin: Mapped[dict[str, Any] | None]
    set_by: Mapped[AccountSetNull]
    surface: Mapped[str]
    set_at: Mapped[TsNow]
    revision: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))


class ThreadModelSetting(Base):
    """The model someone in a conversation chose for it (`/model`, or asking in words).

    One row per (tenant, platform, thread); no row means the agent's own
    model. `daimon.core.thread_models` applies it when a turn is admitted.
    """

    __tablename__ = "thread_model_settings"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "platform", "thread_id", name="pk_thread_model_settings"),
        CheckConstraint(
            "effort IN ('low', 'medium', 'high', 'xhigh', 'max')",
            name="ck_thread_model_settings_effort",
        ),
    )

    tenant_id: Mapped[TenantFK]
    platform: Mapped[str]
    thread_id: Mapped[str]
    # Null when only the effort was chosen: the agent's own model runs (0209).
    model: Mapped[str | None]
    updated_at: Mapped[TsNow]
    # The model's provider; null is a Claude model chosen before providers could be (0197).
    provider: Mapped[str | None]
    # The chosen effort; null keeps the agent's own (0209).
    effort: Mapped[str | None]


class ThreadModelConsent(Base):
    """An agent's consent to run one conversation on its chosen model's provider.

    One row per (tenant, platform, thread, agent): `revision` is the agent's
    backend revision with the thread's provider, model and loss consent, and
    `digest` pins it for the channel revisions that reference it (0197).
    """

    __tablename__ = "thread_model_consents"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "platform", "thread_id", "agent_id", name="pk_thread_model_consents"
        ),
    )

    tenant_id: Mapped[TenantFK]
    platform: Mapped[str]
    thread_id: Mapped[str]
    agent_id: Mapped[uuid.UUID]
    revision: Mapped[Any] = mapped_column(JSONB)
    digest: Mapped[str]
    created_at: Mapped[TsNow]


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

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    platform: Mapped[str]
    thread_id: Mapped[str]
    message_id: Mapped[str]
    created_at: Mapped[TsNow]


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
        CheckConstraint(
            "kind IN ('setup', 'handoff', 'owner', 'web')", name="ck_thread_agent_bindings_kind"
        ),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    platform: Mapped[str]
    parent_channel_id: Mapped[str]
    thread_id: Mapped[str]
    kind: Mapped[str] = mapped_column(server_default=text("'setup'"))
    responder_ma_agent_id: Mapped[str]
    responder_name: Mapped[str]
    configuration_target_ma_agent_id: Mapped[str | None]
    configuration_target_name: Mapped[str | None]
    creator_account_id: Mapped[AccountSetNull]
    archived: Mapped[FalseFlag]
    locked: Mapped[FalseFlag]
    deleted: Mapped[FalseFlag]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNow]
    # A cross-provider hand-off's summary (JSON: from, summary), taken once by the
    # destination's first turn here; cleared when the binding is replaced or released.
    handoff_summary: Mapped[str | None]


class AgentNotice(Base):
    """A failure the agent hears of on its next turn in the thread (`turn.agent_notices`)."""

    __tablename__ = "agent_notices"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "platform",
            "thread_id",
            "agent_id",
            "text",
            name="uq_agent_notices_thread_agent_text",
        ),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    platform: Mapped[str]
    thread_id: Mapped[str]
    # The provider's agent id: a hand-off must not pass one agent's notices to another.
    agent_id: Mapped[str]
    text: Mapped[str]
    created_at: Mapped[TsNow]


class TurnOrigin(Base):
    """Expiring control authority and target snapshot for one caller's running turn."""

    __tablename__ = "turn_origins"
    __table_args__ = (Index("turn_origins_expiry_idx", "expires_at"),)

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    account_id: Mapped[AccountFK]
    chat_session_id: Mapped[str | None]
    human_invocation: Mapped[bool | None]
    memory_read_only: Mapped[bool | None]
    attended: Mapped[FalseFlag]
    platform: Mapped[str]
    parent_channel_id: Mapped[str]
    thread_id: Mapped[str]
    responder_ma_agent_id: Mapped[str]
    responder_name: Mapped[str]
    configuration_target_ma_agent_id: Mapped[str | None]
    configuration_target_name: Mapped[str | None]
    is_setup: Mapped[FalseFlag]
    role: Mapped[str]
    created_at: Mapped[TsNow]
    expires_at: Mapped[datetime]
    # The sender is from another organisation (Teams): no 1:1 chat with Daimon for them.
    sender_foreign: Mapped[FalseFlag]
    # Set by `archive_thread` on this turn's own thread; the adapter archives
    # the thread after the turn's last post.
    archive_requested_at: Mapped[datetime | None]
    # The origin channel is shared with another organisation.
    shared_channel: Mapped[FalseFlag]
    # The platform's ids for the turn's message: where it can be read, and the message.
    platform_channel_id: Mapped[str | None]
    platform_message_id: Mapped[str | None]
    # Set on a helper's turn: the origin of the turn that asked it. A helper asks no
    # helpers and changes nothing about the conversation it works for.
    helper_of: Mapped[uuid.UUID | None]
    # The process running this turn serves its helper asks (`daimon.core.helpers`).
    serves_helpers: Mapped[FalseFlag]
    # The agent may ask copies of itself: its `[self]` roster on a provider without
    # native multiagent (`daimon.core.self_helpers`).
    self_helpers: Mapped[FalseFlag]
    # A helper turn's own session key, when the helper's agent id does not give it.
    helper_key: Mapped[str | None]
    # Set at admission, never by a caller: the turn may read a limited room its person can
    # read themselves (`daimon.core.authz`, READ_CHANNEL). True only for an interactive owner
    # DM turn and a routine fired by the scheduler's owner-DM path.
    own_dm_reach: Mapped[FalseFlag]
    # Set when the turn read a limited room through that reach: for the rest of the turn it
    # posts and sends direct messages to its person's own DM only.
    reach_read: Mapped[FalseFlag]


class AgentHelper(Base):
    """An agent another agent may ask for help inside its own turn (`daimon.core.helpers`)."""

    __tablename__ = "agent_helpers"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id", "helper_agent_id", name="pk_agent_helpers"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        CheckConstraint("agent_id <> helper_agent_id", name="ck_agent_helpers_not_self"),
    )

    tenant_id: Mapped[uuid.UUID]
    agent_id: Mapped[str]
    helper_agent_id: Mapped[str]
    created_at: Mapped[TsNow]


class HelperAsk(Base):
    """One message from a running turn to one of its helpers, and the helper's reply.

    Lives as long as the asking turn's origin: the cascade removes it with that origin.
    """

    __tablename__ = "helper_asks"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["origin_id"], ["turn_origins.id"], ondelete="CASCADE"),
        CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed', 'stopped')",
            name="ck_helper_asks_status",
        ),
        Index("helper_asks_origin_idx", "origin_id", "status"),
        Index("helper_asks_files_token_idx", "files_token", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID]
    origin_id: Mapped[uuid.UUID]
    helper_agent_id: Mapped[str]
    helper_name: Mapped[str]
    message: Mapped[str]
    status: Mapped[str] = mapped_column(server_default=text("'queued'"))
    reply: Mapped[str | None]
    created_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    # Files passed to the helper and the files it sends back: `file_uploads` facts
    # ({id, name, size}). `files_token` is the capability in their download links.
    files: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    reply_files: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    files_token: Mapped[str | None]


class SharedChannel(Base):
    """A channel known to be shared with another organisation, recorded once and kept.

    `source` is who revealed it: the platform's own signal, or a verified client
    requester seen there. Every turn in it is held to it (`daimon.core.turn_hold`).
    """

    __tablename__ = "shared_channels"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "platform", "channel_id", name="pk_shared_channels"),
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_shared_channels_tenants"
        ),
        CheckConstraint("source IN ('platform', 'client')", name="ck_shared_channels_source"),
    )

    tenant_id: Mapped[uuid.UUID]
    platform: Mapped[str]
    channel_id: Mapped[str]
    first_seen_at: Mapped[TsNow]
    source: Mapped[str]


class PendingSkillAdd(Base):
    """One previewed `add_skill` its person may confirm with a yes as their next message.

    Written only where no approval card can show because the deployment turned
    them off. Bound to the previewing turn's caller, thread and target agent
    and to the previewed content. Their next non-queued message approves it for
    that turn (`approved_origin_id`) or cancels it (`consumed_at`); an upload
    consumes it.
    """

    __tablename__ = "pending_skill_adds"
    __table_args__ = (
        Index("pending_skill_adds_lookup_idx", "tenant_id", "account_id", "platform", "thread_id"),
        Index("pending_skill_adds_expiry_idx", "expires_at"),
        # At most one open preview per person and thread, across all agents.
        Index(
            "uq_pending_skill_adds_open",
            "tenant_id",
            "account_id",
            "platform",
            "thread_id",
            unique=True,
            postgresql_where=text("consumed_at IS NULL"),
        ),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    account_id: Mapped[AccountFK]
    platform: Mapped[str]
    thread_id: Mapped[str]
    ma_agent_id: Mapped[str]
    content_hash: Mapped[str]
    preview_origin_id: Mapped[uuid.UUID]
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    # The turn whose person's explicit yes approved it; set at that turn's start.
    approved_origin_id: Mapped[uuid.UUID | None]
    consumed_at: Mapped[datetime | None]


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

    id: Mapped[UuidPk]
    mapping_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("thread_sessions.id", ondelete="CASCADE")
    )
    target_fingerprint: Mapped[str]
    stage: Mapped[str]
    transfer_file_id: Mapped[str | None]
    transfer_kind: Mapped[str | None]
    new_mapping_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("thread_sessions.id", ondelete="SET NULL")
    )
    failure_reason: Mapped[str | None]
    failure_kind: Mapped[str | None]
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNow]


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
            "reason IN ('task_handoff', 'private_input_applied', 'timer', "
            "'github_access_ready', 'watch_fired', 'job_finished', 'watch_report', "
            "'confirmed_call')",
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

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    attended: Mapped[FalseFlag]
    platform: Mapped[str]
    parent_channel_id: Mapped[str]
    thread_id: Mapped[str]
    requester_account_id: Mapped[uuid.UUID]
    requester_external_user_id: Mapped[str]
    target_ma_agent_id: Mapped[str]
    target_name: Mapped[str]
    # NULL means the handoff carried no work to continue: the switch is
    # recorded, and nothing is ever dispatched (never a billed turn).
    requested_work: Mapped[str | None]
    reason: Mapped[str]
    status: Mapped[str] = mapped_column(server_default=text("'pending'"))
    skip_reason: Mapped[str | None]
    idempotency_key: Mapped[uuid.UUID]
    created_at: Mapped[TsNow]
    claimed_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]
    # NULL: dispatched only at the tail of the next turn in the thread (a
    # handoff). Set: also polled, and not claimable before this instant.
    available_at: Mapped[datetime | None]
    lease_owner: Mapped[str | None]
    lease_expires_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(server_default=text("0"))


class TurnOutcome(Base):
    """Content-free terminal diagnostics; one UUID per logical turn."""

    __tablename__ = "turn_outcomes"
    __table_args__ = (
        CheckConstraint(
            "origin IN ('chat', 'routine', 'relay', 'handoff', 'helper')",
            name="ck_turn_outcomes_origin",
        ),
        CheckConstraint(
            "reason IN ('completed', 'interrupted', 'interrupt_timeout', "
            "'connection_lost', 'upstream', 'refused', 'rate_limited', 'session_terminated', "
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

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    account_id: Mapped[uuid.UUID | None]
    platform: Mapped[str]
    channel_id: Mapped[str | None]
    thread_id: Mapped[str | None]
    agent_id: Mapped[str | None]
    session_id: Mapped[str | None]
    origin: Mapped[str]
    reason: Mapped[str]
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime]
    duration_ms: Mapped[int] = mapped_column(BigInteger)
    recovered: Mapped[bool]
    error_class: Mapped[str | None]
    release: Mapped[str]
    usage_refs: Mapped[list[dict[str, str]]] = mapped_column(JSONB)

    # NULL distinguishes historical outcomes from measured zero usage.
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_read_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_creation_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    model_calls: Mapped[int | None]
    model_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    unpriced_calls: Mapped[int | None]
    billing_posture: Mapped[str | None]
    # What the turn ran with (`daimon.core.features.eval_key.EvalKey`), for
    # grouping eval results. NULL on turns without a session snapshot.
    eval_key: Mapped[dict[str, Any] | None]
    # A turn in a private direct message; false for every turn recorded before this column.
    is_dm: Mapped[bool] = mapped_column(server_default=text("false"))
    # Counted as an answer, as `turn.finished` is logged (the alert denominator); NULL before.
    alertable: Mapped[bool | None]


class TenantGitHubRepo(Base):
    """One confirmed repo: server-wide, or owned by one agent (`scope_agent_id`).

    A repo may be confirmed server-wide and for several agents at once; each
    row carries its own confirmation and limit. Agents live in Managed Agents,
    so `scope_agent_id` has no local table to reference.
    """

    __tablename__ = "tenant_github_repos"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "repo_id",
            "scope_agent_id",
            name="uq_tenant_github_repos_scope",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("max_access IN ('read', 'write')"),
        CheckConstraint("status IN ('active', 'suspended', 'revoked')"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[TenantFK]
    repo_id: Mapped[int] = mapped_column(BigInteger)
    # Null: any agent in the workspace may be granted it. Set: only that agent.
    scope_agent_id: Mapped[uuid.UUID | None]
    owner_id: Mapped[int] = mapped_column(BigInteger)
    installation_id: Mapped[int] = mapped_column(BigInteger)
    repo_full_name: Mapped[str]
    max_access: Mapped[str]
    authorized_by_github_user_id: Mapped[int] = mapped_column(BigInteger)
    authorized_by_account_id: Mapped[AccountSetNull]
    # The confirming person's GitHub permission on the repo: admin, maintain,
    # write, triage or read. Null on rows authorized before it was recorded.
    authorized_permission: Mapped[str | None]
    authorized_at: Mapped[TsNow]
    status: Mapped[str] = mapped_column(server_default="active")
    status_reason: Mapped[str | None]
    version: Mapped[int] = mapped_column(server_default="1")


class GitHubConnectInvitation(Base):
    __tablename__ = "github_connect_invitations"
    token_hash: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    requester_account_id: Mapped[AccountFK]
    workspace_label: Mapped[str]
    requester_label: Mapped[str]
    requester_platform_user_id: Mapped[str | None]
    agent_id: Mapped[uuid.UUID | None]
    agent_name: Mapped[str | None]
    # for it can be checked again when the repos are confirmed.
    agent_ma_id: Mapped[str | None]
    operator_issued: Mapped[FalseFlag]
    connected_repo_count: Mapped[int | None]
    origin_platform: Mapped[str | None]
    origin_parent_channel_id: Mapped[str | None]
    origin_thread_id: Mapped[str | None]
    origin_ma_agent_id: Mapped[str | None]
    origin_responder_name: Mapped[str | None]
    requested_work: Mapped[str | None]
    requested_repo: Mapped[str | None]
    encrypted_origin_followup: Mapped[bytes | None]
    origin_followup_expires_at: Mapped[datetime | None]
    connected_repos: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB)
    notice_claimed_at: Mapped[datetime | None]
    notice_delivered_at: Mapped[datetime | None]
    notice_attempts: Mapped[int] = mapped_column(server_default=text("0"))
    notice_next_attempt_at: Mapped[datetime | None]
    encrypted_token: Mapped[bytes | None]
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]


class GitHubConnectClickIntent(Base):
    """Opaque requester-bound Discord button that mints on first click."""

    __tablename__ = "github_connect_click_intents"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    requester_account_id: Mapped[AccountFK]
    requester_platform_user_id: Mapped[str]
    agent_id: Mapped[uuid.UUID]
    agent_name: Mapped[str]
    agent_ma_id: Mapped[str | None]
    origin_parent_channel_id: Mapped[str]
    origin_thread_id: Mapped[str]
    origin_ma_agent_id: Mapped[str]
    origin_responder_name: Mapped[str]
    requested_work: Mapped[str | None]
    requested_repo: Mapped[str | None]
    encrypted_token: Mapped[bytes | None]
    expires_at: Mapped[datetime] = mapped_column(index=True)


class GitHubConnectFlow(Base):
    __tablename__ = "github_connect_flows"
    state_hash: Mapped[str] = mapped_column(primary_key=True)
    invitation_hash: Mapped[str] = mapped_column(
        ForeignKey("github_connect_invitations.token_hash", ondelete="CASCADE")
    )
    cookie_hash: Mapped[str]
    encrypted_verifier: Mapped[bytes]
    encrypted_invitation_token: Mapped[bytes | None]
    encrypted_user_token: Mapped[bytes | None]
    github_user_id: Mapped[int | None] = mapped_column(BigInteger)
    cancelled_at: Mapped[datetime | None]
    expires_at: Mapped[datetime]


class GitHubConnectRequest(Base):
    __tablename__ = "github_connect_requests"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "requester_account_id", "agent_id", name="uq_github_connect_request"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[TenantFK]
    requester_account_id: Mapped[AccountFK]
    agent_id: Mapped[uuid.UUID]
    agent_name: Mapped[str]
    requested_at: Mapped[TsNow]


class AgentGitHubGrant(Base):
    __tablename__ = "agent_github_grants"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id", "repo_id"),
        # Several confirmations may share (tenant_id, repo_id), so grants no
        # longer reference one; the stores remove grants with their repo.
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        CheckConstraint("baseline_access IN ('none', 'read', 'write')"),
        CheckConstraint("ceiling_access IN ('read', 'write')"),
        CheckConstraint(
            "baseline_access = 'none' OR baseline_access = 'read' OR ceiling_access = 'write'"
        ),
    )
    # max_access belongs to tenant_github_repos, so the grant-writing store
    # must enforce ceiling_access <= max_access in the same transaction.
    tenant_id: Mapped[uuid.UUID]
    agent_id: Mapped[uuid.UUID]
    repo_id: Mapped[int] = mapped_column(BigInteger)
    baseline_access: Mapped[str]
    ceiling_access: Mapped[str]
    mount_path: Mapped[str | None]
    is_working_repo: Mapped[FalseFlag]
    # The expand phase keeps the old `staged` column for old workers. A database
    # trigger mirrors its inverse here; new code reads only this field.
    app_visible: Mapped[bool] = mapped_column(server_default=text("true"))
    granted_by_account_id: Mapped[AccountSetNull]
    granted_at: Mapped[TsNow]
    version: Mapped[int] = mapped_column(server_default="1")


class AgentGitHubGrantRemoval(Base):
    """A repo an admin took off an agent, so an agent that follows its org is not given it back.

    Written when a grant is removed; cleared when the grant is staged again.
    """

    __tablename__ = "agent_github_grant_removals"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id", "repo_id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    tenant_id: Mapped[uuid.UUID]
    agent_id: Mapped[uuid.UUID]
    repo_id: Mapped[int] = mapped_column(BigInteger)
    removed_at: Mapped[TsNow]


class AgentGitHubGrantDraft(Base):
    __tablename__ = "agent_github_grant_drafts"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id", "repo_id"),
        # Several confirmations may share (tenant_id, repo_id), so grants no
        # longer reference one; the stores remove grants with their repo.
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        CheckConstraint("operation IN ('upsert', 'remove')"),
    )
    tenant_id: Mapped[uuid.UUID]
    agent_id: Mapped[uuid.UUID]
    repo_id: Mapped[int] = mapped_column(BigInteger)
    operation: Mapped[str]
    baseline_access: Mapped[str | None]
    ceiling_access: Mapped[str | None]
    is_working_repo: Mapped[FalseFlag]
    granted_by_account_id: Mapped[AccountSetNull]


class GitHubNewRepoNotice(Base):
    """A new installation repository awaiting a workspace-admin announcement."""

    __tablename__ = "github_new_repo_notices"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "installation_id", "repo_full_name"),)
    tenant_id: Mapped[TenantFK]
    installation_id: Mapped[int] = mapped_column(BigInteger)
    repo_full_name: Mapped[str]
    queued_at: Mapped[TsNow]
    delivered_at: Mapped[datetime | None]
    claimed_at: Mapped[datetime | None]
    dismissed_at: Mapped[datetime | None]


class GitHubRemovalNotice(Base):
    """One admin notice after GitHub removes an installation."""

    __tablename__ = "github_removal_notices"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "installation_id"),)
    tenant_id: Mapped[TenantFK]
    installation_id: Mapped[int] = mapped_column(BigInteger)
    account_login: Mapped[str]
    queued_at: Mapped[TsNow]
    confirmed_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]
    claimed_at: Mapped[datetime | None]


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
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    requester_account_id: Mapped[AccountFK]
    requester_platform_user_id: Mapped[str]
    platform: Mapped[str]
    parent_channel_id: Mapped[str]
    thread_id: Mapped[str]
    agent_id: Mapped[uuid.UUID]
    ma_agent_id: Mapped[str]
    agent_name: Mapped[str]
    repo_names: Mapped[list[str]] = mapped_column(JSONB)
    required_ability: Mapped[str] = mapped_column(server_default="read")
    approved_by_account_id: Mapped[AccountSetNull]
    requested_work: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default="open")
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNow]
    expires_at: Mapped[datetime]
    admin_notified_at: Mapped[datetime | None]
    admin_card_message_id: Mapped[str | None]
    resumed_at: Mapped[datetime | None]
    expiry_notice_sent_at: Mapped[datetime | None]


class GitHubGrantProposal(Base):
    """A repo grant proposed in one chat turn for confirmation in a later turn."""

    __tablename__ = "github_grant_proposals"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "requester_account_id",
            "platform",
            "requester_platform_user_id",
            "thread_id",
            name="uq_github_grant_proposal",
        ),
        Index("github_grant_proposals_expiry_idx", "expires_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    requester_account_id: Mapped[AccountFK]
    requester_platform_user_id: Mapped[str]
    platform: Mapped[str]
    thread_id: Mapped[str]
    agent_id: Mapped[uuid.UUID]
    repo_name: Mapped[str]
    ability: Mapped[str]
    origin_id: Mapped[uuid.UUID]
    approved_origin_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]


class GitHubAccessRequestDelivery(Base):
    """One request card slot per recipient in the originating conversation."""

    __tablename__ = "github_access_request_deliveries"
    __table_args__ = (PrimaryKeyConstraint("request_id", "recipient_account_id"),)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("github_access_requests.id", ondelete="CASCADE")
    )
    recipient_account_id: Mapped[AccountFK]
    platform_user_id: Mapped[str]
    message_id: Mapped[str | None]
    delivered_at: Mapped[datetime | None]
    dismissed_at: Mapped[datetime | None]


class GitHubUserLink(Base):
    __tablename__ = "github_user_links"
    __table_args__ = (CheckConstraint("status IN ('active', 'broken')"),)
    github_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    login: Mapped[str]
    encrypted_access_token: Mapped[bytes]
    encrypted_refresh_token: Mapped[bytes | None]
    access_expires_at: Mapped[datetime]
    refresh_expires_at: Mapped[datetime | None]
    token_generation: Mapped[int] = mapped_column(server_default="1")
    link_generation: Mapped[int] = mapped_column(server_default="1")
    status: Mapped[str] = mapped_column(server_default="active")
    linked_at: Mapped[TsNow]


class AccountGitHubLink(Base):
    __tablename__ = "account_github_links"
    __table_args__ = (
        CheckConstraint("verified_via IN ('discord_oauth', 'slack_oidc', 'auto_attach_discord')"),
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    github_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("github_user_links.github_user_id", ondelete="CASCADE")
    )
    platform: Mapped[str]
    platform_user_id: Mapped[str]
    verified_via: Mapped[str]
    linked_at: Mapped[TsNow]


class GitHubPersonalLinkIntent(Base):
    __tablename__ = "github_personal_link_intents"
    __table_args__ = (
        CheckConstraint("platform IN ('discord', 'slack')"),
        CheckConstraint("phase IN ('new', 'platform', 'github', 'used')"),
        Index("ix_github_personal_link_intents_expires_at", "expires_at"),
        Index("uq_github_personal_link_intents_platform_state", "platform_state", unique=True),
        Index("uq_github_personal_link_intents_github_state", "github_state", unique=True),
    )
    token_hash: Mapped[str] = mapped_column(primary_key=True)
    account_id: Mapped[AccountFK]
    tenant_id: Mapped[TenantFK]
    platform: Mapped[str]
    platform_user_id: Mapped[str]
    platform_workspace_id: Mapped[str]
    platform_state: Mapped[str | None]
    github_state: Mapped[str | None]
    browser_cookie_hash: Mapped[str | None]
    encrypted_verifier: Mapped[bytes | None]
    phase: Mapped[str] = mapped_column(server_default="new")
    expires_at: Mapped[datetime]
    created_at: Mapped[TsNow]


class GitHubIssuedToken(Base):
    __tablename__ = "github_issued_tokens"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'stored', 'delivered', 'revoked')"),
        # Revoked rows are most of the table and no query reads them.
        Index(
            "ix_github_issued_tokens_live_session",
            "session_id",
            postgresql_where=text("status IN ('stored', 'delivered')"),
        ),
        Index(
            "ix_github_issued_tokens_router_session",
            "router_session_id",
            postgresql_where=text("router_session_id IS NOT NULL AND status = 'delivered'"),
        ),
    )
    token_id: Mapped[UuidPk]
    # Tenant deletion must revoke live installation tokens before this row cascades.
    tenant_id: Mapped[TenantFK]
    agent_id: Mapped[uuid.UUID]
    session_id: Mapped[str]
    installation_id: Mapped[int] = mapped_column(BigInteger)
    repo_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    permissions: Mapped[dict[str, str]] = mapped_column(JSONB)
    grant_versions: Mapped[dict[str, int]] = mapped_column(JSONB)
    requester_account_id: Mapped[AccountSetNull]
    github_user_id: Mapped[int | None] = mapped_column(BigInteger)
    link_generation: Mapped[int | None]
    encrypted_token: Mapped[bytes | None]
    expires_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(server_default="pending")
    revoked_at: Mapped[datetime | None]
    revoke_attempts: Mapped[int] = mapped_column(server_default="0")
    superseded_at: Mapped[datetime | None]
    revoke_after: Mapped[datetime | None]
    created_at: Mapped[TsNow]
    # The router session this token serves, stamped on every row a routed
    # session mints, so the router finds the replacement after a rotation.
    router_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("github_router_sessions.id", ondelete="SET NULL")
    )


class GitHubRouterSession(Base):
    """A session that reaches GitHub's MCP server through Daimon's token router.

    The router authenticates the session by an opaque bearer held in the
    session's vault; only its SHA-256 is stored. Closing the row is the local
    revocation: a closed or expired row authenticates nothing.
    """

    __tablename__ = "github_router_sessions"
    __table_args__ = (
        Index(
            "ix_github_router_sessions_open_ma_session",
            "ma_session_id",
            postgresql_where=text("closed_at IS NULL"),
        ),
        Index(
            "ix_github_router_sessions_open_account",
            "account_id",
            postgresql_where=text("closed_at IS NULL"),
        ),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    agent_id: Mapped[uuid.UUID]
    # Null for shared and headless sessions, and after account erasure.
    account_id: Mapped[AccountSetNull]
    # Set once Managed Agents has created the session.
    ma_session_id: Mapped[str | None]
    credential_sha256: Mapped[bytes] = mapped_column(unique=True)
    created_at: Mapped[TsNow]
    expires_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]


class GitHubAppSessionVault(Base):
    __tablename__ = "github_app_session_vaults"
    __table_args__ = (Index("ix_github_app_session_vaults_mcp_open", "is_mcp", "closed_at"),)

    session_id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    vault_id: Mapped[str]
    is_unmapped: Mapped[bool]
    is_mcp: Mapped[FalseFlag]
    agent_id: Mapped[uuid.UUID | None]
    account_id: Mapped[AccountSetNull]
    repo_urls: Mapped[list[str] | None] = mapped_column(JSONB)
    repo_resource_ids: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    created_at: Mapped[TsNow]
    last_started_at: Mapped[TsNow]
    finished_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]


class SecurityAuditEvent(Base):
    """Append-only security metadata with dedicated erasure and retention maintenance."""

    __tablename__ = "security_audit_events"
    admin_subject_kind: Mapped[str | None]
    admin_subject_id: Mapped[str | None]

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
    id: Mapped[UuidPk]
    tenant_id: Mapped[uuid.UUID]
    account_id: Mapped[uuid.UUID | None]
    agent_id: Mapped[uuid.UUID | None]
    agent_name: Mapped[str | None]
    platform: Mapped[str | None]
    platform_user_id: Mapped[str | None]
    tool_name: Mapped[str]
    operation: Mapped[str | None]
    outcome: Mapped[str]
    reason: Mapped[str]
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp())
    token_kind: Mapped[str | None]
    token_jti: Mapped[uuid.UUID | None]
    scope: Mapped[str | None]
    github_token_id: Mapped[uuid.UUID | None]
    github_session_id: Mapped[str | None]
    github_installation_id: Mapped[int | None] = mapped_column(BigInteger)
    github_repo_ids: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger))
    github_permissions: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    github_grant_versions: Mapped[dict[str, int] | None] = mapped_column(JSONB)
    github_turn_origin_id: Mapped[uuid.UUID | None]
    github_expires_at: Mapped[datetime | None]
    # Set only by the channel tidy tools: which message an edit or delete
    # touched, a keyed HMAC of the text it replaced, and the turn it ran in.
    target_channel_id: Mapped[str | None]
    target_message_id: Mapped[str | None]
    content_hmac: Mapped[str | None]
    turn_ref: Mapped[str | None]


class AgentPostedMessage(Base):
    """A message or thread an agent posted.

    Written at send time by `send_message` and `create_thread` (`source='tool'`),
    and by chat adapters for a turn's status cards, answers and notices
    (`source='turn'`), the thread it opens from a mention
    (`source='auto_thread'`) and a routine's result (`source='routine'`).
    The channel tidy tools edit or delete only what
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
            "source IN ('tool', 'turn', 'auto_thread', 'routine')",
            name="ck_agent_posted_messages_source",
        ),
        CheckConstraint(
            "source <> 'turn' OR turn_card_intent_id IS NOT NULL",
            name="ck_agent_posted_messages_turn_intent",
        ),
        CheckConstraint(
            "role IN ('status', 'answer', 'notice', 'controls', 'routine')",
            name="ck_agent_posted_messages_role",
        ),
        Index(
            "ix_agent_posted_messages_delivery",
            "delivery_id",
            postgresql_where=text("delivery_id IS NOT NULL"),
        ),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    platform: Mapped[str]
    channel_id: Mapped[str]
    message_id: Mapped[str]
    parent_channel_id: Mapped[str | None]
    thread_ts: Mapped[str | None]
    kind: Mapped[str]
    agent_id: Mapped[uuid.UUID]
    content_hmac: Mapped[str | None]
    source: Mapped[str] = mapped_column(server_default=text("'tool'"))
    # The person whose message started the turn (`turn`), whose mention
    # opened the thread (`auto_thread`) or who made the routine (`routine`).
    # NULL for tool posts.
    requester_platform_user_id: Mapped[str | None]
    # The turn that posted a `turn` row; no FK, intents are pruned once retired.
    turn_card_intent_id: Mapped[uuid.UUID | None]
    # What a Teams turn or routine post is (`PostRole`), so the agent can tell
    # its answer from the cards around it. NULL elsewhere.
    role: Mapped[str | None]
    # One routine delivery: its chunks, which in a Teams channel each root
    # their own thread, so no channel groups them. NULL for other posts.
    delivery_id: Mapped[uuid.UUID | None]
    posted_at: Mapped[TsNow]
    deleted_at: Mapped[datetime | None]


class ReachTally(Base):
    """What one turn's tool posts reached so far, and the plan its requester approved.

    Several MCP instances serve one turn, so the tally lives here. Rows are kept
    for a day at most (`daimon.core.stores.reach_tallies`), so the platform ids
    they hold need no purge step.
    """

    __tablename__ = "reach_tallies"

    tenant_id: Mapped[TenantPk]
    turn_key: Mapped[str] = mapped_column(primary_key=True)
    threads: Mapped[int] = mapped_column(server_default=text("0"))
    own_threads: Mapped[int] = mapped_column(server_default=text("0"))
    places: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    people: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    approved_threads: Mapped[int | None]
    approved_places: Mapped[int | None]
    approved_people: Mapped[int | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)


class TagOptOut(Base):
    """A person who asked never to be tagged, by their account.

    Keyed by account so the choice follows the person across every linked
    platform identity; deleted with the account.
    """

    __tablename__ = "tag_opt_outs"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[TenantFK]
    created_at: Mapped[TsNow]


class DmCarryOver(Base):
    """An earlier private conversation, held for the person's first DM here.

    Keyed by platform user, not account: it is imported before the person has
    one. Only a private start with `agent_name` takes it, whichever agent a person's
    DMs route to. Taking it sets `consumed_at` and empties the words.
    """

    __tablename__ = "dm_carry_overs"

    tenant_id: Mapped[TenantPk]
    platform: Mapped[str] = mapped_column(primary_key=True)
    external_user_id: Mapped[str] = mapped_column(primary_key=True)
    agent_name: Mapped[str]
    source_name: Mapped[str]
    exported_at: Mapped[datetime]
    history: Mapped[list[dict[str, str]]] = mapped_column(JSONB)
    context: Mapped[str]
    imported_at: Mapped[TsNow]
    consumed_at: Mapped[datetime | None]


class HeldTimer(Base):
    """An imported one-shot timer, held until an operator confirms it for an agent.

    Settling one sets `settled_at` and `outcome` and empties the note; the row stays
    so a repeated import adds nothing.
    """

    __tablename__ = "held_timers"
    __table_args__ = (
        CheckConstraint("(settled_at IS NULL) = (outcome IS NULL)", name="ck_held_timers_settled"),
        Index("ix_held_timers_account", "account_id"),
    )

    tenant_id: Mapped[TenantPk]
    source_id: Mapped[str] = mapped_column(primary_key=True)
    #: The agent the import was made for; only its go-live and `--full` runs touch the row.
    agent_id: Mapped[str]
    account_id: Mapped[AccountFK]
    external_user_id: Mapped[str]
    platform: Mapped[str]
    channel_id: Mapped[str]
    thread_id: Mapped[str | None]
    place_type: Mapped[str]
    fire_at: Mapped[datetime]
    note: Mapped[str]
    imported_at: Mapped[TsNow]
    settled_at: Mapped[datetime | None]
    outcome: Mapped[str | None]
    #: Paused where it came from: never scheduled, whatever is confirmed.
    paused: Mapped[bool] = mapped_column(server_default=text("false"), default=False)


class DirectMessagePolicy(Base):
    """Explicit tenant opt-in; absence leaves existing DM behavior unchanged."""

    __tablename__ = "direct_message_policies"
    tenant_id: Mapped[TenantPk]
    dm_agent_name: Mapped[str | None]
    enabled: Mapped[FalseFlag]


class DmAgentGrant(Base):
    """Who may start a direct message with one agent. No row: every member."""

    __tablename__ = "dm_agent_grants"
    __table_args__ = (
        CheckConstraint("who IN ('members', 'listed', 'excluded')", name="ck_dm_agent_grants_who"),
    )

    tenant_id: Mapped[TenantPk]
    agent_name: Mapped[str] = mapped_column(primary_key=True)
    who: Mapped[str] = mapped_column(server_default="members")
    user_ids: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    group_ids: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    set_by: Mapped[str | None]
    updated_at: Mapped[TsNow]


class DmRule(Base):
    """One ordered routing rule: who lands on which agent, optionally locked."""

    __tablename__ = "dm_rules"
    __table_args__ = (
        CheckConstraint("subject IN ('user', 'group', 'everyone')", name="ck_dm_rules_subject"),
        CheckConstraint(
            "(subject = 'everyone') = (subject_id IS NULL)", name="ck_dm_rules_subject_id"
        ),
        CheckConstraint("NOT locked OR agent_name IS NOT NULL", name="ck_dm_rules_locked_agent"),
    )

    tenant_id: Mapped[TenantPk]
    position: Mapped[int] = mapped_column(primary_key=True)
    subject: Mapped[str]
    subject_id: Mapped[str | None]
    agent_name: Mapped[str | None]
    locked: Mapped[FalseFlag]
    set_by: Mapped[str | None]
    updated_at: Mapped[TsNow]


class DmAgentPick(Base):
    """The agent a person chose for their direct messages in a workspace."""

    __tablename__ = "dm_agent_picks"

    tenant_id: Mapped[TenantPk]
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    agent_name: Mapped[str]
    updated_at: Mapped[TsNow]


class DirectMessageConversation(Base):
    """One user's selected workspace for a private platform conversation."""

    __tablename__ = "direct_message_conversations"
    platform: Mapped[str] = mapped_column(primary_key=True)
    route_key: Mapped[str] = mapped_column(primary_key=True)
    external_user_id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    account_id: Mapped[AccountFK]
    workspace_id: Mapped[str]
    channel_id: Mapped[str]
    scope_id: Mapped[str]
    source_url: Mapped[str]
    # The parent channel (and thread) /dm-me was run in; re-checked against seals
    # each turn, and the DM's spend counts toward that channel's budget.
    source_channel_id: Mapped[str | None]
    source_thread_id: Mapped[str | None]
    # Slack: channel:thread_ts of every copied message, so later thread seals match.
    source_thread_keys: Mapped[list[str] | None]
    context: Mapped[str]
    memory_read_only: Mapped[bool]
    history: Mapped[list[dict[str, str]]] = mapped_column(JSONB)
    recent_message_ids: Mapped[list[str]]
    # Set while a delivery's turn holds the conversation: an identity compared on finish
    # and delivery, written as a time past the longest turn so an older image, which reads
    # it as a deadline (the column keeps its first name), still sees the claim live.
    claimed_at: Mapped[datetime | None] = mapped_column("active_until", DateTime(timezone=True))


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

    tenant_id: Mapped[uuid.UUID]
    platform: Mapped[str]
    platform_user_id: Mapped[str]
    #: What the platform shows for them: a Discord server nickname or global
    #: name, a Slack display or real name, a Teams name.
    display_name: Mapped[str | None]
    #: Their account handle: a Discord username, a Slack username.
    handle: Mapped[str | None]
    updated_at: Mapped[TsNow]


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
        CheckConstraint(
            "status IN ('named', 'gone', 'direct')", name="ck_platform_channel_names_status"
        ),
    )

    tenant_id: Mapped[uuid.UUID]
    platform: Mapped[str]
    channel_id: Mapped[str]
    name: Mapped[str]
    updated_at: Mapped[TsNow]
    # What the adapter that saw the channel also knew: its kind (text, forum,
    # private, shared...) and its Discord category or Teams team.
    kind: Mapped[str | None]
    parent_id: Mapped[str | None]
    parent_name: Mapped[str | None]
    last_seen_at: Mapped[datetime | None]
    archived: Mapped[bool | None]
    # Whether the platform still has it: "named", "gone" (deleted) or "direct" (a DM).
    status: Mapped[str] = mapped_column(server_default="named")


class ChannelChange(Base):
    """One change to a channel setting, from any surface, with its before and after values.

    Keeps its rows when the actor's account is erased: the actor is a SET NULL reference
    and the values name agents and amounts, never a person.
    """

    __tablename__ = "channel_changes"
    __table_args__ = (
        Index("ix_channel_changes_tenant_channel_at", "tenant_id", "channel_id", "at"),
        CheckConstraint(
            "surface IN ('web', 'chat', 'panel', 'cli', 'system')",
            name="ck_channel_changes_surface",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[TenantFK]
    platform: Mapped[str]
    channel_id: Mapped[str]
    setting: Mapped[str]
    before: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True))
    after: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True))
    actor_account_id: Mapped[AccountSetNull]
    surface: Mapped[str]
    at: Mapped[TsNow]


# The provider-neutral state store (`daimon.core.stores.mux_state`, the
# Postgres `mux.state.store.StateStore`). Nothing in Daimon reads or writes
# these tables yet. Contract ids that are not Daimon UUIDs (account and
# principal ids, binding ids, native session ids) are Text; every row carries
# its tenant for isolation and for the tenant purge cascade.


def _tenant_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )


class ChannelConfigRevision(Base):
    """One immutable revision of a channel's resolved backend configuration."""

    __tablename__ = "channel_config_revision"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "platform", "channel_id", "local"),)

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    platform: Mapped[str]
    channel_id: Mapped[str]
    local: Mapped[int]
    digest: Mapped[str]
    origin: Mapped[str] = mapped_column(server_default=text("'channel'"))
    agent_backend_ref: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True))
    # A `mux.contracts.config.ConfigRevision` as JSON.
    revision: Mapped[dict[str, Any]]
    created_at: Mapped[TsNow]


class ProviderBindingSlot(Base):
    """A binding slot and the one binding id that lives in it.

    A slot is a thread plus the caller account owning it (NULL = a shared
    thread), unique with NULLS NOT DISTINCT. `generation` is the slot's
    current generation, the compare-and-swap target.
    """

    __tablename__ = "provider_binding_slot"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "platform",
            "channel_id",
            "thread_id",
            "account_id",
            name="uq_provider_binding_slot",
            postgresql_nulls_not_distinct=True,
        ),
    )

    binding_id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    platform: Mapped[str]
    channel_id: Mapped[str]
    thread_id: Mapped[str]
    account_id: Mapped[str | None]
    generation: Mapped[int]


class ProviderBinding(Base):
    """Every generation of a binding; the slot's current one is the highest."""

    __tablename__ = "provider_binding"
    __table_args__ = (
        PrimaryKeyConstraint("binding_id", "generation"),
        Index(
            "provider_binding_legacy_row_idx",
            "legacy_row_id",
            postgresql_where=text("legacy_row_id IS NOT NULL"),
        ),
    )

    binding_id: Mapped[str] = mapped_column(
        ForeignKey("provider_binding_slot.binding_id", ondelete="CASCADE")
    )
    generation: Mapped[int]
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    provider: Mapped[str]
    profile: Mapped[str]
    # A `mux.contracts.resources.ProviderBinding` as JSON.
    binding: Mapped[dict[str, Any]]
    # The `thread_sessions` row a backfilled generation came from; NULL otherwise.
    legacy_row_id: Mapped[uuid.UUID | None]
    created_at: Mapped[TsNow]


class ThreadLease(Base):
    """A slot's lease: the highest fence issued and the active holder, if any."""

    __tablename__ = "thread_lease"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "platform",
            "channel_id",
            "thread_id",
            "account_id",
            name="uq_thread_lease_slot",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    platform: Mapped[str]
    channel_id: Mapped[str]
    thread_id: Mapped[str]
    account_id: Mapped[str | None]
    last_fence: Mapped[int] = mapped_column(server_default=text("0"))
    # A `mux.state.lease.Lease` as JSON, or NULL when nobody holds the slot.
    active: Mapped[dict[str, Any] | None]


class ThreadProviderRelease(Base):
    """A confirmed provider switch's request to move one caller's thread.

    The next admitted turn in the slot applies it (`daimon.core.thread_release`): when the
    slot's binding is still at `generation` (0 = unbound), it writes a released
    successor through the binding compare-and-swap, and the turn then selects its
    provider afresh. Applied or stale, the row is deleted.
    """

    __tablename__ = "thread_provider_release"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "platform",
            "channel_id",
            "thread_id",
            "account_id",
            name="uq_thread_provider_release_slot",
        ),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    platform: Mapped[str]
    channel_id: Mapped[str]
    thread_id: Mapped[str]
    account_id: Mapped[str]
    generation: Mapped[int]
    requested_at: Mapped[datetime]


class BackendSwitchCard(Base):
    """A provider switch waiting on its requester's Confirm card (`daimon.core.switch_cards`).

    Posted by Daimon itself for a turn whose provider shows no confirmation
    card (OpenAI, Gemini). The click re-verifies the reference before applying;
    the status moves pending -> claimed -> applied/refused exactly once.
    """

    __tablename__ = "backend_switch_cards"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'claimed', 'applied', 'refused', 'cancelled')",
            name="ck_backend_switch_cards_status",
        ),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    account_id: Mapped[uuid.UUID]
    platform: Mapped[str]
    channel_id: Mapped[str]
    thread_id: Mapped[str]
    setup_thread: Mapped[FalseFlag]
    requester_platform_user_id: Mapped[str]
    # The requester is from another organisation: never an admin at the press.
    client: Mapped[FalseFlag]
    answering_agent_name: Mapped[str | None]
    reference: Mapped[Any] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(server_default=text("'pending'"))
    outcome: Mapped[str | None]
    message_id: Mapped[str | None]
    expires_at: Mapped[datetime]
    created_at: Mapped[TsNow]


class HostCallCard(Base):
    """A gated tool call waiting on its requester's Approve card (`daimon.core.host_calls`).

    Recorded for a provider-hosted turn, which has no confirmation card of its own. The
    status moves pending -> approved -> executing -> applied/failed, or ends denied,
    cancelled or expired, each step exactly once. No foreign key to `turn_origins`: the
    origin is gone by the time anyone presses.
    """

    __tablename__ = "host_call_cards"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'executing', 'applied', 'failed', 'denied', "
            "'cancelled', 'expired')",
            name="ck_host_call_cards_status",
        ),
        Index(
            "uq_host_call_cards_pending_call",
            "origin_id",
            "tool_name",
            "args_digest",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_host_call_cards_thread_status", "tenant_id", "platform", "thread_id", "status"),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    origin_id: Mapped[uuid.UUID]
    requester_account_id: Mapped[AccountFK]
    requester_platform_user_id: Mapped[str]
    platform: Mapped[str]
    channel_id: Mapped[str]
    thread_id: Mapped[str]
    executing_agent_id: Mapped[uuid.UUID]
    executing_ma_agent_id: Mapped[str]
    agent_name: Mapped[str]
    tool_name: Mapped[str]
    args_digest: Mapped[str]
    reference: Mapped[Any] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(server_default=text("'pending'"))
    outcome: Mapped[str | None]
    message_id: Mapped[str | None]
    expires_at: Mapped[datetime]
    executing_at: Mapped[datetime | None]
    created_at: Mapped[TsNow]


class MuxOperation(Base):
    """An idempotent operation: found by (tenant, account, key), owned by a principal.

    The principal is deliberately not in the key: reusing a key from another
    principal is refused, never forked into a second operation.
    """

    __tablename__ = "operation"
    __table_args__ = (PrimaryKeyConstraint("tenant_id", "account_id", "key"),)

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    account_id: Mapped[str]
    key: Mapped[str]
    principal_id: Mapped[str]
    status: Mapped[str]
    # A `mux.state.operations.OperationRecord` as JSON.
    record: Mapped[dict[str, Any]]
    updated_at: Mapped[TsNow]


class JournalSession(Base):
    """A session's journal head: its owning binding (hence slot), projection and cursor.

    Written when a binding names the session, never by an append.
    """

    __tablename__ = "journal_session"

    session_id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    binding_id: Mapped[str] = mapped_column(
        ForeignKey("provider_binding_slot.binding_id", ondelete="CASCADE")
    )
    next_sequence: Mapped[int] = mapped_column(server_default=text("0"))
    # A `mux.contracts.resources.ProjectionSnapshot` as JSON; NULL before the first append.
    projection: Mapped[dict[str, Any] | None]


class Journal(Base):
    """One normalized event, in local commit order."""

    __tablename__ = "journal"
    __table_args__ = (
        PrimaryKeyConstraint("session_id", "sequence"),
        UniqueConstraint(
            "session_id", "source_key", "revision", "preview", name="uq_journal_source"
        ),
    )

    session_id: Mapped[str] = mapped_column(
        ForeignKey("journal_session.session_id", ondelete="CASCADE")
    )
    sequence: Mapped[int]
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    source_key: Mapped[str]
    revision: Mapped[int]
    preview: Mapped[bool]
    # A `mux.contracts.events.Event` as JSON.
    event: Mapped[dict[str, Any]]


class UsageObservation(Base):
    """Each applied revision of a usage observation; the latest is the highest."""

    __tablename__ = "usage_observation"
    __table_args__ = (
        PrimaryKeyConstraint("binding_id", "observation_id", "revision"),
        # The OpenAI daily model cap reads one tenant's observations per turn.
        Index("ix_usage_observation_tenant_id", "tenant_id"),
        # The provider daily ceiling reads today's observations of one provider.
        Index("ix_usage_observation_provider_observed_at", "provider", "observed_at"),
    )

    # No foreign key to the slot: erasing a private slot must leave these rows
    # (see `AccountingBindingContext`).
    binding_id: Mapped[str]
    observation_id: Mapped[str]
    revision: Mapped[int]
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    # A `mux.state.usage_ledger.AppliedUsage` as JSON.
    applied: Mapped[dict[str, Any]]
    # Copied from `applied` for the daily reads; NULL on a row older code wrote.
    provider: Mapped[str | None]
    observed_at: Mapped[datetime | None]


class AccountingOutbox(Base):
    """One revision's signed usage deltas, for the host to apply exactly once."""

    __tablename__ = "accounting_outbox"
    __table_args__ = (
        PrimaryKeyConstraint("binding_id", "observation_id", "revision"),
        Index(
            "accounting_outbox_pending_idx",
            "created_at",
            postgresql_where=text("applied_at IS NULL"),
        ),
    )

    # No foreign key to the slot: erasing a private slot must leave these rows
    # (see `AccountingBindingContext`).
    binding_id: Mapped[str]
    observation_id: Mapped[str]
    revision: Mapped[int]
    prior_applied_revision: Mapped[int | None]
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    # A `mux.state.usage_ledger.OutboxRow` as JSON (its `applied` flag is `applied_at`).
    row: Mapped[dict[str, Any]]
    # Who asked and the budget channel, as recorded: a row settled later debits them.
    platform_user_id: Mapped[str | None]
    channel_id: Mapped[str | None]
    created_at: Mapped[TsNow]
    applied_at: Mapped[datetime | None]


class AccountingBindingContext(Base):
    """What settling a binding's usage rows needs once its slot has been erased.

    Account erasure deletes a private slot, its binding generations and its journal,
    but the slot's `usage_observation` and `accounting_outbox` rows are financial
    evidence the tenant still settles from (pending rows) and replays against (applied
    rows). Those rows no longer hang off the slot, so the erasure copies the two facts
    the host reads from the binding when it settles them: the profile (to price) and the
    channel (to attribute the debit). It holds no thread, account, session or content.
    """

    __tablename__ = "accounting_binding_context"

    binding_id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    platform: Mapped[str]
    channel_id: Mapped[str]
    # The newest generation's profile, and every profile the binding ever had: the
    # pending-usage drain settles only bindings that never left its profiles.
    profile: Mapped[str]
    profiles: Mapped[list[str]]


class ErasedPrincipal(Base):
    """A principal of an erased account: the fence for operations keyed by principal.

    An operation is found by account but owned by a principal, and erasure removes the
    ones owned by the account's principals, so a stale scope naming such a principal is
    refused as well (see `ErasedAccount`).
    """

    __tablename__ = "erased_principal"

    principal_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    account_id: Mapped[uuid.UUID]
    erased_at: Mapped[TsNow]


class ErasedAccount(Base):
    """An account that was erased: the durable half of the mux writer fence.

    A mux writer holds a shared lock on its account while it writes, and purge holds the
    exclusive one, so a write either finishes before the erasure or finds this row after
    it. The account id is a bare identifier; nothing else about the account is kept.
    """

    __tablename__ = "erased_account"

    account_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    erased_at: Mapped[TsNow]


class NeutralAgentRevision(Base):
    """Immutable host revisions for explicitly selected alternate backends."""

    __tablename__ = "neutral_agent_revisions"
    __table_args__ = (
        CheckConstraint("provider IN ('openai', 'gemini')", name="ck_neutral_agent_provider"),
        CheckConstraint("local_revision > 0", name="ck_neutral_agent_revision"),
    )
    tenant_id: Mapped[TenantPk]
    # No account key: a shared thread's owner has no account row (0204).
    account_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(primary_key=True)
    account_scope_id: Mapped[str] = mapped_column(primary_key=True)
    catalog_id: Mapped[str] = mapped_column(primary_key=True)
    local_revision: Mapped[int] = mapped_column(primary_key=True)
    principal_id: Mapped[str]
    agent: Mapped[dict[str, Any]]


class NeutralSkillVersion(Base):
    """Retained checked bundles; native skill versions remain opaque strings."""

    __tablename__ = "neutral_skill_versions"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('openai', 'gemini', 'selfhosted')", name="ck_neutral_skill_provider"
        ),
    )
    tenant_id: Mapped[TenantPk]
    # No account key: a shared thread's owner has no account row (0204).
    account_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(primary_key=True)
    account_scope_id: Mapped[str] = mapped_column(primary_key=True)
    skill_id: Mapped[str] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(primary_key=True)
    principal_id: Mapped[str]
    agent_name: Mapped[str]
    display_title: Mapped[str]
    preview: Mapped[dict[str, Any]]
    zip_bytes: Mapped[bytes]


class DiscordMessageAdmission(Base):
    """Durable addressed-message admission, shared by gateway and startup replay."""

    __tablename__ = "discord_message_admissions"
    __table_args__ = (
        Index("ix_discord_admissions_activity", "tenant_id", "created_at"),
        Index("ix_discord_admissions_channel", "tenant_id", "channel_id", "created_at"),
    )
    tenant_id: Mapped[TenantPk]
    message_id: Mapped[str] = mapped_column(primary_key=True)
    channel_id: Mapped[str]
    owner_key: Mapped[int] = mapped_column(BigInteger)
    turn_card_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("turn_card_intents.id", ondelete="SET NULL")
    )
    handled: Mapped[FalseFlag]
    created_at: Mapped[TsNow]


class DiscordWorkerHeartbeat(Base):
    """Last confirmed lock ownership, retained across an abrupt connection loss."""

    __tablename__ = "discord_worker_heartbeats"
    owner_key: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seen_at: Mapped[datetime]
    lease_aware: Mapped[FalseFlag]


class DiscordGatewayLease(Base):
    """The last process to hold the Discord gateway lease, and whether it let go cleanly."""

    __tablename__ = "discord_gateway_lease"
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    holder_owner_key: Mapped[int] = mapped_column(BigInteger)
    acquired_at: Mapped[datetime]
    released_at: Mapped[datetime | None]


class ProviderKeyCooldown(Base):
    """When a provider key may be used again for one model, after a rate limit.

    Keyed by a digest of the key, never the key itself. Private to
    `daimon.core.stores.provider_key_gate`.
    """

    __tablename__ = "provider_key_cooldowns"
    provider: Mapped[str] = mapped_column(primary_key=True)
    key_digest: Mapped[str] = mapped_column(primary_key=True)
    model: Mapped[str] = mapped_column(primary_key=True)
    until: Mapped[datetime]


class ProviderKeyLease(Base):
    """One running turn on a provider key; it expires if the turn never releases it."""

    __tablename__ = "provider_key_leases"
    __table_args__ = (Index("ix_provider_key_leases_key", "provider", "key_digest", "tenant_id"),)

    id: Mapped[UuidPk]
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE", name="fk_provider_key_leases_tenants")
    )
    provider: Mapped[str]
    key_digest: Mapped[str]
    expires_at: Mapped[datetime]


class SessionSettleState(Base):
    """Whether a headless send left one MA session to be settled.

    `settle_requested_at` is written before the send and `settle_turn_event_id`
    (the send's own `user.message`) once it is accepted; both are cleared when
    that turn has been settled (`daimon.core.usage_sweep`). `covered_through`
    is read only on the reserved cutoff rows: `backlog-cutoff`, when this
    settle path was deployed, and `web-search-cutoff`, when search billing
    was. `unattended_turn_event_id` is the latest turn an agent-chat send
    started (`unattended_turn`), never cleared by a settle.

    The table keeps its original name; the former catch-up's cursor columns
    are no longer mapped and are dropped later. Private to
    `daimon.core.stores.**` per the import-linter ORM contract.
    """

    __tablename__ = "usage_reconcile_progress"

    managed_session_id: Mapped[str] = mapped_column(primary_key=True)
    covered_through: Mapped[datetime | None]
    settle_turn_event_id: Mapped[str | None]
    settle_requested_at: Mapped[datetime | None]
    # The latest turn an agent-chat send started here, and its tool-rule channel.
    unattended_turn_event_id: Mapped[str | None]
    unattended_rules_channel_id: Mapped[str | None]
    updated_at: Mapped[TsNow]


class AgentBackendRevisionRecord(Base):
    """Tenant/agent-scoped immutable backend settings, like channel revisions."""

    __tablename__ = "agent_backend_revision"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id", "local"),
        CheckConstraint("local > 0", name="ck_agent_backend_local"),
        CheckConstraint(
            "backend IS NULL OR backend IN ('anthropic', 'openai', 'gemini', 'selfhosted')",
            name="ck_agent_backend_provider",
        ),
        CheckConstraint(
            "(environment IS NULL) = (backend IS DISTINCT FROM 'selfhosted')"
            " AND (environment IS NULL OR left(environment, 7) = 'sh_env_')",
            name="ck_agent_backend_environment",
        ),
        CheckConstraint(
            "thread_mode IS NULL OR thread_mode IN ('per_caller', 'shared')",
            name="ck_agent_backend_thread_mode",
        ),
        CheckConstraint(
            "effort IS NULL OR effort IN ('low', 'medium', 'high')",
            name="ck_agent_backend_effort",
        ),
    )
    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    agent_id: Mapped[uuid.UUID]
    local: Mapped[int]
    backend: Mapped[str | None]
    agent_name: Mapped[str]
    preview_digest: Mapped[str]
    digest: Mapped[str]
    previous_local: Mapped[int | None]
    # A loss consent older rows kept; no longer written, read and ignored.
    drop_consent: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[TsNow]
    # The admin-picked model; null is the provider's default (0118, so last).
    model: Mapped[str | None]
    # The agent's own profile, requirements and thread mode; null is the
    # backend's default (0119).
    profile: Mapped[str | None]
    requires: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True))
    thread_mode: Mapped[str | None]
    # The agent's reasoning effort on its non-default provider; null keeps the model's own.
    effort: Mapped[str | None]
    # The self-hosted environment record a self-hosted agent runs in (0202).
    environment: Mapped[str | None]


class AgentOwner(Base):
    """The one account that may edit an agent without being a server admin.

    No row: only server admins may. Recorded when a person creates the agent
    from chat, or assigned by an admin; never inferred.
    """

    __tablename__ = "agent_owner"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id", name="pk_agent_owner"),
        CheckConstraint("source IN ('created', 'admin_assigned')", name="ck_agent_owner_source"),
        Index("ix_agent_owner_account", "owner_account_id"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE", name="fk_agent_owner_tenants")
    )
    agent_id: Mapped[uuid.UUID]
    owner_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE", name="fk_agent_owner_accounts")
    )
    source: Mapped[str]
    recorded_at: Mapped[TsNow]


class AgentCoOwner(Base):
    """An additional account that may edit an agent, beside its one primary owner.

    Added and removed by an admin only. The primary owner stays in `agent_owner`;
    a person in both places is simply an owner. Deleting the account deletes the
    row.
    """

    __tablename__ = "agent_co_owners"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id", "account_id", name="pk_agent_co_owners"),
        Index("ix_agent_co_owners_account", "account_id"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE", name="fk_agent_co_owners_tenants")
    )
    agent_id: Mapped[uuid.UUID]
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE", name="fk_agent_co_owners_accounts")
    )
    added_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL", name="fk_agent_co_owners_added_by")
    )
    added_at: Mapped[TsNow]


class GeminiRuntimeState(Base):
    """Driver records and conservative attempt holds on the host state database."""

    __tablename__ = "gemini_runtime_state"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "account_id", "project", "kind"),
        CheckConstraint("kind IN ('records', 'holds')", name="ck_gemini_runtime_state_kind"),
    )

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    account_id: Mapped[uuid.UUID]
    project: Mapped[str]
    kind: Mapped[str]
    body: Mapped[str | None]


class GeminiRecord(Base):
    """One Gemini driver record row: an account's operation log, a session, a resource.

    Split from the one `gemini_runtime_state` blob per account, so a turn locks and
    rewrites only its own session. `version` and `touched_at` change on every write.
    """

    __tablename__ = "gemini_records"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "account_id", "project", "kind", "id"),
        CheckConstraint(
            "kind IN ('account', 'session', 'agent', 'environment', 'skill', 'artifact')",
            name="ck_gemini_records_kind",
        ),
        Index("ix_gemini_records_kind_touched_at", "kind", "touched_at"),
    )

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    account_id: Mapped[uuid.UUID]
    project: Mapped[str]
    kind: Mapped[str]
    id: Mapped[str]
    body: Mapped[str]
    version: Mapped[int] = mapped_column(BigInteger, server_default=text("1"))
    touched_at: Mapped[TsNow]
    # A session's binding; an artifact's session (workspace snapshot files).
    binding_id: Mapped[str | None]
    session_id: Mapped[str | None]
    # A session's `kind:id` refs: its agent, environment, pinned skills and files.
    refs: Mapped[list[str]] = mapped_column(server_default=text("'{}'::text[]"))


class Watch(Base):
    """Owner-registered source and immutable conversation binding."""

    __tablename__ = "watches"
    __table_args__ = (
        Index("watch_poll_due_idx", "source", "status", "next_check_at"),
        CheckConstraint("delivery IN ('thread', 'dm', 'new_thread')", name="ck_watches_delivery"),
        # A thread watch is bound to one live session; the others never are.
        CheckConstraint(
            "(delivery = 'thread') = (session_id IS NOT NULL)", name="ck_watches_delivery_session"
        ),
        CheckConstraint(
            "delivery = 'thread' OR source IN ('poll', 'mcp_poll', 'callback')",
            name="ck_watches_delivery_source",
        ),
        # No expiry: an admin-approved polled watch, or a callback watch where the
        # `callback_watch_lifetime` feature is `0` (permanent).
        CheckConstraint(
            "expires_at IS NOT NULL OR source = 'callback' OR "
            "(permanent_approved_by IS NOT NULL AND source IN ('poll', 'mcp_poll'))",
            name="ck_watches_permanent",
        ),
        CheckConstraint("max_turns_per_day BETWEEN 1 AND 240", name="ck_watches_max_turns"),
    )
    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    owner_account_id: Mapped[AccountFK]
    owner_external_id: Mapped[str]
    platform: Mapped[str]
    parent_channel_id: Mapped[str]
    thread_id: Mapped[str]
    agent_id: Mapped[str]
    agent_name: Mapped[str]
    # NULL unless `delivery` is 'thread': the others run each fire on a fresh session.
    session_id: Mapped[str | None]
    # Where a fire reports: 'thread' (the bound thread), 'dm' (the owner's DM) or
    # 'new_thread' (a new thread under the parent). `thread_id` and
    # `parent_channel_id` always name the place the watch was made.
    delivery: Mapped[str] = mapped_column(server_default="thread")
    instructions: Mapped[str]
    producer: Mapped[str] = mapped_column(unique=True)
    page_id: Mapped[uuid.UUID]
    revision: Mapped[int] = mapped_column(server_default="1")
    source: Mapped[str] = mapped_column(server_default="page_event")
    status: Mapped[str] = mapped_column(server_default="active")
    baseline: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    rule: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    source_state: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    # NULL is a permanent watch; it expires only after 30 days paused (`paused_at`).
    expires_at: Mapped[datetime | None]
    # The admin whose fresh admin check allowed a permanent watch, kept for audit.
    permanent_approved_by: Mapped[uuid.UUID | None]
    paused_at: Mapped[datetime | None]
    lease_owner: Mapped[str | None]
    lease_expires_at: Mapped[datetime | None]
    next_check_at: Mapped[datetime | None]
    backoff_until: Mapped[datetime | None]
    consecutive_errors: Mapped[int] = mapped_column(server_default="0")
    max_turns_per_day: Mapped[int] = mapped_column(server_default="24")
    max_fires: Mapped[int | None]
    fire_count: Mapped[int] = mapped_column(server_default="0")
    rate_window_at: Mapped[datetime | None]
    rate_count: Mapped[int] = mapped_column(server_default="0")
    created_at: Mapped[TsNow]


class WatchCapability(Base):
    """Only the hash survives issuance. Secrets are never queryable."""

    __tablename__ = "watch_capabilities"
    id: Mapped[UuidPk]
    watch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"))
    digest: Mapped[str]
    key_id: Mapped[str]
    generation: Mapped[int]
    operation: Mapped[str]
    # None only for the link of a callback watch with no expiry.
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


class WatchFiring(Base):
    """One coalesced batch linked to the existing durable wake."""

    __tablename__ = "watch_firings"
    wake_key: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("task_continuations.idempotency_key", ondelete="CASCADE"), primary_key=True
    )
    watch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("watches.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int]
    generation: Mapped[int]
    events: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    omitted: Mapped[int] = mapped_column(server_default="0")
    created_at: Mapped[TsNow]
    # Written once: the thread a 'new_thread' fire made, so a retry reuses it.
    delivery_thread_id: Mapped[str | None]
    # Written once: the fresh session a 'dm' fire runs on.
    delivery_session_id: Mapped[str | None]


class WatchEvidence(Base):
    """Durably accepted producer event; retries return the original wake."""

    __tablename__ = "watch_inbox"
    __table_args__ = (
        UniqueConstraint("producer", "event_id", name="uq_watch_inbox_event"),
        Index("watch_inbox_accepted_idx", "accepted_at"),
        Index("watch_inbox_watch_idx", "watch_id"),
    )
    id: Mapped[UuidPk]
    watch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"))
    producer: Mapped[str]
    event_id: Mapped[str]
    payload: Mapped[str]
    wake_key: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("watch_firings.wake_key", ondelete="CASCADE")
    )
    accepted_at: Mapped[TsNow]


class WatchControl(Base):
    """Durable global producer gate and owner/tenant stops, outside sidecar authority."""

    __tablename__ = "watch_controls"
    __table_args__ = (
        CheckConstraint(
            "(max_turns_per_watch_per_day IS NULL OR max_turns_per_watch_per_day BETWEEN 1 AND 240)"
            " AND (max_turns_per_owner_per_day IS NULL"
            " OR max_turns_per_owner_per_day BETWEEN 1 AND 1000)"
            " AND (scope = 'global' OR scope LIKE 'tenant:%'"
            " OR (max_turns_per_watch_per_day IS NULL AND max_turns_per_owner_per_day IS NULL))",
            name="ck_watch_controls_ceilings",
        ),
    )
    scope: Mapped[str] = mapped_column(primary_key=True)
    enabled: Mapped[FalseFlag]
    # Daily firing ceilings, on the global row or a tenant row (which overrides it);
    # NULL is the default, 24 per watch and 100 per owner.
    max_turns_per_watch_per_day: Mapped[int | None]
    max_turns_per_owner_per_day: Mapped[int | None]


class WatchNoticeReceipt(Base):
    """One owner-notice send attempt: ids and outcome, never the notice text.

    No foreign key to `watches`, so a receipt outlives its watch; the notice
    poller prunes receipts after `NOTICE_RECEIPT_RETENTION`.
    """

    __tablename__ = "watch_notice_receipts"
    __table_args__ = (
        UniqueConstraint(
            "watch_id", "kind", "generation", "attempt", name="uq_watch_notice_attempt"
        ),
        CheckConstraint(
            "outcome IN ('pending', 'sent', 'failed', 'interrupted', 'refused')",
            name="ck_watch_notice_outcome",
        ),
        Index("ix_watch_notice_receipt_time", "attempted_at"),
    )
    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    watch_id: Mapped[uuid.UUID]
    owner_account_id: Mapped[AccountFK]
    platform: Mapped[str]
    kind: Mapped[str]
    generation: Mapped[int]
    attempt: Mapped[int]
    # The notice claim's lease token, so an attempt maps to exactly one claim.
    claim: Mapped[str]
    outcome: Mapped[str] = mapped_column(server_default="pending")
    attempted_at: Mapped[datetime]
    settled_at: Mapped[datetime | None]
    channel_id: Mapped[str | None]
    message_id: Mapped[str | None]


class WatchCredential(Base):
    """An owner's own read key for one MCP server, used only by that owner's watches.

    Encrypted with the same MultiFernet as `agent_mcp_credentials`. Written
    only by the requester-only private form; the poll executor decrypts the
    one row for the leased watch's owner and URL. No sidecar or ingress grants.
    """

    __tablename__ = "watch_credentials"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "owner_account_id", "server_url", name="uq_watch_credentials_owner_url"
        ),
    )
    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    owner_account_id: Mapped[AccountFK]
    server_url: Mapped[str]
    token_encrypted: Mapped[bytes]
    created_at: Mapped[TsNow]
    rotated_at: Mapped[TsNow]
    last_used_at: Mapped[datetime | None]


class LeadIntakeConfig(Base):
    __tablename__ = "lead_intake_configs"
    tenant_id: Mapped[TenantPk]
    config: Mapped[dict[str, Any]]
    encrypted_credentials: Mapped[bytes]


class LeadIntakeRecord(Base):
    __tablename__ = "lead_intakes"
    __table_args__ = (UniqueConstraint("tenant_id", "source_digest", name="uq_lead_source"),)
    tenant_id: Mapped[TenantPk]
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    source_digest: Mapped[str]
    actor_digest: Mapped[str | None]
    status: Mapped[str]
    encrypted_payload: Mapped[bytes | None]
    encrypted_raw: Mapped[bytes | None]
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    raw_expires_at: Mapped[datetime]
    # Who may read a lead is fixed at capture; kept in plaintext so it outlives erasure.
    destination_guild_id: Mapped[str]
    destination_channel_id: Mapped[str]


class LeadIntakeEffect(Base):
    __tablename__ = "lead_intake_effects"
    __table_args__ = (
        Index(
            "uq_lead_discord_starter",
            "tenant_id",
            "remote_id",
            unique=True,
            postgresql_where=text("step = 'starter' AND remote_id IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["tenant_id", "intake_id"],
            ["lead_intakes.tenant_id", "lead_intakes.id"],
            ondelete="CASCADE",
        ),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    intake_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    step: Mapped[str] = mapped_column(primary_key=True)
    stable_key: Mapped[str]
    state: Mapped[str]
    remote_id: Mapped[str | None]
    created_at: Mapped[datetime]


class LeadResolutionAudit(Base):
    __tablename__ = "lead_resolution_audit"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "intake_id"],
            ["lead_intakes.tenant_id", "lead_intakes.id"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID]
    intake_id: Mapped[uuid.UUID]
    encrypted_resolution: Mapped[bytes]
    created_at: Mapped[datetime]


class LeadMailboxState(Base):
    __tablename__ = "lead_mailbox_states"
    tenant_id: Mapped[TenantPk]
    mailbox_key: Mapped[str] = mapped_column(primary_key=True)
    encrypted_state: Mapped[bytes]


class LeadIntakeExecution(Base):
    __tablename__ = "lead_intake_executions"
    tenant_id: Mapped[TenantPk]
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    routine_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("routines.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime]


class LeadOpportunityRecord(Base):
    __tablename__ = "lead_opportunities"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "intake_id"],
            ["lead_intakes.tenant_id", "lead_intakes.id"],
            ondelete="CASCADE",
        ),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    identity_digest: Mapped[str] = mapped_column(primary_key=True)
    intake_id: Mapped[uuid.UUID]
    encrypted_mapping: Mapped[bytes]
    expires_at: Mapped[datetime]


class LeadMailboxScanRecord(Base):
    __tablename__ = "lead_mailbox_scans"
    tenant_id: Mapped[TenantPk]
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    mailbox_key: Mapped[str]
    window_start: Mapped[datetime]
    window_end: Mapped[datetime]
    state: Mapped[str]
    discovered_count: Mapped[int]
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    error_code: Mapped[str | None]


class LeadMailboxMessageRecord(Base):
    __tablename__ = "lead_mailbox_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "intake_id"],
            ["lead_intakes.tenant_id", "lead_intakes.id"],
            ondelete="CASCADE",
        ),
    )
    tenant_id: Mapped[TenantPk]
    source_digest: Mapped[str] = mapped_column(primary_key=True)
    mailbox_key: Mapped[str]
    encrypted_message_id: Mapped[bytes | None]
    disposition: Mapped[str]
    intake_id: Mapped[uuid.UUID | None]
    expires_at: Mapped[datetime]


class SelfHostedAgentRecord(Base):
    """Tenant-owned resource; spec contains model, system, tools, skills and metadata."""

    __tablename__ = "selfhosted_agents"
    __table_args__ = (
        Index(
            "uq_selfhosted_agents_active_name",
            "tenant_id",
            "name",
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    name: Mapped[str]
    spec: Mapped[dict[str, Any]]
    skill_attachments: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    revision: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNow]
    archived_at: Mapped[datetime | None]


class SelfHostedAgentVersion(Base):
    """Every revision of a Daimon-owned agent, so a session can read the version it began on."""

    __tablename__ = "selfhosted_agent_versions"

    agent_id: Mapped[str] = mapped_column(
        ForeignKey("selfhosted_agents.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    name: Mapped[str]
    spec: Mapped[dict[str, Any]]
    archived: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[TsNow]


class SelfHostedEnvironmentRecord(Base):
    """Harness routing and credential references live in the environment spec."""

    __tablename__ = "selfhosted_environments"
    __table_args__ = (
        Index(
            "uq_selfhosted_environments_active_name",
            "tenant_id",
            "name",
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    name: Mapped[str]
    spec: Mapped[dict[str, Any]]
    revision: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNow]
    archived_at: Mapped[datetime | None]


class SelfHostedResourceOperation(Base):
    """Reference-only receipts, principal-owned and retained for seven days."""

    __tablename__ = "selfhosted_resource_operations"
    __table_args__ = (Index("ix_selfhosted_resource_operations_expires_at", "expires_at"),)

    tenant_id: Mapped[TenantPk]
    account_id: Mapped[str] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    principal_id: Mapped[str]
    operation_id: Mapped[str]
    action: Mapped[str]
    digest: Mapped[str]
    result: Mapped[dict[str, Any]]
    created_at: Mapped[TsNow]
    expires_at: Mapped[datetime] = mapped_column(server_default=text("now() + interval '7 days'"))


class LongJobPolicyRecord(Base):
    __tablename__ = "long_job_policy"
    __table_args__ = (
        Index(
            "uq_long_job_policy_scope",
            "tenant_id",
            "agent_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            (
                "default_deadline_minutes > 0 AND max_deadline_minutes >= "
                "default_deadline_minutes AND max_usd > 0"
            ),
            name="ck_long_job_policy_limits",
        ),
    )
    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    agent_id: Mapped[str | None]
    enabled: Mapped[bool]
    default_deadline_minutes: Mapped[int]
    max_deadline_minutes: Mapped[int]
    max_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    set_by: Mapped[str]
    set_at: Mapped[datetime]


class LongJobPolicyHistory(Base):
    __tablename__ = "long_job_policy_history"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[TenantFK]
    agent_id: Mapped[str | None]
    before: Mapped[dict[str, Any] | None]
    after: Mapped[dict[str, Any]]
    set_by: Mapped[str]
    set_at: Mapped[datetime]


class LongJobRecord(Base):
    __tablename__ = "long_job"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "parent_session_id", "tool_use_id", name="uq_long_job_registration"
        ),
        CheckConstraint(
            (
                "state IN "
                "('queued','launching','running','succeeded','failed','timed_out','cancelled','lost','over_budget')"
            ),
            name="ck_long_job_state",
        ),
        CheckConstraint("deadline_minutes > 0 AND max_usd > 0", name="ck_long_job_limits"),
        Index("ix_long_job_claim", "tenant_id", "state", "lease_expires_at"),
    )
    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    owner_account_id: Mapped[AccountFK]
    owner_external_user_id: Mapped[str]
    agent_id: Mapped[str]
    agent_name: Mapped[str]
    agent_version: Mapped[int]
    environment_id: Mapped[str]
    environment_updated_at: Mapped[datetime]
    platform: Mapped[str]
    parent_channel_id: Mapped[str]
    thread_id: Mapped[str]
    parent_session_id: Mapped[str]
    source_metadata: Mapped[dict[str, str]] = mapped_column(JSONB, server_default="{}")
    initial_send_at: Mapped[datetime | None]
    tool_use_id: Mapped[str]
    command: Mapped[str]
    title: Mapped[str]
    deadline_minutes: Mapped[int]
    max_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    state: Mapped[str] = mapped_column(server_default="queued")
    version: Mapped[int] = mapped_column(server_default="0")
    worker_session_id: Mapped[str | None]
    lease_owner: Mapped[str | None]
    lease_expires_at: Mapped[datetime | None]
    deadline_at: Mapped[datetime | None]
    stop_state: Mapped[str | None]
    cancel_requested: Mapped[FalseFlag]
    interrupt_attempted_at: Mapped[datetime | None]
    interrupted_at: Mapped[datetime | None]
    archived_at: Mapped[datetime | None]
    summary: Mapped[str | None]
    exit_code: Mapped[int | None]
    output_files: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default="0")
    created_at: Mapped[TsNow]
    finished_at: Mapped[datetime | None]


class LongJobCancelAuditRecord(Base):
    __tablename__ = "long_job_cancel_audit"

    audit_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[TenantFK]
    id: Mapped[uuid.UUID] = mapped_column(ForeignKey("long_job.id", ondelete="CASCADE"))
    state: Mapped[str]
    owner_account_id: Mapped[uuid.UUID]
    requested_by: Mapped[uuid.UUID]
    requested_at: Mapped[TsNow]


class ContextProviderBinding(Base):
    """Admin-owned pre-turn provider; empty selectors are the tenant default."""

    __tablename__ = "context_provider_bindings"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "channel_id", "agent_id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    tenant_id: Mapped[uuid.UUID]
    channel_id: Mapped[str] = mapped_column(server_default=text("''"))
    agent_id: Mapped[str] = mapped_column(server_default=text("''"))
    config: Mapped[dict[str, Any]]
    updated_by: Mapped[str]
    updated_at: Mapped[TsNow]


class ComputeSessionRecord(Base):
    __tablename__ = "compute_sessions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_key", name="uq_compute_request"),
        UniqueConstraint("app_name", name="uq_compute_app"),
        CheckConstraint(
            "state IN ('launching','running','stopping','deleted')", name="ck_compute_state"
        ),
        CheckConstraint(
            "ttl_hours BETWEEN 1 AND 168 AND cpus > 0 AND memory_mb > 0", name="ck_compute_limits"
        ),
        Index("ix_compute_owner", "tenant_id", "owner_account_id", "state"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    owner_account_id: Mapped[AccountFK]
    owner_external_user_id: Mapped[str]
    deployment_id: Mapped[str]
    platform: Mapped[str]
    parent_channel_id: Mapped[str]
    thread_id: Mapped[str]
    agent_id: Mapped[str]
    agent_name: Mapped[str]
    source_metadata: Mapped[dict[str, str]] = mapped_column(JSONB, server_default="{}")
    extension_keys: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    request_key: Mapped[str]
    app_name: Mapped[str]
    profile: Mapped[str]
    image: Mapped[str]
    region: Mapped[str]
    # The removed Fly provider's CPU class. Nothing reads it. It is still written, as
    # `shared`, so a release that reads it and a schema without the default both work;
    # a later release drops the column.
    cpu_kind: Mapped[str] = mapped_column(default="shared", server_default="shared")
    cpus: Mapped[int]
    memory_mb: Mapped[int]
    state: Mapped[str] = mapped_column(server_default="launching")
    machine_id: Mapped[str | None]
    ttl_hours: Mapped[int]
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime]
    warned_for: Mapped[datetime | None]


class RepoMemory(Base):
    """One tenant/repository pool, shared across agents; personal directives stay owned."""

    __tablename__ = "repo_memory"
    __table_args__ = (
        CheckConstraint("kind IN ('reference', 'directive')", name="ck_repo_memory_kind"),
        CheckConstraint("scope IN ('repo', 'user')", name="ck_repo_memory_scope"),
        CheckConstraint("scope = 'repo' OR kind = 'directive'", name="ck_repo_memory_personal"),
        CheckConstraint("length(name) BETWEEN 1 AND 64", name="ck_repo_memory_name"),
        Index("ix_repo_memory_turn", "tenant_id", "repo_id", "kind", "name"),
    )
    tenant_id: Mapped[TenantPk]
    repo_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(primary_key=True)
    title: Mapped[str]
    body: Mapped[str]
    kind: Mapped[str]
    scope: Mapped[str]
    # The erasure path deletes personal directives first; shared knowledge
    # survives with its attribution removed.
    created_by_account_id: Mapped[AccountSetNull]
    source_id: Mapped[str | None]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNow]


class RepoMemorySession(Base):
    """Last delivered repository-memory state for any provider's session."""

    __tablename__ = "repo_memory_sessions"

    tenant_id: Mapped[TenantPk]
    provider: Mapped[str] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(primary_key=True)
    sha256: Mapped[str]


class ComputeTaskRecord(Base):
    __tablename__ = "compute_tasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_key", name="uq_compute_task_request"),
        CheckConstraint(
            "state IN ('starting','running','succeeded','failed','timed_out','lost','cancelled')",
            name="ck_compute_task_state",
        ),
        CheckConstraint("timeout_minutes > 0 AND output_bytes >= 0", name="ck_compute_task_limits"),
        Index("ix_compute_task_session", "tenant_id", "session_id", "state"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("compute_sessions.id", ondelete="CASCADE")
    )
    owner_account_id: Mapped[AccountFK]
    request_key: Mapped[str]
    command_preview: Mapped[str]
    state: Mapped[str] = mapped_column(server_default="starting")
    exit_code: Mapped[int | None]
    timeout_minutes: Mapped[int]
    deadline_at: Mapped[datetime]
    output_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    output_tail: Mapped[str] = mapped_column(server_default="")
    output_path: Mapped[str]
    created_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    version: Mapped[int] = mapped_column(server_default="0")
    # The scheduler does not poll the task before this: its caller still waits for it.
    wait_until: Mapped[datetime | None]


class AgentDaimonToolAllowlist(Base):
    """The daimon tools an agent enables by name, when it lists them explicitly.

    Written each time a session for the agent is created or has its tools
    replaced; no row means the agent's daimon toolset enables every tool. The
    MCP server reads it to list those tools directly for the agent's chat
    credential, since tools reached only through `call_tool` are never enabled
    by an explicit list.
    """

    __tablename__ = "agent_daimon_tool_allowlists"

    tenant_id: Mapped[TenantPk]
    # derive_agent_uuid(tenant, MA agent id): the chat credential's chat_agent_id.
    agent_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tool_names: Mapped[list[str]]


class WebPerson(Base):
    """One person in one workspace, as the web app knows them.

    Tenant-scoped: someone who belongs to two workspaces has two rows, so a purge
    stays per tenant. `account_id` is the account their turns, memory and spend belong
    to. Erasing the account cascades here, and from here to identities, sessions and
    tokens.
    """

    __tablename__ = "web_people"
    __table_args__ = (
        UniqueConstraint("tenant_id", "account_id", name="uq_web_people_tenant_account"),
        CheckConstraint("kind IN ('member', 'visitor')", name="ck_web_people_kind"),
        CheckConstraint("status IN ('active', 'suspended')", name="ck_web_people_status"),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    account_id: Mapped[AccountFK]
    kind: Mapped[str]
    display_name: Mapped[str]
    workspace_name: Mapped[str] = mapped_column(server_default="")
    email: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default="active")
    invite_id: Mapped[uuid.UUID | None]
    invited_by_account_id: Mapped[AccountSetNull]
    last_seen_at: Mapped[TsNow]
    created_at: Mapped[TsNow]


class WebIdentity(Base):
    """A way a person signs in: an OIDC subject, a Discord id, a Slack or Microsoft id.

    `issuer` is the OIDC `iss` and is empty for the other providers. Looked up across
    tenants at sign-in, so one login reaches every workspace it belongs to.
    """

    __tablename__ = "web_identities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "issuer", "subject", name="uq_web_identities_subject"
        ),
        Index("ix_web_identities_lookup", "provider", "issuer", "subject"),
        Index("ix_web_identities_person", "person_id"),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("web_people.id", ondelete="CASCADE"))
    provider: Mapped[str]
    issuer: Mapped[str] = mapped_column(server_default="")
    subject: Mapped[str]
    label: Mapped[str] = mapped_column(server_default="")
    groups: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    linked_at: Mapped[TsNow]


class WebLogin(Base):
    """The accounts one member connected, across platforms (`WebLoginAccount`)."""

    __tablename__ = "web_logins"

    id: Mapped[UuidPk]
    created_at: Mapped[TsNow]


class WebLoginAccount(Base):
    """A sign-in account in one login; each (provider, subject) belongs to one login.

    `last_proven_at` is when the provider last vouched for it; the account's workspaces
    join a session only while it is recent.
    """

    __tablename__ = "web_login_accounts"
    __table_args__ = (Index("ix_web_login_accounts_login", "login_id"),)

    login_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("web_logins.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(primary_key=True)
    subject: Mapped[str] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(server_default="")
    last_proven_at: Mapped[TsNow]


class WebSession(Base):
    """A browser sign-in. Only the SHA-256 of the cookie token is stored.

    Spans the person rows one sign-in reached (`WebSessionPerson`); the current one
    decides the workspace. Deleted with the current person.
    """

    __tablename__ = "web_sessions"
    __table_args__ = (
        Index("ix_web_sessions_expires_at", "expires_at"),
        Index("ix_web_sessions_login", "login_id"),
        # The account a sign-in came with: disconnecting it ends every session of it.
        Index("ix_web_sessions_account", "provider", "subject"),
    )

    id: Mapped[UuidPk]
    token_sha256: Mapped[bytes] = mapped_column(unique=True)
    provider: Mapped[str]
    issuer: Mapped[str] = mapped_column(server_default="")
    subject: Mapped[str]
    current_person_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("web_people.id", ondelete="CASCADE")
    )
    tenant_id: Mapped[TenantFK]
    created_at: Mapped[TsNow]
    last_seen_at: Mapped[TsNow]
    expires_at: Mapped[datetime]
    roles_checked_at: Mapped[TsNow]
    login_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("web_logins.id", ondelete="SET NULL")
    )


class WebSessionPerson(Base):
    """A person row a browser session may switch to."""

    __tablename__ = "web_session_people"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("web_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[TenantFK]
    person_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("web_people.id", ondelete="CASCADE"), primary_key=True
    )


class WebNodeToken(Base):
    """A person's access token at a connection node's server, Fernet-encrypted.

    Got by token exchange and exchanged again once spent; `token_key` is
    `<issuer>#<server>`.
    """

    __tablename__ = "web_node_tokens"

    person_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("web_people.id", ondelete="CASCADE"), primary_key=True
    )
    token_key: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    access_token_enc: Mapped[bytes]
    expires_at: Mapped[datetime]


class ServerSkill(Base):
    """A library skill mirrored from a skills server, one per (tenant, server, upstream skill).

    The sync owns its library skill. `digest` names the accepted files; an update that
    needs approval waits in the `pending_*` columns. `added_by_account_id` is whom the
    server is asked as; once that account is gone the mirror is unavailable.
    """

    __tablename__ = "server_skills"
    __table_args__ = (
        CheckConstraint(
            "status IN ('current', 'pending', 'unavailable')", name="ck_server_skills_status"
        ),
        UniqueConstraint("tenant_id", "server", "uri", name="uq_server_skills_upstream"),
        UniqueConstraint("tenant_id", "skill_id", name="uq_server_skills_skill"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    server: Mapped[str]
    uri: Mapped[str]
    name: Mapped[str]
    skill_id: Mapped[str]
    version_label: Mapped[str | None]
    digest: Mapped[str]
    manifest: Mapped[dict[str, Any]]
    allowed_tools: Mapped[Any] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(server_default="current")
    pending_digest: Mapped[str | None]
    pending_label: Mapped[str | None]
    pending_changes: Mapped[dict[str, Any] | None]
    added_by_account_id: Mapped[AccountSetNull]
    last_checked_at: Mapped[datetime | None]
    last_error: Mapped[str | None]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNow]


class ServerSkillDeployment(Base):
    """The concrete version one provider holds of a mirrored skill's files (`digest`)."""

    __tablename__ = "server_skill_deployments"
    __table_args__ = (
        CheckConstraint("version <> 'latest'", name="ck_server_skill_deployments_concrete"),
    )

    mirror_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("server_skills.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    digest: Mapped[str]
    version: Mapped[str]
    updated_at: Mapped[TsNow]


class AgentSkillFollow(Base):
    """Whether an agent follows a mirrored skill's updates; no row means it does."""

    __tablename__ = "agent_skill_follows"

    tenant_id: Mapped[TenantPk]
    agent_id: Mapped[str] = mapped_column(primary_key=True)
    skill_id: Mapped[str] = mapped_column(primary_key=True)
    follow: Mapped[bool]
    # The update the agent's backend consent held back: it waits for the owner's re-confirm.
    held_digest: Mapped[str | None]
    held_reason: Mapped[str | None]
    updated_at: Mapped[TsNow]


class WorkspaceMember(Base):
    """Who is a member of a chat workspace Daimon is installed in, as its platform says.

    A mirror the chat adapters keep from the platform (a full reload, then join, leave
    and role events), so the web app can tell who still belongs without asking the
    platform per request. Not erased with an account: it holds only what the platform
    shows every member, and the next sync would write it back.
    """

    __tablename__ = "workspace_members"

    tenant_id: Mapped[TenantPk]
    platform_user_id: Mapped[str] = mapped_column(primary_key=True)
    is_admin: Mapped[FalseFlag]
    updated_at: Mapped[TsNow]
    # The member's role ids, where the platform has roles (Discord). None: not recorded
    # (a Slack member, or a row written before roles were kept), so it says nothing.
    role_ids: Mapped[list[str] | None]


class WorkspaceMemberSync(Base):
    """A workspace whose member list was loaded in full at least once.

    Without a row the list is incomplete, so sign-in falls back to what the
    platform's own sign-in proves.
    """

    __tablename__ = "workspace_member_syncs"

    tenant_id: Mapped[TenantPk]
    workspace_name: Mapped[str] = mapped_column(server_default="")
    synced_at: Mapped[datetime]


class DiscordAudienceState(Base):
    """Whether a Discord server's room facts are complete, and until when they stay fresh.

    The bot writes it; the scheduler and MCP processes read it. A missing row, an
    incomplete one or one past `fresh_until` makes every room of the server unknown.
    """

    __tablename__ = "discord_audience_state"

    tenant_id: Mapped[TenantPk]
    complete: Mapped[bool] = mapped_column(server_default=text("false"))
    generation: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    fresh_until: Mapped[datetime | None]
    updated_at: Mapped[TsNow]


class DiscordChannelAudience(Base):
    """One room's facts from the generation that `DiscordAudienceState` names."""

    __tablename__ = "discord_channel_audience"

    tenant_id: Mapped[TenantPk]
    channel_id: Mapped[str] = mapped_column(primary_key=True)
    parent_id: Mapped[str | None]
    contained: Mapped[bool]
    narrow: Mapped[bool]
    generation: Mapped[int] = mapped_column(BigInteger)
    computed_at: Mapped[datetime]


class WorkspaceAdmin(Base):
    """Who administers a workspace in the web app, beyond platform admins."""

    __tablename__ = "workspace_admins"
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'admin')", name="ck_workspace_admins_role"),
    )

    tenant_id: Mapped[TenantPk]
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str]
    granted_by_account_id: Mapped[AccountSetNull]
    granted_at: Mapped[TsNow]


class WebAudit(Base):
    """What admins did in the web app. Keeps its rows when an actor's account is erased.

    A person or invite a row is about is an id, shown by its current name or email, so
    erasing them leaves `target` as a generic label and no name or address behind.
    """

    __tablename__ = "web_audit"
    __table_args__ = (Index("ix_web_audit_tenant_at", "tenant_id", "at"),)

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    at: Mapped[TsNow]
    actor_account_id: Mapped[AccountSetNull]
    action: Mapped[str]
    target: Mapped[str]
    target_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("web_people.id", ondelete="SET NULL")
    )
    target_invite_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("web_invites.id", ondelete="SET NULL")
    )
    detail: Mapped[str | None]


class WebInvite(Base):
    """A link into a workspace: an invite, a person's one-time sign-in, or the owner's.

    Only the SHA-256 of the token is stored. An accepted invite goes with the person
    who accepted it, so erasing them also erases the email it was sent to.
    """

    __tablename__ = "web_invites"
    __table_args__ = (
        Index("ix_web_invites_tenant_created", "tenant_id", "created_at"),
        CheckConstraint("kind IN ('invite', 'sign_in', 'owner')", name="ck_web_invites_kind"),
        CheckConstraint("role IN ('visitor', 'member')", name="ck_web_invites_role"),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    token_sha256: Mapped[bytes] = mapped_column(unique=True)
    kind: Mapped[str]
    email: Mapped[str] = mapped_column(server_default="")
    role: Mapped[str] = mapped_column(server_default="visitor")
    credit_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), server_default="0")
    # Whether a visitor this invite brings in may attach files in web chat.
    allow_files: Mapped[FalseFlag]
    # The person a sign-in link signs in.
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("web_people.id", ondelete="CASCADE")
    )
    created_by_account_id: Mapped[AccountSetNull]
    created_at: Mapped[TsNow]
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None]
    accepted_by_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("web_people.id", ondelete="CASCADE")
    )
    revoked_at: Mapped[datetime | None]


class WebInviteAgent(Base):
    """An agent an invite opens to the person who accepts it."""

    __tablename__ = "web_invite_agents"

    invite_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("web_invites.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[TenantFK]
    agent_id: Mapped[str] = mapped_column(primary_key=True)


class WebAgentGrant(Base):
    """Whether an agent chats on the web, and with whom. No row: on, for everyone."""

    __tablename__ = "web_agent_grants"
    __table_args__ = (
        CheckConstraint("who IN ('everyone', 'groups', 'invites')", name="ck_web_agent_grants_who"),
    )

    tenant_id: Mapped[TenantPk]
    agent_id: Mapped[str] = mapped_column(primary_key=True)
    web: Mapped[bool] = mapped_column(server_default=text("true"))
    who: Mapped[str] = mapped_column(server_default="everyone")
    groups: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    updated_at: Mapped[TsNow]


class WebWorkspaceSettings(Base):
    """What a workspace's admins switched off in the web app. No row: all on."""

    __tablename__ = "web_workspace_settings"

    tenant_id: Mapped[TenantPk]
    modules_off: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    default_agent: Mapped[str | None]
    updated_at: Mapped[TsNow]


class WebThread(Base):
    """One web chat: a person's conversation with the agent it was started with."""

    __tablename__ = "web_threads"
    __table_args__ = (Index("ix_web_threads_person_updated", "person_id", "updated_at"),)

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("web_people.id", ondelete="CASCADE"))
    agent_id: Mapped[str]
    agent_name: Mapped[str]
    title: Mapped[str]
    created_at: Mapped[TsNow]
    updated_at: Mapped[TsNow]
    deleted_at: Mapped[datetime | None]


class WebMessage(Base):
    """One message of a web chat. A user message keeps the id its browser chose."""

    __tablename__ = "web_messages"
    __table_args__ = (
        Index("ix_web_messages_thread_seq", "thread_id", "seq"),
        # Finds the messages carrying a file (`blocks @> ...`) when an upload is swept.
        Index(
            "ix_web_messages_blocks",
            "blocks",
            postgresql_using="gin",
            postgresql_ops={"blocks": "jsonb_path_ops"},
        ),
        CheckConstraint("role IN ('user', 'assistant')", name="ck_web_messages_role"),
        CheckConstraint(
            "status IN ('running', 'done', 'stopped', 'interrupted')", name="ck_web_messages_status"
        ),
        CheckConstraint("feedback IN ('up', 'down')", name="ck_web_messages_feedback"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[TenantFK]
    thread_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("web_threads.id", ondelete="CASCADE"))
    role: Mapped[str]
    content: Mapped[str] = mapped_column(server_default="")
    blocks: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    turn_id: Mapped[uuid.UUID | None]
    status: Mapped[str] = mapped_column(server_default="done")
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    duration_ms: Mapped[int | None]
    feedback: Mapped[str | None]
    seq: Mapped[int] = mapped_column(BigInteger, Identity())
    created_at: Mapped[TsNow]


class AgentDraft(Base):
    """One person's unsaved web edit of an agent: the whole config, and the
    version it began from. Deleting the account deletes the draft."""

    __tablename__ = "agent_drafts"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id", "account_id", name="pk_agent_drafts"),
        Index("ix_agent_drafts_account", "account_id"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE", name="fk_agent_drafts_tenants")
    )
    agent_id: Mapped[str]
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE", name="fk_agent_drafts_accounts")
    )
    base_version: Mapped[int]
    # The agent as the draft began, kept so an apply can tell that places or tool rules
    # (which live outside the provider's agent, and so outside its version) moved.
    base_config: Mapped[dict[str, Any]]
    config: Mapped[dict[str, Any]]
    updated_at: Mapped[TsNow]


class AgentVersion(Base):
    """An agent's config at one provider version, as the web app recorded it.

    Written when a draft begins (the base, no author) and by each apply or revert
    (author, note and the changes from the version before). An apply that touches only
    places or tool rules leaves the provider's version where it was and adds its changes
    to the row already there. The author is SET NULL
    when their account is erased; the history belongs to the tenant's agent.
    """

    __tablename__ = "agent_versions"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id", "version", name="pk_agent_versions"),
        Index("ix_agent_versions_account", "account_id"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE", name="fk_agent_versions_tenants")
    )
    agent_id: Mapped[str]
    version: Mapped[int]
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL", name="fk_agent_versions_accounts")
    )
    note: Mapped[str | None]
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    config: Mapped[dict[str, Any]]
    at: Mapped[TsNow]


class PendingCarryOver(Base):
    """A replacement session's framing, kept until its first message is sent (`turn.run`)."""

    __tablename__ = "pending_carry_overs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "session_id", name="uq_pending_carry_overs_session"),
        Index("ix_pending_carry_overs_created_at", "created_at"),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE", name="fk_pending_carry_overs_tenants")
    )
    # The provider's id for the session that was never sent its framing.
    session_id: Mapped[str]
    # The framing quotes the person's earlier conversation: it goes with them.
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE", name="fk_pending_carry_overs_accounts")
    )
    user_prefix: Mapped[str]
    system_blocks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[TsNow]


class MonitoredEvent(Base):
    """A question, answer, MCP tool call or shown notice, kept while the agent is monitored.

    `agent_id` is the agent's local id (`ma_identity.derive_agent_uuid`). `conversation`
    is the DM, web chat or MCP session, and `turn_ref` pairs an answer with its question.
    A tool row has its name and outcome only, never what the call sent or returned. A
    notice row says when the person was told they are monitored.
    """

    __tablename__ = "monitored_events"
    __table_args__ = (
        Index("ix_monitored_events_agent_created", "tenant_id", "agent_id", "created_at"),
        Index("ix_monitored_events_created", "created_at"),
        Index("ix_monitored_events_account", "account_id"),
        Index(
            "ix_monitored_events_notice",
            "tenant_id",
            "agent_id",
            "created_at",
            postgresql_where=text("kind = 'notice'"),
        ),
        # One row of each kind per turn, so a retried capture is a no-op.
        Index(
            "uq_monitored_events_turn",
            "tenant_id",
            "conversation",
            "turn_ref",
            "kind",
            unique=True,
            postgresql_where=text("turn_ref IS NOT NULL"),
        ),
        CheckConstraint(
            "platform IN ('discord', 'slack', 'teams', 'web', 'mcp')",
            name="ck_monitored_events_platform",
        ),
        CheckConstraint(
            "kind IN ('question', 'answer', 'tool', 'notice')", name="ck_monitored_events_kind"
        ),
        CheckConstraint(
            "(kind = 'tool') = (tool_name IS NOT NULL)", name="ck_monitored_events_tool_name"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[TenantFK]
    agent_id: Mapped[uuid.UUID]
    platform: Mapped[str]
    account_id: Mapped[AccountFK]
    conversation: Mapped[str | None]
    turn_ref: Mapped[str | None]
    kind: Mapped[str]
    content: Mapped[str | None]
    tool_name: Mapped[str | None]
    outcome: Mapped[str | None]
    created_at: Mapped[TsNow]


class OpsEvent(Base):
    """One failure or warning an operator watches, coalesced: `count` occurrences in one row.

    Content-free: `reason` is a fixed vocabulary word, never exception text, and only
    `note` (a routine agent's own one-line blocker, at most 300 characters) is free text.
    Issues are these rows grouped by `fingerprint` at read time. A person's rows go with
    their account; every row goes with its tenant and after the retention window.
    """

    __tablename__ = "ops_events"
    __table_args__ = (
        Index("ix_ops_events_tenant_at", "tenant_id", text("occurred_at DESC")),
        Index("ix_ops_events_fingerprint", "tenant_id", "fingerprint", text("occurred_at DESC")),
        Index("ix_ops_events_occurred", "occurred_at"),
        Index(
            "ix_ops_events_account", "account_id", postgresql_where=text("account_id IS NOT NULL")
        ),
        CheckConstraint("severity IN ('error', 'warning')", name="ck_ops_events_severity"),
        CheckConstraint(
            "area IN ('turn', 'dm', 'routine', 'delivery', 'watch', 'wake', 'bridge')",
            name="ck_ops_events_area",
        ),
        CheckConstraint("count >= 1", name="ck_ops_events_count"),
        CheckConstraint("char_length(note) <= 300", name="ck_ops_events_note"),
    )

    id: Mapped[UuidPk]
    tenant_id: Mapped[TenantFK]
    occurred_at: Mapped[datetime]
    count: Mapped[int] = mapped_column(server_default="1")
    code: Mapped[str]
    area: Mapped[str]
    severity: Mapped[str]
    reason: Mapped[str | None]
    fingerprint: Mapped[str]
    # The agent is the MA agent id (text), as on routines and watches.
    agent_id: Mapped[str | None]
    routine_id: Mapped[uuid.UUID | None]
    watch_id: Mapped[uuid.UUID | None]
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE")
    )
    platform: Mapped[str | None]
    channel_id: Mapped[str | None]
    thread_id: Mapped[str | None]
    session_id: Mapped[str | None]
    # The bridge server, connector key or wake kind the event is about.
    subject_key: Mapped[str | None]
    release: Mapped[str | None]
    note: Mapped[str | None]


class OpsIssueState(Base):
    """An admin's resolve or mute of one issue (a fingerprint); reopening deletes the row.

    Erasing the admin's account clears `by_account_id` and keeps the state.
    """

    __tablename__ = "ops_issue_states"
    __table_args__ = (
        CheckConstraint("state IN ('resolved', 'muted')", name="ck_ops_issue_states_state"),
    )

    tenant_id: Mapped[TenantPk]
    fingerprint: Mapped[str] = mapped_column(primary_key=True)
    state: Mapped[str]
    by_account_id: Mapped[AccountSetNull]
    at: Mapped[datetime]


def _install_erased_account_guards() -> None:
    """Create the guards (`_erasure_guards`) wherever the schema is built from the models.

    Production gets the same statements from migrations 0142 and 0206; this keeps a schema built
    by `create_all` (the test schemas) behaving the same.
    """
    for table_name in _erasure_guards.GUARDED_TABLES:
        table = Base.metadata.tables[table_name]
        for statement in (
            *_erasure_guards.ALL_FUNCTIONS,
            _erasure_guards.create_trigger(table_name),
        ):
            event.listen(table, "after_create", DDL(statement))


_install_erased_account_guards()
