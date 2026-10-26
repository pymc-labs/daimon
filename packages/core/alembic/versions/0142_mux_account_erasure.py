"""Mux account erasure: settlement rows outlive their slot, and erased accounts are fenced.

Account deletion now erases a person's private provider slots (bindings, journal,
leases, operations). Two things have to survive that:

* `usage_observation` and `accounting_outbox` rows are financial evidence. They stop
  cascading from `provider_binding_slot` (their tenant foreign key still removes them
  with the tenant), and `accounting_binding_context` keeps the profile and channel the
  host needs to settle them.
* `erased_account` and `erased_principal` record which accounts (and their principals)
  were erased, so a writer that held a stale scope or lease cannot write for one
  afterwards. Triggers on the private-state tables enforce that rule in the database, so
  a process still running code from before this migration is held to it too: it can no
  longer recreate an erased account's slot, lease, operation or usage row. A trigger never
  waits for a lock: against an account whose erasure is running it refuses the write. Account and
  principal ids are random and never reused, so a returning person is unaffected.

Upgrade only adds tables, functions and triggers and drops two foreign keys; it rewrites
no rows. The downgrade drops them and puts the two foreign keys back NOT VALID, so rows
whose slot was already erased stay and only new rows are checked.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0142_mux_account_erasure"
down_revision: str | None = "0141_remove_fleet"
branch_labels: str | None = None
depends_on: str | None = None

_DETACHED = ("usage_observation", "accounting_outbox")

# A frozen copy of `daimon.core._erasure_guards`, which a test keeps equal to it.
GUARD_STATEMENTS: tuple[str, ...] = (
    """CREATE OR REPLACE FUNCTION mux_account_erased(subject text) RETURNS boolean
LANGUAGE plpgsql AS $guard$
DECLARE
    pattern constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    parsed uuid;
BEGIN
    IF subject IS NULL OR subject !~* pattern THEN
        RETURN false;
    END IF;
    parsed := subject::uuid;
    IF NOT pg_try_advisory_xact_lock_shared(hashtextextended(parsed::text, 0)) THEN
        RAISE EXCEPTION 'the account is being erased';
    END IF;
    RETURN EXISTS (SELECT 1 FROM erased_account WHERE account_id = parsed);
END
$guard$""",
    """CREATE OR REPLACE FUNCTION mux_principal_erased(subject text) RETURNS boolean
LANGUAGE plpgsql AS $guard$
DECLARE
    pattern constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    parsed uuid;
BEGIN
    IF subject IS NULL OR subject !~* pattern THEN
        RETURN false;
    END IF;
    parsed := subject::uuid;
    IF NOT pg_try_advisory_xact_lock_shared(hashtextextended(parsed::text, 0)) THEN
        RAISE EXCEPTION 'the account is being erased';
    END IF;
    RETURN EXISTS (SELECT 1 FROM erased_principal WHERE principal_id = parsed);
END
$guard$""",
    """CREATE OR REPLACE FUNCTION mux_reject_erased_owner() RETURNS trigger
LANGUAGE plpgsql AS $guard$
BEGIN
    IF mux_account_erased(NEW.account_id) THEN
        RAISE EXCEPTION 'the account was erased';
    END IF;
    RETURN NEW;
END
$guard$""",
    """CREATE OR REPLACE FUNCTION mux_reject_erased_operation() RETURNS trigger
LANGUAGE plpgsql AS $guard$
BEGIN
    IF mux_account_erased(NEW.account_id) OR mux_principal_erased(NEW.principal_id) THEN
        RAISE EXCEPTION 'the account was erased';
    END IF;
    RETURN NEW;
END
$guard$""",
    """CREATE OR REPLACE FUNCTION mux_reject_erased_usage() RETURNS trigger
LANGUAGE plpgsql AS $guard$
BEGIN
    IF mux_account_erased(NEW.applied #>> '{observation,session,account_id}') THEN
        RAISE EXCEPTION 'the account was erased';
    END IF;
    RETURN NEW;
END
$guard$""",
    """CREATE OR REPLACE TRIGGER provider_binding_slot_erased_account_guard
    BEFORE INSERT OR UPDATE ON provider_binding_slot
    FOR EACH ROW EXECUTE FUNCTION mux_reject_erased_owner()""",
    """CREATE OR REPLACE TRIGGER thread_lease_erased_account_guard
    BEFORE INSERT OR UPDATE ON thread_lease
    FOR EACH ROW EXECUTE FUNCTION mux_reject_erased_owner()""",
    """CREATE OR REPLACE TRIGGER thread_provider_release_erased_account_guard
    BEFORE INSERT OR UPDATE ON thread_provider_release
    FOR EACH ROW EXECUTE FUNCTION mux_reject_erased_owner()""",
    """CREATE OR REPLACE TRIGGER operation_erased_account_guard
    BEFORE INSERT OR UPDATE ON operation
    FOR EACH ROW EXECUTE FUNCTION mux_reject_erased_operation()""",
    """CREATE OR REPLACE TRIGGER usage_observation_erased_account_guard
    BEFORE INSERT ON usage_observation
    FOR EACH ROW EXECUTE FUNCTION mux_reject_erased_usage()""",
)

GUARD_REMOVAL: tuple[str, ...] = (
    """DROP TRIGGER IF EXISTS provider_binding_slot_erased_account_guard
    ON provider_binding_slot""",
    """DROP TRIGGER IF EXISTS thread_lease_erased_account_guard
    ON thread_lease""",
    """DROP TRIGGER IF EXISTS thread_provider_release_erased_account_guard
    ON thread_provider_release""",
    """DROP TRIGGER IF EXISTS operation_erased_account_guard
    ON operation""",
    """DROP TRIGGER IF EXISTS usage_observation_erased_account_guard
    ON usage_observation""",
    """DROP FUNCTION IF EXISTS mux_reject_erased_usage()""",
    """DROP FUNCTION IF EXISTS mux_reject_erased_operation()""",
    """DROP FUNCTION IF EXISTS mux_reject_erased_owner()""",
    """DROP FUNCTION IF EXISTS mux_principal_erased(text)""",
    """DROP FUNCTION IF EXISTS mux_account_erased(text)""",
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "accounting_binding_context",
        sa.Column("binding_id", sa.Text(), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("profile", sa.Text(), nullable=False),
        sa.Column("profiles", postgresql.ARRAY(sa.Text()), nullable=False),
    )
    op.create_table(
        "erased_account",
        sa.Column("account_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "erased_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "erased_principal",
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "erased_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    for table in _DETACHED:
        op.drop_constraint(f"{table}_binding_id_fkey", table, type_="foreignkey")
    for statement in GUARD_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in GUARD_REMOVAL:
        op.execute(statement)
    for table in _DETACHED:
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_binding_id_fkey "
            "FOREIGN KEY (binding_id) REFERENCES provider_binding_slot (binding_id) "
            "ON DELETE CASCADE NOT VALID"
        )
    op.drop_table("erased_principal")
    op.drop_table("erased_account")
    op.drop_table("accounting_binding_context")
