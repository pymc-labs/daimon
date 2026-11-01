"""web_audit_targets: audit rows name a person or invite by id, not by text.

Adds `target_person_id` and `target_invite_id` to `web_audit`, each cleared when the
person or invite is deleted. The trail now shows the current name or email while the
row exists and a generic label after erasure, so no name or address stays as free text.
Rows written before this named their target in `target`; those that held a person's
name or an invite's address are overwritten with the generic label. Additive columns;
downgrading drops the ids and keeps the generic labels.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0154_web_audit_targets"
down_revision: str | None = "0153_web_workspace_settings"
branch_labels: str | None = None
depends_on: str | None = None

_PERSON_ACTIONS = (
    "('Changed role', 'Suspended', 'Restored', 'Created sign-in link', 'Accepted owner link')"
)
_INVITE_ACTIONS = "('Created invite', 'Revoked invite')"
_ALLOWANCE_ACTIONS = "('Added allowance', 'Removed allowance')"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("web_audit", sa.Column("target_person_id", UUID(as_uuid=True), nullable=True))
    op.add_column("web_audit", sa.Column("target_invite_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_web_audit_target_person",
        "web_audit",
        "web_people",
        ["target_person_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_web_audit_target_invite",
        "web_audit",
        "web_invites",
        ["target_invite_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(f"UPDATE web_audit SET target = 'A person' WHERE action IN {_PERSON_ACTIONS}")
    op.execute(f"UPDATE web_audit SET target = 'An invite' WHERE action IN {_INVITE_ACTIONS}")
    op.execute(
        "UPDATE web_audit SET target = 'An invite' "
        "WHERE action = 'Accepted invite' AND target LIKE '%@%'"
    )
    # A group's allowance reads "Everyone" or "<name> group"; anything else named a person.
    op.execute(
        f"UPDATE web_audit SET target = 'A person' WHERE action IN {_ALLOWANCE_ACTIONS} "
        "AND target <> 'Everyone' AND target NOT LIKE '% group'"
    )


def downgrade() -> None:
    op.drop_constraint("fk_web_audit_target_invite", "web_audit", type_="foreignkey")
    op.drop_constraint("fk_web_audit_target_person", "web_audit", type_="foreignkey")
    op.drop_column("web_audit", "target_invite_id")
    op.drop_column("web_audit", "target_person_id")
