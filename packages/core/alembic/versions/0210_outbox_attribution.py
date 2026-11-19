"""outbox_attribution: who asked and which channel budget, per usage row.

`accounting_outbox` gains nullable `platform_user_id` and `channel_id`, written when
the usage is recorded. A row that stays pending and is settled later by the drain
then debits the person's allowance and the budget channel the turn was admitted
under, instead of no person and the thread's own channel. Older rows stay NULL and
settle as before. Downgrading drops the columns; pending rows then settle as before.

downgrade: safe
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0210_outbox_attribution"
down_revision: str | None = "0209_thread_model_effort"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("accounting_outbox", sa.Column("platform_user_id", sa.Text(), nullable=True))
    op.add_column("accounting_outbox", sa.Column("channel_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("accounting_outbox", "channel_id")
    op.drop_column("accounting_outbox", "platform_user_id")
