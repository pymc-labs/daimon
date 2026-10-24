"""Erase retained owner-memory output from destination-free routines.

downgrade: safe
"""

from alembic import op

revision: str = "0140_clear_legacy_routine_output"
down_revision: str | None = "0139_thread_model_settings"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "UPDATE routines SET last_result_tail = NULL, last_error = NULL, "
        "delivery_payload = NULL, delivery_note = NULL "
        "WHERE destination_kind IS NULL AND destination_id IS NULL AND channel_id IS NULL"
    )


def downgrade() -> None:
    # Erased personal data cannot and must not be reconstructed.
    op.execute("SELECT 1")
