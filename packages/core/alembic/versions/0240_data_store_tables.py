"""data_store_tables: the data store's sources, content, access lists and policies.

Runs `0001_store.sql` through `daimon.data.revision`: sources, records and chunks, the
access lists and their functions, the row policies, scopes, grants and the audit trail,
plus `0001_grants.sql` (grants, and the vector table and BM25 index where the server has
those extensions). Nothing reads them until the `data_sources` feature is on.

As with 0233, a server that lacks what the store needs, or an install without the
`daimon-data` package, logs a warning and does nothing here; `daimon data init` applies
it later.

Downgrading drops both store schemas and everything in them, as 0233 does.

downgrade: destructive
schema: contract
"""

from __future__ import annotations

from alembic import op

revision: str = "0240_data_store_tables"
down_revision: str | None = "0239_data_store"
branch_labels: str | None = None
depends_on: str | None = None

# Pinned and cumulative: a later file is a later revision, never an edit to this list, and
# each revision lists every file before it, so one skipped earlier still runs.
FILES: tuple[str, ...] = ("0001_store.sql",)


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
