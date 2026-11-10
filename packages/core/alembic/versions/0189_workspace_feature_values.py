"""workspace settings as feature rows: typed mentions, direct messages, web modules

Copies four workspace settings into `feature_values` (`plain_text_mentions`,
`direct_messages`, `dm_memory_read_only`, one `web.<module>` Off per switched-off module).
Only the On or Off that differs from the default is copied. The old columns stay, unread,
so an older build can still start. A downgrade first resets those columns to their defaults,
so a value set back to Default after the upgrade does not return, then copies the rows back
and removes them.

Revision ID: 0189_workspace_feature_values
Revises: 0188_eval_key

downgrade: safe
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0189_workspace_feature_values"
down_revision: str | None = "0188_eval_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MODULE_NAMES = ("chat", "agents", "channels", "routines", "credits", "integrations")
_MODULES = str(_MODULE_NAMES)
_INSERT = (
    "INSERT INTO feature_values (tenant_id, tier, key, value, surface) "
    "SELECT {tenant}, 'workspace', {key}, {value}, 'migration' FROM {source} "
    "ON CONFLICT DO NOTHING"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for source, tenant, key, value in (
        ("tenants WHERE plain_text_mentions", "id", "'plain_text_mentions'", "'on'"),
        ("direct_message_policies WHERE enabled", "tenant_id", "'direct_messages'", "'on'"),
        (
            "tenant_access_policies WHERE (policy->>'dm_memory_read_only')::boolean",
            "tenant_id",
            "'dm_memory_read_only'",
            "'on'",
        ),
    ):
        op.execute(_INSERT.format(tenant=tenant, key=key, value=value, source=source))
    op.execute(
        _INSERT.format(
            tenant="w.tenant_id",
            key="'web.' || m",
            value="'off'",
            source=f"web_workspace_settings w, unnest(w.modules_off) AS m WHERE m IN {_MODULES}",
        )
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # The old columns went stale on upgrade: a value reset to Default deleted its row, but
    # the column still holds what it had. Reset them, so only a live row is copied back.
    op.execute("UPDATE tenants SET plain_text_mentions = false WHERE plain_text_mentions")
    op.execute("UPDATE direct_message_policies SET enabled = false WHERE enabled")
    op.execute(
        "UPDATE tenant_access_policies SET policy = policy - 'dm_memory_read_only' "
        "WHERE policy ->> 'dm_memory_read_only' IS NOT NULL"
    )
    op.execute(
        "UPDATE web_workspace_settings SET modules_off = ARRAY("
        f"SELECT m FROM unnest(modules_off) AS m WHERE m NOT IN {_MODULES}) "
        f"WHERE modules_off && ARRAY{list(_MODULE_NAMES)}"
    )
    where = "f.tenant_id = {table}.tenant_id AND f.tier = 'workspace' AND f.key = '{key}'"
    op.execute(
        "UPDATE tenants SET plain_text_mentions = (f.value = 'on') FROM feature_values f "
        "WHERE f.tenant_id = tenants.id AND f.tier = 'workspace' AND f.key = 'plain_text_mentions'"
    )
    op.execute(
        "INSERT INTO direct_message_policies (tenant_id, enabled) "
        "SELECT tenant_id, value = 'on' FROM feature_values "
        "WHERE tier = 'workspace' AND key = 'direct_messages' "
        "ON CONFLICT (tenant_id) DO UPDATE SET enabled = excluded.enabled"
    )
    op.execute(
        "UPDATE tenant_access_policies SET policy = policy || "
        "jsonb_build_object('dm_memory_read_only', f.value = 'on') FROM feature_values f "
        f"WHERE {where.format(table='tenant_access_policies', key='dm_memory_read_only')}"
    )
    op.execute(
        "INSERT INTO web_workspace_settings (tenant_id, modules_off) "
        "SELECT tenant_id, array_agg(substr(key, 5)) FROM feature_values "
        "WHERE tier = 'workspace' AND key LIKE 'web.%' AND value = 'off' GROUP BY tenant_id "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "modules_off = web_workspace_settings.modules_off || excluded.modules_off"
    )
    op.execute(
        "DELETE FROM feature_values WHERE tier = 'workspace' AND ("
        "key IN ('plain_text_mentions', 'direct_messages', 'dm_memory_read_only') "
        "OR key LIKE 'web.%')"
    )
