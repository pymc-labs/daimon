"""data_sync: the data store's sync runs, source cursors and attachments.

Runs `0002_sync.sql` through `daimon.data.revision`, plus `0002_grants.sql`. Nothing reads
or writes them until the `data_sources` feature is on and the `data-sync` service runs.

As with 0230 and 0231, a server that lacks what the store needs, or an install without the
`daimon-data` package, logs a warning and does nothing here; `daimon data init` applies it
later.

Its list holds `0001_store.sql` too, so a store that 0231 skipped is set up here. Downgrading
drops the sync tables only (`0002_sync_down.sql`); 0231's downgrade drops the store.

downgrade: destructive
"""

from __future__ import annotations

from alembic import op

revision: str = "0232_data_sync"
down_revision: str | None = "0231_data_store_tables"
branch_labels: str | None = None
depends_on: str | None = None

# Pinned and cumulative, as in 0231.
FILES: tuple[str, ...] = ("0001_store.sql", "0002_sync.sql")


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
    data_revision.downgrade(op.get_bind(), "0002_sync.sql")
