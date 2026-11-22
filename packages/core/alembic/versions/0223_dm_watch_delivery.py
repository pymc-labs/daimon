"""Watches made in a DM report there.

A poll watch made in a DM was saved to report into that DM's thread, which is no place
a fire can run, so every fire was dropped. Discord rows become `delivery = 'dm'` (each
fire a fresh private turn of the owner); a Slack or Teams row is paused with a reason,
since only Discord can run one. Each changed row keeps what it had under
`source_state.dm_migration`, which the downgrade restores. Data only.

downgrade: safe
"""

from alembic import op

revision: str = "0223_dm_watch_delivery"
down_revision: str | None = "0222_secret_shares"
branch_labels: str | None = None
depends_on: str | None = None

# A watch made in a DM, on the old thread delivery.
_OLD_DM_WATCH = (
    "delivery = 'thread' AND source_state->'dm_migration' IS NULL "
    "AND (thread_id LIKE 'dm:%' OR parent_channel_id LIKE 'dm:%' "
    "OR (platform = 'teams' AND thread_id LIKE 'a:%'))"
)
_PAUSED_REASON = "Watch paused: a watch made in a DM cannot report there on this platform."


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        f"""
        UPDATE watches SET
            delivery = 'dm',
            session_id = NULL,
            revision = revision + 1,
            source_state = source_state
                || jsonb_build_object('dm_migration', jsonb_build_object('session_id', session_id))
        WHERE platform = 'discord' AND source IN ('poll', 'mcp_poll', 'callback')
          AND {_OLD_DM_WATCH}
        """
    )
    # Queued fires of the rows paused below go with them, as `pause_watch` does.
    op.execute(
        f"""
        UPDATE task_continuations SET
            status = 'cancelled', skip_reason = 'watch_revoked',
            lease_owner = NULL, lease_expires_at = NULL
        WHERE status IN ('pending', 'claimed') AND idempotency_key IN (
            SELECT wake_key FROM watch_firings WHERE watch_id IN (
                SELECT id FROM watches
                WHERE platform <> 'discord' AND status = 'active' AND {_OLD_DM_WATCH}
            )
        )
        """
    )
    op.execute(
        f"""
        UPDATE watches SET
            status = 'paused',
            paused_at = now(),
            revision = revision + 1,
            lease_owner = NULL,
            lease_expires_at = NULL,
            source_state = source_state
                || jsonb_build_object('dm_migration', jsonb_build_object('paused', true))
                || jsonb_build_object('reason', '{_PAUSED_REASON}')
        WHERE platform <> 'discord' AND status = 'active' AND {_OLD_DM_WATCH}
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        UPDATE watches SET
            delivery = 'thread',
            session_id = source_state->'dm_migration'->>'session_id',
            revision = revision + 1,
            source_state = source_state - 'dm_migration'
        WHERE delivery = 'dm' AND source_state->'dm_migration' ? 'session_id'
          AND source_state->'dm_migration'->>'session_id' IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE watches SET
            status = 'active',
            paused_at = NULL,
            revision = revision + 1,
            source_state = source_state - 'dm_migration' - 'reason'
        WHERE status = 'paused' AND source_state->'dm_migration' ? 'paused'
        """
    )
