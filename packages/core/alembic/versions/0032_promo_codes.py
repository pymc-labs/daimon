"""Promo codes, their per-tenant redemptions and the refused-attempt throttle.

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0032_promo_codes"
down_revision: str | None = "0031_tenant_turn_cap"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "promo_codes",
        sa.Column(
            "id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()
        ),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("amount_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("credit_starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credit_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeem_starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeem_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_redemptions", sa.Integer(), nullable=True),
        sa.Column("redeemed_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount_usd > 0", name="ck_promo_codes_amount_positive"),
        sa.CheckConstraint("kind IN ('credit', 'timed')", name="ck_promo_codes_kind"),
        sa.CheckConstraint(
            "(kind = 'credit' AND credit_starts_at IS NULL AND credit_ends_at IS NULL)"
            " OR (kind = 'timed' AND credit_starts_at < credit_ends_at)",
            name="ck_promo_codes_credit_window",
        ),
        sa.CheckConstraint(
            "redeem_starts_at IS NULL OR redeem_ends_at IS NULL"
            " OR redeem_starts_at < redeem_ends_at",
            name="ck_promo_codes_redeem_window",
        ),
        sa.CheckConstraint(
            "max_redemptions IS NULL OR max_redemptions > 0",
            name="ck_promo_codes_max_redemptions",
        ),
        sa.CheckConstraint(
            "redeemed_count >= 0"
            " AND (max_redemptions IS NULL OR redeemed_count <= max_redemptions)",
            name="ck_promo_codes_redeemed_count",
        ),
    )
    op.create_index("promo_codes_code_hash_idx", "promo_codes", ["code_hash"], unique=True)
    op.create_table(
        "promo_redemptions",
        sa.Column(
            "id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()
        ),
        sa.Column(
            "promo_code_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("promo_codes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "redeemed_by_account_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_usd", sa.Numeric(12, 6), nullable=True),
        sa.UniqueConstraint("promo_code_id", "tenant_id", name="uq_promo_redemptions_code_tenant"),
        sa.CheckConstraint(
            "expired_usd IS NULL OR expired_usd >= 0", name="ck_promo_redemptions_expired_usd"
        ),
    )
    op.create_index("promo_redemptions_tenant_idx", "promo_redemptions", ["tenant_id"])
    op.create_table(
        "promo_redeem_failures",
        sa.Column(
            "id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.func.gen_random_uuid()
        ),
        sa.Column(
            "tenant_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "promo_redeem_failures_tenant_idx", "promo_redeem_failures", ["tenant_id", "attempted_at"]
    )


def downgrade() -> None:
    op.drop_table("promo_redeem_failures")
    op.drop_table("promo_redemptions")
    op.drop_table("promo_codes")
