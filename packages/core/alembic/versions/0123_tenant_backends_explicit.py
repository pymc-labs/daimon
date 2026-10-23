"""tenant_backends_explicit: whether an admin chose the tenant's providers.

`tenant_access_policies.enabled_backends_explicit` is false for a tenant that
follows the deployment default: Anthropic plus every provider the deployment
holds a key for, resolved when the policy is read (`daimon.core.provider_keys`).
Such a row keeps `enabled_backends = ["anthropic"]`, so older builds read it as
before.

Existing rows become explicit when they could only come from a choice: any
list other than the old server default, or a tenant with a recorded admin
allowlist save. A row still on `["anthropic"]` with no such save follows the
default. Downgrading drops the column; older builds then read every row's
stored list, which for defaulted tenants is Anthropic only.

downgrade: safe
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0123_tenant_backends_explicit"
down_revision: str | None = "0120_mcp_token_surface"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "tenant_access_policies",
        sa.Column(
            "enabled_backends_explicit",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.execute(
        """
        UPDATE tenant_access_policies AS policy
        SET enabled_backends_explicit = true
        WHERE policy.enabled_backends <> '["anthropic"]'::jsonb
           OR EXISTS (
               SELECT 1 FROM security_audit_events AS event
               WHERE event.tenant_id = policy.tenant_id
                 AND event.tool_name = 'agent_backend'
                 AND event.operation = 'allowlist'
                 AND event.outcome = 'allowed'
           )
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("tenant_access_policies", "enabled_backends_explicit")
