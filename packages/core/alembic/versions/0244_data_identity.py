"""data_identity: who each source identity is, link decisions and connections.

Runs `0004_identity.sql` through `daimon.data.revision`. Nothing reads or writes them until
the `data_sources` feature is on. As with 0239, a server that lacks what the store needs, or
an install without the `daimon-data` package, does nothing here; `daimon data init` applies
it later.

Its list holds the earlier files too, as 0241's does. Downgrading drops what this file made
and puts back 0001's reader functions (`0004_identity_down.sql`); 0240's downgrade drops the
store.

downgrade: destructive
schema: contract
"""

from __future__ import annotations

from alembic import op

revision: str = "0244_data_identity"
down_revision: str | None = "0243_discord_room_viewers"
branch_labels: str | None = None
depends_on: str | None = None

# Pinned and cumulative, as in 0231.
FILES: tuple[str, ...] = ("0001_store.sql", "0002_sync.sql", "0003_store.sql", "0004_identity.sql")


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
    data_revision.downgrade(op.get_bind(), "0004_identity.sql")
