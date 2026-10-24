"""Retain selfhosted skill bundles in the scoped local catalog.

downgrade: unsupported
Removing the provider would strand retained versions used by active sessions.
"""

from alembic import op

revision: str = "0113_selfhosted_skill_catalog"
down_revision: str = "0112_selfhosted_resources"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_neutral_skill_provider", "neutral_skill_versions", type_="check")
    op.create_check_constraint(
        "ck_neutral_skill_provider",
        "neutral_skill_versions",
        "provider IN ('openai', 'gemini', 'selfhosted')",
    )


def downgrade() -> None:
    raise NotImplementedError("Retained selfhosted skill versions cannot be discarded")
