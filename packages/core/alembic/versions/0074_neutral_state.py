"""Provider-neutral state: config revisions, bindings, leases, operations, journal, usage.

Adds the tables behind `daimon.core.stores.mux_state` and two nullable
columns on `thread_sessions` naming the binding a row backs. Nothing reads
them yet, and no existing query or default changes.

Backfill: every caller-owned `thread_sessions` row (account_id set) becomes
one generation of an Anthropic Managed Agents binding in that caller's
private slot, numbered by creation order, so a slot's newest row is its
current generation. The binding id is the slot's oldest row id. Rows with no
account (frozen before per-caller sessions) are left alone: they match no
caller today, and a binding with no account would read as a shared thread.
A row with no recorded channel gets an empty channel id. A session id that
two slots share stays with the slot that recorded it first.

downgrade: destructive
"""

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0074_neutral_state"
down_revision: str | None = "0073_github_removal_notice"
branch_labels: str | None = None
depends_on: str | None = None


def _tenant() -> sa.Column[uuid.UUID]:
    return sa.Column(
        "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )


def _slot_columns() -> list[sa.Column[str]]:
    return [
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=True),
    ]


def _binding_fk() -> sa.ForeignKey:
    return sa.ForeignKey("provider_binding_slot.binding_id", ondelete="CASCADE")


_SLOT = ("tenant_id", "platform", "channel_id", "thread_id", "account_id")

_BACKFILL = """
WITH rows AS (
    SELECT
        ts.id,
        ts.tenant_id,
        ts.platform,
        coalesce(ts.channel_id, '') AS channel_id,
        ts.thread_id,
        ts.account_id::text AS account_id,
        ts.ma_session_id,
        ts.ma_agent_id,
        row_number() OVER slot_order AS generation,
        first_value(ts.id::text) OVER slot_order AS binding_id,
        count(*) OVER (PARTITION BY ts.tenant_id, ts.platform, coalesce(ts.channel_id, ''),
                                    ts.thread_id, ts.account_id) AS generations,
        ts.created_at
    FROM thread_sessions ts
    WHERE ts.account_id IS NOT NULL
    WINDOW slot_order AS (
        PARTITION BY ts.tenant_id, ts.platform, coalesce(ts.channel_id, ''),
                     ts.thread_id, ts.account_id
        ORDER BY ts.created_at, ts.id
    )
),
slots AS (
    INSERT INTO provider_binding_slot
        (binding_id, tenant_id, platform, channel_id, thread_id, account_id, generation)
    SELECT binding_id, tenant_id, platform, channel_id, thread_id, account_id, generations
    FROM rows WHERE generation = 1
    RETURNING binding_id
),
bindings AS (
    INSERT INTO provider_binding
        (binding_id, generation, tenant_id, provider, profile, binding, created_at)
    SELECT
        r.binding_id, r.generation, r.tenant_id, 'anthropic', 'anthropic.managed_agents',
        jsonb_build_object(
            'id', r.binding_id,
            'thread', jsonb_build_object(
                'channel', jsonb_build_object(
                    'tenant_id', r.tenant_id::text,
                    'platform', r.platform,
                    'channel_id', r.channel_id
                ),
                'thread_id', r.thread_id
            ),
            'provider', 'anthropic',
            'profile', 'anthropic.managed_agents',
            'native_refs', jsonb_strip_nulls(
                jsonb_build_object('session', r.ma_session_id, 'agent', r.ma_agent_id)
            ),
            'generation', r.generation,
            'config_revision', 0,
            'legacy_account_id', r.account_id
        ),
        r.created_at
    FROM rows r JOIN slots s ON s.binding_id = r.binding_id
    RETURNING binding_id
)
UPDATE thread_sessions ts
SET binding_id = r.binding_id, binding_generation = r.generation
FROM rows r
WHERE ts.id = r.id AND EXISTS (SELECT 1 FROM bindings b WHERE b.binding_id = r.binding_id)
"""

