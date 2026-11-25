"""data_identity: who each source identity is, link decisions, data admins and connections.

Runs `0003_identity.sql` through `daimon.data.revision`. Nothing reads or writes them until
the `data_sources` feature is on. As with 0230, a server that lacks what the store needs, or
an install without the `daimon-data` package, does nothing here; `daimon data init` applies
it later.

Downgrading drops both store schemas and everything in them, as 0230 does.

downgrade: destructive
"""

from __future__ import annotations

from alembic import op

revision: str = "0241_data_identity"
down_revision: str | None = "0240_discord_room_viewers"
branch_labels: str | None = None
depends_on: str | None = None

# Pinned: a later file is a later revision, never an edit to this list.
FILES: tuple[str, ...] = ("0003_identity.sql",)


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
