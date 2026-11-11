"""drop stored values of nine retired development features

`daimon_tools`, `turn_controls`, `checkpoint_prompt`, `artifact_images`, `status_log`,
`native_footer_cost`, `unmentioned_reply_hint`, `writers_none_notice` and `waiting_card` are
fixed behaviour, not features. Nothing reads their rows; they are removed so no channel
lists them as customised. Each affected tenant's `feature_revision` is bumped.
A downgrade restores nothing: an older build reads a missing row as inheriting.

Revision ID: 0191_drop_dev_toggle_values
Revises: 0190_long_job_features

downgrade: safe
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0191_drop_dev_toggle_values"
down_revision: str | None = "0190_long_job_features"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEYS = (
    "('daimon_tools', 'turn_controls', 'checkpoint_prompt', 'artifact_images', 'status_log', "
    "'native_footer_cost', 'unmentioned_reply_hint', 'writers_none_notice', 'waiting_card')"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "UPDATE tenants SET feature_revision = feature_revision + 1 "
        f"WHERE id IN (SELECT tenant_id FROM feature_values WHERE key IN {_KEYS})"
    )
    op.execute(f"DELETE FROM feature_values WHERE key IN {_KEYS}")


def downgrade() -> None:
    """The deleted rows stay gone; caches are refreshed so the older build reads inheritance."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("UPDATE tenants SET feature_revision = feature_revision + 1")
