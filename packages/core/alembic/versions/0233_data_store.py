"""data_store: the data store's schemas, roles and tables.

Runs the store's pinned SQL files through `daimon.data.revision`: the `data` and
`data_views` schemas (named by `DAIMON_DATA__SCHEMA_NAME`), the four cluster-wide
`daimon_data_*` roles and, from later releases, the store's tables. Nothing reads them
until the `data_sources` feature is on.

A server that lacks what the store needs (CREATE on the database, CREATEROLE or the
roles made by an administrator), or an install without the `daimon-data` package, logs a
warning and does nothing here, so Daimon's own migrations still finish. `daimon data init`
sets the store up afterwards, and the older release never reads these schemas.

Downgrading drops both schemas and everything in them. The roles stay: other databases
on the same server may use them.

downgrade: destructive
"""

from __future__ import annotations

from alembic import op

revision: str = "0233_data_store"
down_revision: str | None = "0232_agent_chat_session_grant"
branch_labels: str | None = None
depends_on: str | None = None

# Pinned: a later file is a later revision, never an edit to this list.
FILES: tuple[str, ...] = ()


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
    data_revision.downgrade(op.get_bind())