_SESSIONS = """
INSERT INTO journal_session (session_id, tenant_id, binding_id)
SELECT DISTINCT ON (ts.ma_session_id) ts.ma_session_id, ts.tenant_id, ts.binding_id
FROM thread_sessions ts
WHERE ts.binding_id IS NOT NULL
ORDER BY ts.ma_session_id, ts.created_at, ts.id
ON CONFLICT (session_id) DO NOTHING
"""


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.create_table(
        "channel_config_revision",
        _tenant(),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("local", sa.Integer(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("revision", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("tenant_id", "platform", "channel_id", "local"),
    )
    op.create_table(
        "provider_binding_slot",
        sa.Column("binding_id", sa.Text(), primary_key=True),
        _tenant(),
        *_slot_columns(),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            *_SLOT, name="uq_provider_binding_slot", postgresql_nulls_not_distinct=True
        ),
    )
    op.create_table(
        "provider_binding",
        sa.Column("binding_id", sa.Text(), _binding_fk(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        _tenant(),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("profile", sa.Text(), nullable=False),
        sa.Column("binding", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("binding_id", "generation"),
    )
    op.create_table(
        "thread_lease",
        sa.Column("id", sa.UUID(), server_default=sa.func.gen_random_uuid(), primary_key=True),
        _tenant(),
        *_slot_columns(),
        sa.Column("last_fence", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            *_SLOT, name="uq_thread_lease_slot", postgresql_nulls_not_distinct=True
        ),
    )
    op.create_table(
        "operation",
        _tenant(),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("tenant_id", "account_id", "key"),
    )
    op.create_table(
        "journal_session",
        sa.Column("session_id", sa.Text(), primary_key=True),
        _tenant(),
        sa.Column("binding_id", sa.Text(), _binding_fk(), nullable=False),
        sa.Column("next_sequence", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("projection", postgresql.JSONB(), nullable=True),
    )
    op.create_table(
        "journal",
        sa.Column(
            "session_id",
            sa.Text(),
            sa.ForeignKey("journal_session.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        _tenant(),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("preview", sa.Boolean(), nullable=False),
        sa.Column("event", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("session_id", "sequence"),
        sa.UniqueConstraint(
            "session_id", "source_key", "revision", "preview", name="uq_journal_source"
        ),
    )
    op.create_table(
        "usage_observation",
        sa.Column("binding_id", sa.Text(), _binding_fk(), nullable=False),
        sa.Column("observation_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        _tenant(),
        sa.Column("applied", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("binding_id", "observation_id", "revision"),
    )
    op.create_table(
        "accounting_outbox",
        sa.Column("binding_id", sa.Text(), _binding_fk(), nullable=False),
        sa.Column("observation_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("prior_applied_revision", sa.Integer(), nullable=True),
        _tenant(),
        sa.Column("row", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("binding_id", "observation_id", "revision"),
    )
    op.create_index(
        "accounting_outbox_pending_idx",
        "accounting_outbox",
        ["created_at"],
        postgresql_where=sa.text("applied_at IS NULL"),
    )
    op.add_column("thread_sessions", sa.Column("binding_id", sa.Text(), nullable=True))
    op.add_column("thread_sessions", sa.Column("binding_generation", sa.Integer(), nullable=True))
    connection.execute(sa.text(_BACKFILL))
    connection.execute(sa.text(_SESSIONS))


def downgrade() -> None:
    op.drop_column("thread_sessions", "binding_generation")
    op.drop_column("thread_sessions", "binding_id")
    op.drop_index("accounting_outbox_pending_idx", table_name="accounting_outbox")
    for table in (
        "accounting_outbox",
        "usage_observation",
        "journal",
        "journal_session",
        "operation",
        "thread_lease",
        "provider_binding",
        "provider_binding_slot",
        "channel_config_revision",
    ):
        op.drop_table(table)
