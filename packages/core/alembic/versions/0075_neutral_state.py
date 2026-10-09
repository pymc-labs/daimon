"""Provider-neutral state: config revisions, bindings, leases, operations, journal, usage.

Adds the tables behind `daimon.core.stores.mux_state` and two nullable
columns on `thread_sessions` naming the binding a row backs. Nothing reads
them yet, and no existing query or default changes.

Backfill. A binding's current generation must be the row the legacy
reader (`get_live_thread_session`) returns today: the newest `live` row of
a caller's thread, keyed like that reader by (tenant, platform, thread,
account) and not by channel. So only caller-owned threads (account_id set)
with a live row get a binding. Its generations are the caller's rows with
the live ones last, each group in creation order, so the newest live row is
the highest generation; the binding id is the first row in that order and
the slot's channel is the newest channel any of its rows recorded ('' if
none). A thread with no live row gets no binding, as today it cold-creates
a fresh session. Rows with no account (frozen before per-caller sessions)
are left alone: they match no caller today, and a binding with no account
would read as a shared thread. Each backfilled generation records the row
it came from in `provider_binding.legacy_row_id`.

A session id recorded in more than one caller's thread is ambiguous: no
slot owns it, so it takes no journal appends and no usage, and a thread
whose live session is ambiguous gets no binding. The host resolves it by
binding explicitly.

Locking and consistency. The backfill reads `thread_sessions` once, in
one statement (one snapshot), into a temp table; everything it writes
derives from that. A row a legacy writer commits meanwhile is simply not
backfilled, like any row written after the migration. The steps that lock
existing tables run last, so their locks are held only until the commit:
the tenant FKs (SHARE ROW EXCLUSIVE on `tenants`) and the two nullable
`ADD COLUMN`s (ACCESS EXCLUSIVE on `thread_sessions`; no default, no
rewrite). The new
columns stay NULL here; `daimon.core.stores.mux_state.link_legacy_thread_sessions`
fills them in short batches, idempotently, whenever it is run.

downgrade: destructive
"""

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0075_neutral_state"
down_revision: str | None = "0074_pending_skill_adds"
branch_labels: str | None = None
depends_on: str | None = None


def _tenant() -> sa.Column[uuid.UUID]:
    """The tenant column. Its FK to `tenants` is added last (see `finish`)."""
    return sa.Column("tenant_id", sa.UUID(), nullable=False)


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

_CAPTURE = """
CREATE TEMP TABLE neutral_state_source ON COMMIT DROP AS
WITH caller_rows AS (
    SELECT
        ts.id, ts.tenant_id, ts.platform, ts.thread_id, ts.account_id::text AS account_id,
        ts.ma_session_id, ts.ma_agent_id, ts.status, ts.created_at,
        dense_rank() OVER (
            ORDER BY ts.tenant_id, ts.platform, ts.thread_id, ts.account_id
        ) AS slot,
        coalesce(
            first_value(ts.channel_id) OVER (
                PARTITION BY ts.tenant_id, ts.platform, ts.thread_id, ts.account_id
                ORDER BY ts.channel_id IS NULL, ts.created_at DESC, ts.id DESC
            ),
            ''
        ) AS channel_id
    FROM thread_sessions ts
    WHERE ts.account_id IS NOT NULL
),
ambiguous AS (
    SELECT ma_session_id FROM caller_rows GROUP BY ma_session_id HAVING count(DISTINCT slot) > 1
),
live AS (
    SELECT DISTINCT ON (slot) slot, ma_session_id
    FROM caller_rows WHERE status = 'live'
    ORDER BY slot, created_at DESC, id DESC
),
bound AS (
    SELECT live.slot FROM live
    WHERE live.ma_session_id NOT IN (SELECT ma_session_id FROM ambiguous)
),
rows AS (
    SELECT
        r.*,
        row_number() OVER slot_order AS generation,
        first_value(r.id::text) OVER slot_order AS binding_id,
        count(*) OVER (PARTITION BY r.slot) AS generations
    FROM caller_rows r JOIN bound USING (slot)
    WINDOW slot_order AS (PARTITION BY r.slot ORDER BY r.status = 'live', r.created_at, r.id)
)
SELECT rows.*, rows.ma_session_id IN (SELECT ma_session_id FROM ambiguous) AS ambiguous_session
FROM rows
"""
"""One statement, so one snapshot: everything the backfill writes derives from it."""

_BINDINGS = """
WITH slots AS (
    INSERT INTO provider_binding_slot
        (binding_id, tenant_id, platform, channel_id, thread_id, account_id, generation)
    SELECT binding_id, tenant_id, platform, channel_id, thread_id, account_id, generations
    FROM neutral_state_source WHERE generation = 1
    RETURNING binding_id
)
INSERT INTO provider_binding
    (binding_id, generation, tenant_id, provider, profile, binding, legacy_row_id, created_at)
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
    r.id,
    r.created_at
FROM neutral_state_source r JOIN slots s ON s.binding_id = r.binding_id
"""

_SESSIONS = """
INSERT INTO journal_session (session_id, tenant_id, binding_id)
SELECT DISTINCT ON (r.ma_session_id) r.ma_session_id, r.tenant_id, r.binding_id
FROM neutral_state_source r
WHERE NOT r.ambiguous_session
ORDER BY r.ma_session_id, r.created_at, r.id
ON CONFLICT (session_id) DO NOTHING
"""

_TENANT_TABLES = (
    "channel_config_revision",
    "provider_binding_slot",
    "provider_binding",
    "thread_lease",
    "operation",
    "journal_session",
    "journal",
    "usage_observation",
    "accounting_outbox",
)


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
        sa.Column("legacy_row_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("binding_id", "generation"),
    )
    op.create_index(
        "provider_binding_legacy_row_idx",
        "provider_binding",
        ["legacy_row_id"],
        postgresql_where=sa.text("legacy_row_id IS NOT NULL"),
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
    capture()
    derive()
    finish()


def capture() -> None:
    """Snapshot the legacy rows once. Reads `thread_sessions`; blocks no writer."""
    op.get_bind().execute(sa.text(_CAPTURE))


def derive() -> None:
    """Fill the new tables from the snapshot only, never from a second read."""
    connection = op.get_bind()
    connection.execute(sa.text(_BINDINGS))
    connection.execute(sa.text(_SESSIONS))
    connection.execute(sa.text("DROP TABLE neutral_state_source"))


def finish() -> None:
    """The steps that lock existing tables, last, so they are held only to the commit.

    The tenant FKs take SHARE ROW EXCLUSIVE on `tenants` (blocking tenant
    writes); the nullable ADD COLUMNs take ACCESS EXCLUSIVE on `thread_sessions`.
    Validating the FKs scans only the new tables. A tenant deleted while
    the backfill ran is gone from `tenants` but not from the snapshot, so its
    backfilled rows are dropped first, under the same lock, so no deletion
    can slip in between.
    """
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE tenants IN SHARE ROW EXCLUSIVE MODE"))
    # Cascades to provider_binding and journal_session through binding_id.
    connection.execute(
        sa.text(
            "DELETE FROM provider_binding_slot s"
            " WHERE NOT EXISTS (SELECT 1 FROM tenants t WHERE t.id = s.tenant_id)"
        )
    )
    for table in _TENANT_TABLES:
        op.create_foreign_key(
            f"{table}_tenant_id_fkey", table, "tenants", ["tenant_id"], ["id"], ondelete="CASCADE"
        )
    op.add_column("thread_sessions", sa.Column("binding_id", sa.Text(), nullable=True))
    op.add_column("thread_sessions", sa.Column("binding_generation", sa.Integer(), nullable=True))


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
