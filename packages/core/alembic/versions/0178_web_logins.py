"""Web logins: connected sign-in accounts remembered across platforms.

`web_logins` groups the accounts one member connected; `web_login_accounts` holds each
account once, with when it last proved itself. `web_sessions.login_id` is null for
invite sessions. Existing browser sign-ins are deleted: none of them is on a login, so
disconnecting an account could not reach them and everyone signs in once more.
Downgrading drops the groups.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0178_web_logins"
down_revision: str | None = "0177_invite_files"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "web_logins",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "web_login_accounts",
        sa.Column(
            "login_id",
            UUID(as_uuid=True),
            sa.ForeignKey("web_logins.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "last_proven_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("provider", "subject", name="pk_web_login_accounts"),
    )
    op.create_index("ix_web_login_accounts_login", "web_login_accounts", ["login_id"])
    op.add_column(
        "web_sessions",
        sa.Column(
            "login_id",
            UUID(as_uuid=True),
            sa.ForeignKey("web_logins.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.execute("DELETE FROM web_sessions")
    op.create_index("ix_web_sessions_login", "web_sessions", ["login_id"])
    op.create_index("ix_web_sessions_account", "web_sessions", ["provider", "subject"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_web_sessions_account", table_name="web_sessions")
    op.drop_index("ix_web_sessions_login", table_name="web_sessions")
    op.drop_column("web_sessions", "login_id")
    op.drop_index("ix_web_login_accounts_login", table_name="web_login_accounts")
    op.drop_table("web_login_accounts")
    op.drop_table("web_logins")
