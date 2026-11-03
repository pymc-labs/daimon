"""fold_caps_into_allowances: per-member monthly caps become cap rows of the allowance table.

A cap is a ceiling, not an allowance: allowances take the most generous row that governs
a person, so a cap folded into one would let a generous group, a lifetime allowance or a
visitor's invite step past it. `spend_allowances.is_cap` marks the rows the gate treats as
ceilings. A tenant's default cap becomes a monthly cap for the built-in `everyone` group,
and each per-user override a monthly cap for that person, both for every agent. No other
row is changed or added.

The old cap counted raw provider cost; a cap row counts billed dollars (raw cost times
the deployment markup, `DAIMON_BILLING__MARKUP`). Each cap is multiplied by that markup, to
the cent, so the effective limit is unchanged. The `tenant_user_caps` rows stay as a record.

downgrade: unsupported
"""

from __future__ import annotations

import os
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "0171_fold_caps_into_allowances"
down_revision: str | None = "0170_channel_changes"
branch_labels: str | None = None
depends_on: str | None = None

_EVERYONE = "everyone"
_CENT = Decimal("0.01")


def _markup() -> Decimal:
    raw = os.environ.get("DAIMON_BILLING__MARKUP", "").strip()
    return Decimal(raw) if raw else Decimal("1.0")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # IF NOT EXISTS: test databases build their tables from the models, which have it.
    op.execute(
        "ALTER TABLE spend_allowances ADD COLUMN IF NOT EXISTS is_cap boolean "
        "NOT NULL DEFAULT false"
    )
    conn = op.get_bind()
    markup = _markup()
    caps = conn.execute(
        sa.text("SELECT tenant_id, platform_user_id, cap_usd, updated_at FROM tenant_user_caps")
    ).all()
    for tenant_id, user_id, cap_usd, set_at in caps:
        kind, subject = ("group", _EVERYONE) if user_id is None else ("person", user_id)
        conn.execute(
            sa.text(
                "INSERT INTO spend_allowances (tenant_id, subject_kind, subject_id, "
                'amount_usd, "window", starts_at, is_cap) '
                "VALUES (:t, :k, :s, :a, 'month', :c, true)"
            ),
            {
                "t": tenant_id,
                "k": kind,
                "s": subject,
                "a": (Decimal(cap_usd) * markup).quantize(_CENT),
                "c": set_at,
            },
        )


def downgrade() -> None:
    raise NotImplementedError("Converted caps cannot be told from allowances set by hand")
