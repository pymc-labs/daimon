"""Lease mcp_poll watches: claim_watch_polls also claims MCP server polls.

0156 let the firing transaction accept `mcp_poll` rows; their reader now ships,
so the claim leases them too. Nothing else in 0143's claim changes. The
function stays owned by daimon_watch_ingress_owner, with EXECUTE for the
sidecar only. The body stays frozen here; edit only in a new migration.

downgrade: unsupported
"""

import sqlalchemy as sa
from alembic import op

revision = "0157_watch_mcp_claim"
down_revision: str | None = "0156_watch_mcp_sources"
branch_labels = None
depends_on = None

# Frozen SQL: 0143's claim_watch_polls with `source IN ('poll','mcp_poll')`.
CLAIM_SQL = """
CREATE OR REPLACE FUNCTION {schema}.claim_watch_polls() RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $body$
DECLARE
    result jsonb := '[]'::jsonb;
    w record;
    token text;
    t timestamptz := clock_timestamp();
BEGIN
    PERFORM pg_advisory_xact_lock(813749201);
    IF NOT coalesce((SELECT enabled FROM {schema}.watch_controls WHERE scope='global'),false)
       THEN RETURN result; END IF;
    UPDATE {schema}.task_continuations q SET status='cancelled'
      FROM {schema}.watch_firings f JOIN {schema}.watches v ON v.id=f.watch_id
      WHERE q.idempotency_key=f.wake_key AND q.status IN ('pending','claimed')
        AND v.expires_at<=t;
    UPDATE {schema}.watches SET status='expired',lease_owner=NULL,lease_expires_at=NULL,
      revision=revision+1 WHERE expires_at<=t AND status IN ('active','paused');
    FOR w IN SELECT v.id FROM {schema}.watches v
      WHERE v.source IN ('poll','mcp_poll') AND v.status='active' AND v.expires_at>t
        AND (v.max_fires IS NULL OR v.fire_count<v.max_fires)
        AND v.source_state->>'reason' IS DISTINCT FROM 'source_unmapped'
        AND v.source_state->>'confirmed' IS DISTINCT FROM 'false'
        AND v.next_check_at<=t AND (v.backoff_until IS NULL OR v.backoff_until<=t)
        AND (v.lease_expires_at IS NULL OR v.lease_expires_at<=t)
        AND NOT EXISTS(SELECT 1 FROM {schema}.watch_controls WHERE NOT enabled
          AND scope IN ('tenant:'||v.tenant_id::text,'owner:'||v.owner_account_id::text))
      ORDER BY v.next_check_at,v.id LIMIT 4 FOR UPDATE SKIP LOCKED
    LOOP
      token := gen_random_uuid()::text;
      UPDATE {schema}.watches SET lease_owner=token,lease_expires_at=t+interval '60 seconds'
        WHERE id=w.id;
      result := result || jsonb_build_array(jsonb_build_object('watch_id',w.id,'lease',token));
    END LOOP;
    RETURN result;
END
$body$;
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    schema = bind.dialect.identifier_preparer.quote_schema(
        bind.scalar(sa.text("SELECT current_schema()"))
    )
    op.execute(CLAIM_SQL.format(schema=schema))
    # Restate 0143's ownership and grants, so the audit's approved shape holds
    # whatever the replace kept.
    op.execute(f"REVOKE ALL ON FUNCTION {schema}.claim_watch_polls() FROM PUBLIC")
    op.execute(f"GRANT CREATE ON SCHEMA {schema} TO daimon_watch_ingress_owner")
    op.execute(f"ALTER FUNCTION {schema}.claim_watch_polls() OWNER TO daimon_watch_ingress_owner")
    op.execute(f"GRANT EXECUTE ON FUNCTION {schema}.claim_watch_polls() TO daimon_sidecar")
    op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM daimon_watch_ingress_owner")


def downgrade() -> None:
    raise NotImplementedError("Watch evidence and delivery history must be retained")
