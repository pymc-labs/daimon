"""data_search: the data store's datasets, metrics, read views and caller SQL entry point.

Runs `0003_store.sql` through `daimon.data.revision`, plus `0003_grants.sql`. Nothing reads
them until the `data_sources` feature is on.

As with 0233 to 0235, a server that lacks what the store needs, or an install without the
`daimon-data` package, logs a warning and does nothing here; `daimon data init` applies it
later.

Its list holds the earlier files too, as 0235's does. Downgrading drops what this file made
(`0003_store_down.sql`); 0234's downgrade drops the store.

downgrade: destructive
"""

from __future__ import annotations

from alembic import op

revision: str = "0236_data_search"
down_revision: str | None = "0235_data_sync"
branch_labels: str | None = None
depends_on: str | None = None

# Pinned and cumulative, as in 0234.
FILES: tuple[str, ...] = ("0001_store.sql", "0002_sync.sql", "0003_store.sql")


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
    data_revision.downgrade(op.get_bind(), "0003_store.sql")
