"""fold_caps_into_allowances: per-member monthly caps become spend allowances.

A tenant's default cap becomes a monthly allowance for the built-in `everyone` group,
and each per-user override a monthly allowance for that person, both for every agent.
Where the person or group already has allowances, the stricter limit wins: each existing
allowance of the same subject is lowered to the cap when it was higher, and no second
monthly allowance is added when one for every agent exists. A person's override is also
held to the highest monthly `everyone` allowance that existed before, since a person's
own allowance replaces the group's.

The old cap counted raw cost; an allowance counts billed dollars, so a markup above one
makes the converted limit stricter. The `tenant_user_caps` rows stay as a record.

downgrade: unsupported
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0171_fold_caps_into_allowances"
down_revision: str | None = "0170_channel_changes"
branch_labels: str | None = None
depends_on: str | None = None

_EVERYONE = "everyone"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    conn = op.get_bind()
    # Overrides first, so a person's ceiling reads only allowances that predate the caps.
    caps = conn.execute(
        sa.text(
            "SELECT tenant_id, platform_user_id, cap_usd, updated_at FROM tenant_user_caps "
            "ORDER BY (platform_user_id IS NULL), tenant_id"
        )
    ).all()
    for tenant_id, user_id, cap_usd, set_at in caps:
        kind, subject = ("group", _EVERYONE) if user_id is None else ("person", user_id)
        amount = cap_usd
        if user_id is not None:
            ceiling = conn.execute(
                sa.text(
                    "SELECT max(amount_usd) FROM spend_allowances WHERE tenant_id = :t "
                    "AND subject_kind = 'group' AND subject_id = :e AND agent_id IS NULL "
                    "AND \"window\" = 'month'"
                ),
                {"t": tenant_id, "e": _EVERYONE},
            ).scalar_one()
            if ceiling is not None:
                amount = min(amount, ceiling)
        conn.execute(
            sa.text(
                "UPDATE spend_allowances SET amount_usd = LEAST(amount_usd, :a) "
                "WHERE tenant_id = :t AND subject_kind = :k AND subject_id = :s"
            ),
            {"a": amount, "t": tenant_id, "k": kind, "s": subject},
        )
        has_one = conn.execute(
            sa.text(
                "SELECT 1 FROM spend_allowances WHERE tenant_id = :t AND subject_kind = :k "
                "AND subject_id = :s AND agent_id IS NULL AND \"window\" = 'month' "
                "AND expires_at IS NULL"
            ),
            {"t": tenant_id, "k": kind, "s": subject},
        ).first()
        if has_one is None:
            conn.execute(
                sa.text(
                    "INSERT INTO spend_allowances (tenant_id, subject_kind, subject_id, "
                    "amount_usd, \"window\", starts_at) VALUES (:t, :k, :s, :a, 'month', :c)"
                ),
                {"t": tenant_id, "k": kind, "s": subject, "a": amount, "c": set_at},
            )


def downgrade() -> None:
    raise NotImplementedError("Converted caps cannot be told from allowances set by hand")
