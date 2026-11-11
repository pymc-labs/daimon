"""reset every saved "Allow writes" approvals choice to Ask

While the operator switch for approval overrides was off, a saved "Allow writes" choice
had no effect. The switch is gone and saved choices always apply, so those dormant choices
would start allowing writes at once. "Allow writes" is `approvals: "off"` in a tenant's
access policy, on the workspace and on each channel or thread rule. Each is cleared to
inherit, which the policy stores by leaving `approvals` and `approvals_set_by` out; a rule
left open by that is dropped, as the policy does. Each affected tenant's `feature_revision`
is bumped. A downgrade restores nothing: an older build reads a missing choice as inheriting.

Revision ID: 0192_reset_allow_writes
Revises: 0191_drop_dev_toggle_values

downgrade: safe
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0192_reset_allow_writes"
down_revision: str | None = "0191_drop_dev_toggle_values"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RULES = (
    "CASE WHEN jsonb_typeof(p.policy->'channel_rules') = 'object' "
    "THEN p.policy->'channel_rules' ELSE '{}'::jsonb END"
)
_AFFECTED = (
    "(p.policy->>'approvals' = 'off' "
    f"OR EXISTS (SELECT 1 FROM jsonb_each({_RULES}) e WHERE e.value->>'approvals' = 'off'))"
)
_CLEARED = "- 'approvals' - 'approvals_set_by'"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "UPDATE tenants SET feature_revision = feature_revision + 1 WHERE id IN "
        f"(SELECT p.tenant_id FROM tenant_access_policies p WHERE {_AFFECTED})"
    )
    op.execute(
        f"""
        WITH reset AS (
            SELECT p.tenant_id,
                CASE WHEN p.policy->>'approvals' = 'off'
                     THEN p.policy {_CLEARED} ELSE p.policy END AS top,
                (SELECT jsonb_object_agg(r.key, r.rule) FROM (
                    SELECT e.key,
                           CASE WHEN e.value->>'approvals' = 'off'
                                THEN e.value {_CLEARED} ELSE e.value END AS rule,
                           e.value->>'approvals' = 'off' AS was_off
                    FROM jsonb_each({_RULES}) e
                ) r
                WHERE NOT (r.was_off
                           AND r.rule - 'readers' - 'writers' = '{{}}'::jsonb
                           AND coalesce(r.rule->>'readers', 'any') = 'any'
                           AND coalesce(r.rule->>'writers', 'any') = 'any')
                ) AS rules
            FROM tenant_access_policies p
            WHERE {_AFFECTED}
        )
        UPDATE tenant_access_policies p
        SET policy = CASE WHEN reset.rules IS NULL THEN reset.top - 'channel_rules'
                          ELSE jsonb_set(reset.top, '{{channel_rules}}', reset.rules) END
        FROM reset
        WHERE reset.tenant_id = p.tenant_id
        """
    )


def downgrade() -> None:
    """The cleared choices stay cleared; caches refresh so the older build reads inheritance."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("UPDATE tenants SET feature_revision = feature_revision + 1")
