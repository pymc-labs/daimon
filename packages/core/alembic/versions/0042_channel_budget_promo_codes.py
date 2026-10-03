"""Promo codes that raise a channel's budget instead of the tenant balance.

A `channel_budget` code carries an amount and no credit window; redeeming it
adds the amount to one channel's budget limit. The redemption records that
channel in `promo_redemptions.channel_id` (NULL for credit and timed codes).

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0042_channel_budget_promo_codes"
down_revision: str | None = "0041_channel_tidy"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_constraint("ck_promo_codes_kind", "promo_codes", type_="check")
    op.create_check_constraint(
        "ck_promo_codes_kind", "promo_codes", "kind IN ('credit', 'timed', 'channel_budget')"
    )
    op.drop_constraint("ck_promo_codes_credit_window", "promo_codes", type_="check")
    op.create_check_constraint(
        "ck_promo_codes_credit_window",
        "promo_codes",
        "(kind IN ('credit', 'channel_budget')"
        " AND credit_starts_at IS NULL AND credit_ends_at IS NULL)"
        " OR (kind = 'timed' AND credit_starts_at < credit_ends_at)",
    )
    op.add_column("promo_redemptions", sa.Column("channel_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.execute(
        "DELETE FROM promo_redemptions WHERE promo_code_id IN"
        " (SELECT id FROM promo_codes WHERE kind = 'channel_budget')"
    )
    op.execute("DELETE FROM promo_codes WHERE kind = 'channel_budget'")
    op.drop_column("promo_redemptions", "channel_id")
    op.drop_constraint("ck_promo_codes_credit_window", "promo_codes", type_="check")
    op.create_check_constraint(
        "ck_promo_codes_credit_window",
        "promo_codes",
        "(kind = 'credit' AND credit_starts_at IS NULL AND credit_ends_at IS NULL)"
        " OR (kind = 'timed' AND credit_starts_at < credit_ends_at)",
    )
    op.drop_constraint("ck_promo_codes_kind", "promo_codes", type_="check")
    op.create_check_constraint("ck_promo_codes_kind", "promo_codes", "kind IN ('credit', 'timed')")
