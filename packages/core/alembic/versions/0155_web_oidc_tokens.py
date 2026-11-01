"""web_oidc_tokens: keep a sign-in's access token even when no refresh token came.

An embedded app then gets the token until it expires, and a new sign-in after that.
Downgrading deletes the rows without a refresh token.

downgrade: destructive
"""

from __future__ import annotations

from alembic import op

revision: str = "0155_web_oidc_tokens"
down_revision: str | None = "0154_web_audit_targets"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.alter_column("web_oidc_tokens", "refresh_token_enc", nullable=True)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DELETE FROM web_oidc_tokens WHERE refresh_token_enc IS NULL")
    op.alter_column("web_oidc_tokens", "refresh_token_enc", nullable=False)
