"""At most one open add_skill preview per person, thread and agent.

Cancels all but the newest open preview of each, then enforces it with a
partial unique index that the preview upsert targets.

downgrade: safe
"""

from alembic import op

revision: str = "0075_pending_skill_add_one_open"
down_revision: str | None = "0074_pending_skill_adds"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE pending_skill_adds AS older SET consumed_at = now()
        WHERE older.consumed_at IS NULL AND EXISTS (
            SELECT 1 FROM pending_skill_adds AS newer
            WHERE newer.consumed_at IS NULL
              AND newer.tenant_id = older.tenant_id
              AND newer.account_id = older.account_id
              AND newer.platform = older.platform
              AND newer.thread_id = older.thread_id
              AND newer.ma_agent_id = older.ma_agent_id
              AND (newer.created_at, newer.id) > (older.created_at, older.id)
        )
        """
    )
    op.create_index(
        "uq_pending_skill_adds_open",
        "pending_skill_adds",
        ["tenant_id", "account_id", "platform", "thread_id", "ma_agent_id"],
        unique=True,
        postgresql_where="consumed_at IS NULL",
    )


def downgrade() -> None:
    op.drop_index("uq_pending_skill_adds_open", table_name="pending_skill_adds")
