"""web_node_tokens: the web app keeps only tokens exchanged for connection nodes.

Web sign-in is Discord only. `web_node_tokens` is new, keyed by `token_key`
(`<issuer>#<server>`). Sign-in sessions of another provider are deleted and
`web_identities.groups` (which feed spend allowances, and nothing refreshes now) are
cleared. Nothing else is dropped or renamed, so the previous release keeps running
during a deploy: `web_oidc_tokens`, `web_group_roles` and
`web_workspace_settings.signin_off` stay, unread by the current code. A later
migration drops them. Downgrading drops `web_node_tokens` (its tokens are re-fetched on
use); the deleted sessions and cleared groups stay gone.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0157_web_node_tokens"
down_revision: str | None = "0156_workspace_members"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "web_node_tokens",
        sa.Column("person_id", UUID(as_uuid=True), nullable=False),
        sa.Column("token_key", sa.Text(), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("access_token_enc", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("person_id", "token_key", name="pk_web_node_tokens"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_web_node_tokens_tenants"
        ),
        sa.ForeignKeyConstraint(
            ["person_id"], ["web_people.id"], ondelete="CASCADE", name="fk_web_node_tokens_people"
        ),
    )
    # Dev sign-in also files under 'discord'; anything else is an OIDC, Slack or Microsoft session.
    op.execute("DELETE FROM web_sessions WHERE provider <> 'discord'")
    op.execute("UPDATE web_identities SET groups = '{}' WHERE groups <> '{}'")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("web_node_tokens")
