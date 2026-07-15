"""initial_schema (squash of the pre-publication migration history)

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-07-13

Squash of the original alembic chain (0001 through 0032, the chain head at
authoring time). DDL-only: no data backfills, no INSERT/UPDATE/DELETE of
any kind. Excludes `agents`/`environments`/`skills` (created 0001, dropped
0003) and `workspaces`/`workspace_config`/`tenant_system_config` (dropped
0022/0023) — none of the six survive to head.

This is the sole revision under packages/core/alembic/versions/; fresh
databases bootstrap directly from it, and databases carrying the pre-squash
chain move onto it via `alembic stamp 0001_initial_schema` (no data
migration — the squash is schema-identical to the chain it replaces).

Table creation order below is dependency-ordered (FK targets before
referencing tables), not alphabetical: tenants and accounts come first
since nearly everything else references one or both.

tenant_user_caps (D-06): created directly under its final name — the table
was `guild_user_caps` in the chain until a 0020 rename. The chain's
`_not_null` column-constraint fossil names (guild_user_caps_id_not_null
etc., a PG18 artifact of the historical ALTER path) are NOT reproduced —
verify_squash.sh normalizes those away. The pkey/fkey constraint names ARE
reproduced explicitly below (they survived the rename unchanged in the
chain) so the artifact matches byte-for-byte after normalization.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # tenants — root of every FK chain
    # ------------------------------------------------------------------
    op.create_table(
        "tenants",
        sa.Column(
            "id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("provision_status", sa.Text(), nullable=False, server_default=sa.text("'ready'")),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("platform", "external_id", name="uq_tenants_platform_external_id"),
    )

    # ------------------------------------------------------------------
    # accounts
    # ------------------------------------------------------------------
    op.create_table(
        "accounts",
        sa.Column(
            "id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("role", sa.Text(), nullable=False, server_default=sa.text("'user'")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_accounts_tenant_id", "accounts", ["tenant_id"])

    # ------------------------------------------------------------------
    # agent_files
    # ------------------------------------------------------------------
    op.create_table(
        "agent_files",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id", "key", name="pk_agent_files"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_agent_files_tenant_id", ondelete="CASCADE"
        ),
    )

    # ------------------------------------------------------------------
    # agent_github_binding
    # ------------------------------------------------------------------
    op.create_table(
        "agent_github_binding",
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("principal_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_index(
        "ix_agent_github_binding_principal_id", "agent_github_binding", ["principal_id"]
    )

    # ------------------------------------------------------------------
    # agent_google_binding
    # ------------------------------------------------------------------
    op.create_table(
        "agent_google_binding",
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("agent_id"),
    )

    # ------------------------------------------------------------------
    # agent_repo_binding
    # ------------------------------------------------------------------
    op.create_table(
        "agent_repo_binding",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("repo_url", sa.Text(), nullable=False),
        sa.Column("default_branch", sa.Text(), nullable=False),
        sa.Column("ma_secret_ref", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id", name="pk_agent_repo_binding"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_agent_repo_binding_tenant_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index("agent_repo_binding_repo_url_idx", "agent_repo_binding", ["repo_url"])

    # ------------------------------------------------------------------
    # channel_config
    # ------------------------------------------------------------------
    op.create_table(
        "channel_config",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=True),
        sa.Column("environment_name", sa.Text(), nullable=True),
        sa.Column("agent_name_set_by_account_id", UUID(as_uuid=True), nullable=True),
        sa.Column("agent_name_set_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False, server_default=sa.text("'agent'")),
        sa.PrimaryKeyConstraint("tenant_id", "channel_id", name="pk_channel_config"),
        sa.ForeignKeyConstraint(
            ["agent_name_set_by_account_id"],
            ["accounts.id"],
            name="fk_channel_config_agent_name_set_by_account_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_channel_config_tenants", ondelete="CASCADE"
        ),
        sa.CheckConstraint("mode IN ('agent', 'user_active')", name="ck_channel_config_mode"),
    )

    # ------------------------------------------------------------------
    # cli_principals
    # ------------------------------------------------------------------
    op.create_table(
        "cli_principals",
        sa.Column(
            "id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("os_user", sa.Text(), nullable=False),
        sa.Column("account_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "os_user", name="uq_cli_principals_tenant_os_user"),
    )
    op.create_index("ix_cli_principals_tenant_id", "cli_principals", ["tenant_id"])

    # ------------------------------------------------------------------
    # github_app_installations (+ owned bigserial sequence)
    # ------------------------------------------------------------------
    op.create_table(
        "github_app_installations",
        sa.Column("installation_id", sa.BigInteger(), primary_key=True),
        sa.Column("account_login", sa.Text(), nullable=False),
        sa.Column(
            "repo_full_names",
            ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ------------------------------------------------------------------
    # github_credentials
    # ------------------------------------------------------------------
    op.create_table(
        "github_credentials",
        sa.Column("principal_id", UUID(as_uuid=True), nullable=False),
        sa.Column("github_login", sa.Text(), nullable=False),
        sa.Column("encrypted_token", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("principal_id"),
    )

    # ------------------------------------------------------------------
    # github_oauth_states
    # ------------------------------------------------------------------
    op.create_table(
        "github_oauth_states",
        sa.Column(
            "state", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("platform_user_id", sa.Text(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("state"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )

    # ------------------------------------------------------------------
    # mcp_tokens
    # ------------------------------------------------------------------
    op.create_table(
        "mcp_tokens",
        sa.Column("jti", UUID(as_uuid=True), nullable=False),  # NO server_default — deliberate
        sa.Column("account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("jti"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_index("mcp_tokens_account_idx", "mcp_tokens", ["account_id"])

    # ------------------------------------------------------------------
    # payment_events
    # ------------------------------------------------------------------
    op.create_table(
        "payment_events",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("amount_usd", sa.Numeric(10, 2), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("credited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="payment_events_tenant_id_fkey", ondelete="CASCADE"
        ),
    )
    op.create_index("payment_events_tenant_idx", "payment_events", ["tenant_id"])

    # ------------------------------------------------------------------
    # pending_file_deletes
    # ------------------------------------------------------------------
    op.create_table(
        "pending_file_deletes",
        sa.Column("file_id", sa.Text(), nullable=False),
        sa.Column("delete_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("file_id", name="pk_pending_file_deletes"),
    )
    op.create_index(
        "ix_pending_file_deletes_delete_after", "pending_file_deletes", ["delete_after"]
    )

    # ------------------------------------------------------------------
    # platform_principals
    # ------------------------------------------------------------------
    op.create_table(
        "platform_principals",
        sa.Column(
            "id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("account_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("active_agent_name", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "platform", "external_id", name="uq_platform_principal"),
    )
    op.create_index("ix_platform_principals_tenant_id", "platform_principals", ["tenant_id"])

    # ------------------------------------------------------------------
    # principal_links
    # ------------------------------------------------------------------
    op.create_table(
        "principal_links",
        sa.Column("cli_principal_id", UUID(as_uuid=True), nullable=False),
        sa.Column("platform_principal_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "cli_principal_id", "platform_principal_id", name="pk_principal_links"
        ),
        sa.ForeignKeyConstraint(["cli_principal_id"], ["cli_principals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["platform_principal_id"], ["platform_principals.id"], ondelete="CASCADE"
        ),
    )

    # ------------------------------------------------------------------
    # routines
    # ------------------------------------------------------------------
    op.create_table(
        "routines",
        sa.Column(
            "id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("created_by_user_id", sa.Text(), nullable=True),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("cron_expr", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False, server_default=sa.text("'UTC'")),
        sa.Column("trigger_message", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_result_tail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="routines_tenant_id_fkey", ondelete="CASCADE"
        ),
    )
    op.create_index(
        "routines_due_idx",
        "routines",
        ["next_fire_at"],
        postgresql_where=sa.text("enabled AND next_fire_at IS NOT NULL"),
    )
    op.create_index("routines_tenant_idx", "routines", ["tenant_id"])

    # ------------------------------------------------------------------
    # slack_bot_tokens
    # ------------------------------------------------------------------
    op.create_table(
        "slack_bot_tokens",
        sa.Column("team_id", sa.Text(), nullable=False),
        sa.Column("encrypted_token", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_token", sa.LargeBinary(), nullable=True),
        sa.PrimaryKeyConstraint("team_id"),
    )

    # ------------------------------------------------------------------
    # slack_connect_prompts
    # ------------------------------------------------------------------
    op.create_table(
        "slack_connect_prompts",
        sa.Column("team_id", sa.Text(), nullable=False),
        sa.Column("slack_user_id", sa.Text(), nullable=False),
        sa.Column("prompted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("team_id", "slack_user_id"),
    )

    # ------------------------------------------------------------------
    # slack_event_dedup
    # ------------------------------------------------------------------
    op.create_table(
        "slack_event_dedup",
        sa.Column("team_id", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("event_ts", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("team_id", "channel", "event_ts"),
    )

    # ------------------------------------------------------------------
    # slack_turn_contexts
    # ------------------------------------------------------------------
    op.create_table(
        "slack_turn_contexts",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("thread_ts", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_slack_turn_contexts_tenant_account",
        "slack_turn_contexts",
        ["tenant_id", "account_id"],
    )

    # ------------------------------------------------------------------
    # slack_user_tokens
    # ------------------------------------------------------------------
    op.create_table(
        "slack_user_tokens",
        sa.Column("team_id", sa.Text(), nullable=False),
        sa.Column("slack_user_id", sa.Text(), nullable=False),
        sa.Column("encrypted_token", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("encrypted_refresh_token", sa.LargeBinary(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("team_id", "slack_user_id"),
    )

    # ------------------------------------------------------------------
    # tenant_config
    # ------------------------------------------------------------------
    op.create_table(
        "tenant_config",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=True),
        sa.Column("environment_name", sa.Text(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False, server_default="agent"),
        sa.Column("agent_name_set_by_account_id", UUID(as_uuid=True), nullable=True),
        sa.Column("agent_name_set_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_tenant_config"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_tenant_config_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["agent_name_set_by_account_id"],
            ["accounts.id"],
            ondelete="SET NULL",
            name="fk_tenant_config_accounts",
        ),
        sa.CheckConstraint("mode IN ('agent', 'user_active')", name="ck_tenant_config_mode"),
    )

    # ------------------------------------------------------------------
    # tenant_ledger
    # ------------------------------------------------------------------
    op.create_table(
        "tenant_ledger",
        sa.Column(
            "id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("delta_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("payment_event_id", sa.Text(), nullable=True),
        sa.Column("payment_intent", sa.Text(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["payment_event_id"], ["payment_events.id"], ondelete="SET NULL"),
    )
    op.create_index("tenant_ledger_tenant_idx", "tenant_ledger", ["tenant_id"])
    op.create_index("tenant_ledger_idem_idx", "tenant_ledger", ["idempotency_key"], unique=True)

    # ------------------------------------------------------------------
    # tenant_user_caps (D-06: final name from the start; pkey/fkey names
    # reproduced explicitly since they survived the chain's guild_user_caps
    # -> tenant_user_caps rename unchanged — only the _not_null column
    # fossils are excluded, per the harness's normalization)
    # ------------------------------------------------------------------
    op.create_table(
        "tenant_user_caps",
        sa.Column(
            "id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("platform_user_id", sa.Text(), nullable=True),
        sa.Column("cap_usd", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="guild_user_caps_pkey"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="guild_user_caps_tenant_id_fkey",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "platform_user_id",
            name="uq_tenant_user_caps_tenant_user",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index("tenant_user_caps_tenant_idx", "tenant_user_caps", ["tenant_id"])

    # ------------------------------------------------------------------
    # thread_sessions
    # ------------------------------------------------------------------
    op.create_table(
        "thread_sessions",
        sa.Column(
            "id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("ma_session_id", sa.Text(), nullable=False),
        sa.Column("watermark_message_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'live'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("account_id", UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "thread_sessions_lookup_idx", "thread_sessions", ["tenant_id", "platform", "thread_id"]
    )
    op.create_index("thread_sessions_tenant_idx", "thread_sessions", ["tenant_id"])
    op.create_index(
        "thread_sessions_caller_lookup_idx",
        "thread_sessions",
        ["tenant_id", "platform", "thread_id", "account_id", "status"],
    )

    # ------------------------------------------------------------------
    # usage_events
    # ------------------------------------------------------------------
    op.create_table(
        "usage_events",
        sa.Column(
            "id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("platform_user_id", sa.Text(), nullable=True),
        sa.Column("managed_session_id", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "cache_read_input_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cache_creation_input_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="usage_events_tenant_id_fkey", ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "managed_session_id", "event_id", name="uq_usage_events_managed_session_event"
        ),
    )
    op.create_index("usage_events_occurred_idx", "usage_events", ["occurred_at"])
    op.create_index("usage_events_tenant_idx", "usage_events", ["tenant_id"])
    op.create_index(
        "usage_events_user_tenant_idx", "usage_events", ["tenant_id", "platform_user_id"]
    )

    # ------------------------------------------------------------------
    # user_config
    # ------------------------------------------------------------------
    op.create_table(
        "user_config",
        sa.Column("account_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=True),
        sa.Column("environment_name", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("account_id"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
    )

    # ------------------------------------------------------------------
    # user_skills
    # ------------------------------------------------------------------
    op.create_table(
        "user_skills",
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("principal_id", UUID(as_uuid=True), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source_repo_url", sa.Text(), nullable=False),
        sa.Column("source_repo_branch", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_path", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("anthropic_id", sa.Text(), nullable=True),
        sa.Column("anthropic_latest_version", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "principal_id", "agent_name", "name", name="pk_user_skills"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    raise NotImplementedError("initial schema — drop the database to reset")
