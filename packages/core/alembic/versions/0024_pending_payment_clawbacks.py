"""Retain Stripe clawbacks delivered before their Checkout credit.

Verified refund/dispute events can precede checkout.session.completed. Keep
their payment intent and cumulative target until the matching credit arrives.

Revision ID: 0024_pending_payment_clawbacks
Revises: 0023_mcp_oauth_flows_url_ix

downgrade: destructive
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0024_pending_payment_clawbacks"
down_revision: str | None = "0023_mcp_oauth_flows_url_ix"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "pending_payment_clawbacks",
        sa.Column("event_id", sa.Text(), primary_key=True),
        sa.Column("payment_intent", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("target_amount_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "pending_payment_clawbacks_intent_idx",
        "pending_payment_clawbacks",
        ["payment_intent", "received_at"],
    )
    op.create_index(
        "pending_payment_clawbacks_received_idx",
        "pending_payment_clawbacks",
        ["received_at"],
    )


def downgrade() -> None:
    op.drop_index("pending_payment_clawbacks_received_idx", table_name="pending_payment_clawbacks")
    op.drop_index("pending_payment_clawbacks_intent_idx", table_name="pending_payment_clawbacks")
    op.drop_table("pending_payment_clawbacks")
