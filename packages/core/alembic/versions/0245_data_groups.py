"""data_groups: source groups share group evidence; a person's personal reads can be off.

Runs `0007_groups.sql` through `daimon.data.revision`. Inert until a source names a group or a
person turns a part off. As with 0230, a server that lacks what the store needs, or
an install without the `daimon-data` package, does nothing here; `daimon data init` applies
it later.

Its list holds the earlier files too, as 0232's does. Downgrading drops what this file made
(`0007_groups_down.sql`); 0231's downgrade drops the store.

downgrade: destructive
"""

from __future__ import annotations

from alembic import op

revision: str = "0245_data_groups"
down_revision: str | None = "0244_data_memory"
branch_labels: str | None = None
depends_on: str | None = None

# Pinned and cumulative, as in 0231.
FILES: tuple[str, ...] = (
    "0001_store.sql",
    "0002_sync.sql",
    "0003_store.sql",
    "0004_identity.sql",
    "0005_ops.sql",
    "0006_redaction.sql",
    "0007_groups.sql",
)


def upgrade() -> None:
    try:
        from daimon.data import revision as data_revision
    except ImportError:
        return
    data_revision.upgrade(op.get_bind(), FILES)


def downgrade() -> None:
    try:
        from daimon.data import revision as data_revision
    except ImportError:
        return
    data_revision.downgrade(op.get_bind(), "0007_groups.sql")
