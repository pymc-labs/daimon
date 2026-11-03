"""Channel settings: a thread's parent on scope modes, where a routine was made, archived names.

`thread_participation_scopes` and `progress_plan_scopes` gain `parent_id`, the channel a
thread row sits under, so a channel can list its threads with their own setting. Slack
thread ids (`channel:ts`) and Teams ones (`channel;messageid=root`) carry their parent and
are backfilled; a Discord thread row learns its parent when next written. `routines`
gains `made_in_channel_id`, the channel a routine was created from. `platform_channel_names`
gains `archived`, as the platform last reported it. Downgrading drops the columns and
what they hold.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0173_channel_settings"
down_revision: str | None = "0172_fold_caps_into_allowances"
branch_labels: str | None = None
depends_on: str | None = None

_SCOPE_TABLES = ("thread_participation_scopes", "progress_plan_scopes")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in _SCOPE_TABLES:
        op.add_column(table, sa.Column("parent_id", sa.Text(), nullable=True))
        op.execute(
            f"UPDATE {table} SET parent_id = split_part(scope_id, ':', 1) "
            "WHERE scope = 'thread' AND platform = 'slack' AND position(':' IN scope_id) > 0"
        )
        op.execute(
            f"UPDATE {table} SET parent_id = split_part(scope_id, ';messageid=', 1) "
            "WHERE scope = 'thread' AND platform = 'teams' "
            "AND position(';messageid=' IN scope_id) > 0"
        )
        op.create_index(
            f"ix_{table}_parent",
            table,
            ["tenant_id", "platform", "parent_id"],
            postgresql_where=sa.text("parent_id IS NOT NULL"),
        )
    op.add_column("routines", sa.Column("made_in_channel_id", sa.Text(), nullable=True))
    op.add_column("platform_channel_names", sa.Column("archived", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("platform_channel_names", "archived")
    op.drop_column("routines", "made_in_channel_id")
    for table in _SCOPE_TABLES:
        op.drop_index(f"ix_{table}_parent", table_name=table)
        op.drop_column(table, "parent_id")
