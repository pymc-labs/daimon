"""agent_chat_session_grant: where an agent-chat session came from.

Every send or resume on an agent-chat session copies three facts from the session's
own metadata into its `usage_reconcile_progress` row: `agent_chat_grant`, the signed
grant (`chat_session_id`) of the Daimon credential it mounts; `agent_chat_channel`,
the channel its tools were chosen for; and `agent_chat_account_id`, the account that
created it. The MCP server finds the session of a credential by its grant, so it can
tell an agent-chat session from a chat thread's without a provider call.

The columns are nullable with no default. The older release writes its rows without
naming them, so their values stay as they were until the next send of this release.
A row with no grant is not found: the session is treated as before this change. The
unique index covers only rows with a grant and is built concurrently, so sends keep
writing meanwhile. A retry after an interrupted run keeps the columns it added and builds
an invalid index again.

Downgrading drops the columns and, with them, the index. After an upgrade again, each session is
found once its next send copies them again.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0232_agent_chat_session_grant"
down_revision: str | None = "0231_agent_chat_send_count"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "usage_reconcile_progress"
INDEX = "uq_usage_reconcile_progress_agent_chat_grant"


#: Each column and its type as `information_schema.columns.data_type` names it.
COLUMNS = (
    ("agent_chat_grant", "text"),
    ("agent_chat_channel", "text"),
    ("agent_chat_account_id", "uuid"),
)


def add_columns(bind: sa.Connection) -> None:
    """Add each column that is missing. A run cut short after the ALTER committed (the index
    step below runs outside its transaction) left them, so a retry keeps a column only of
    the type this revision adds, and refuses any other."""
    for name, kind in COLUMNS:
        found = bind.scalar(
            sa.text(
                "SELECT data_type FROM information_schema.columns WHERE table_schema = "
                "current_schema() AND table_name = :table AND column_name = :column"
            ),
            {"table": TABLE, "column": name},
        )
        if found is None:
            op.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {name} {kind}")
        elif found != kind:
            raise RuntimeError(f"{TABLE}.{name} exists as {found}, not {kind}")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    add_columns(op.get_bind())
    with op.get_context().autocommit_block():  # commits the ALTER, releasing its lock
        invalid = op.get_bind().scalar(
            sa.text("SELECT NOT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"),
            {"name": INDEX},
        )
        if invalid:
            # An interrupted build leaves its index invalid: build it again.
            op.drop_index(INDEX, table_name=TABLE, postgresql_concurrently=True)
        op.create_index(
            INDEX,
            TABLE,
            ["agent_chat_grant"],
            unique=True,
            postgresql_where=sa.text("agent_chat_grant IS NOT NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # The index goes with its column.
    op.drop_column(TABLE, "agent_chat_account_id")
    op.drop_column(TABLE, "agent_chat_channel")
    op.drop_column(TABLE, "agent_chat_grant")
