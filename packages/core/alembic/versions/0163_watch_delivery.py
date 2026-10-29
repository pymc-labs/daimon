"""Watches can report into the owner's DM or a new thread per fire.

`watches.delivery` is 'thread' (every existing row), 'dm' or 'new_thread'. For
the last two `session_id` is NULL: each fire runs on a fresh session, and
`thread_id`/`parent_channel_id` keep naming the place the watch was made, which
is the provenance its policy and budget use (like a DM routine's source). Only
polled watches may use them.

Their wakes use the new reason 'watch_report', so the thread wake path, which
lists rows by `thread_id`, can never pick one up. `watch_firings` records the
thread a 'new_thread' fire made and the session a 'dm' fire ran on, each
written once. `accept_watch_poll_result` checks the live thread session only for
'thread' delivery; the owner principal, account and ready tenant are checked
for all three. The body stays frozen here; edit only in a new migration.

downgrade: unsupported
"""

import sqlalchemy as sa
from alembic import op

revision = "0163_watch_delivery"
down_revision: str | None = "0162_watch_credentials"
branch_labels = None
depends_on = None

# Frozen SQL: 0156's accept_watch_poll_result with the delivery-aware binding check,
# the wake reason and plainer wake text.
POLL_SQL = """
CREATE OR REPLACE FUNCTION {schema}.accept_watch_poll_result(
    p_watch uuid, p_lease text, p_value jsonb, p_payload text
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $body$
DECLARE
    w {schema}.watches%ROWTYPE;
    c record;
    k uuid;
    t timestamptz := clock_timestamp();
    n bigint;
    p_event uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(813749201);
    SELECT * INTO w FROM {schema}.watches WHERE id=p_watch FOR UPDATE;
    IF NOT FOUND OR w.source NOT IN ('poll','mcp_poll') OR w.status <> 'active'
       OR w.expires_at <= t OR w.lease_owner IS DISTINCT FROM p_lease
       OR w.lease_expires_at <= t OR w.lease_expires_at IS NULL
       OR NOT coalesce((SELECT enabled FROM {schema}.watch_controls WHERE scope='global'),false)
       OR EXISTS (SELECT 1 FROM {schema}.watch_controls WHERE NOT enabled AND
          scope IN ('tenant:'||w.tenant_id::text,'owner:'||w.owner_account_id::text)) THEN
        RAISE EXCEPTION 'poll registration refused' USING ERRCODE='42501';
    END IF;
    IF p_value IS NULL OR octet_length(p_value::text)>65536
       OR (p_payload IS NOT NULL AND octet_length(p_payload) NOT BETWEEN 1 AND 16384) THEN
        RAISE EXCEPTION 'poll evidence bounds' USING ERRCODE='22023'; END IF;
    p_event := p_lease::uuid;
    SELECT 0 AS generation INTO c;
    IF (w.max_fires IS NOT NULL AND w.fire_count >= w.max_fires) OR
       NOT EXISTS (SELECT 1 FROM {schema}.accounts a JOIN {schema}.tenants g
          ON g.id=a.tenant_id WHERE a.id=w.owner_account_id AND a.tenant_id=w.tenant_id
          AND g.archived_at IS NULL AND g.provision_status='ready') OR
       NOT EXISTS (SELECT 1 FROM {schema}.platform_principals p WHERE p.tenant_id=w.tenant_id
          AND p.platform=w.platform AND p.external_id=w.owner_external_id
          AND p.account_id=w.owner_account_id) OR
       (w.delivery='thread' AND NOT EXISTS (SELECT 1 FROM {schema}.thread_sessions s
          WHERE s.tenant_id=w.tenant_id
          AND s.account_id=w.owner_account_id AND s.platform=w.platform
          AND s.thread_id=w.thread_id AND s.channel_id=w.parent_channel_id
          AND s.ma_agent_id=w.agent_id AND s.ma_session_id=w.session_id
          AND s.status='live')) THEN
        RAISE EXCEPTION 'poll binding refused' USING ERRCODE='42501';
    END IF;
    IF p_payload IS NULL THEN
        UPDATE {schema}.watches SET baseline=jsonb_build_object('value',p_value),
          lease_owner=NULL,lease_expires_at=NULL,consecutive_errors=0,backoff_until=NULL,
          next_check_at=t+make_interval(secs=>(source_state->>'interval')::int)
          WHERE id=w.id;
        RETURN NULL;
    END IF;
    -- Rows and payloads expire after seven days; freshness prevents replay after deletion.
    DELETE FROM {schema}.watch_inbox WHERE accepted_at < t-interval '7 days';
    UPDATE {schema}.watch_firings SET events='[]'::jsonb
        WHERE created_at < t-interval '7 days' AND events <> '[]'::jsonb;
    SELECT count(*) INTO n FROM {schema}.watch_inbox
        WHERE accepted_at > t-interval '1 minute';
    IF n >= 600 THEN RAISE EXCEPTION 'global event rate' USING ERRCODE='54000'; END IF;
    SELECT count(*) INTO n FROM {schema}.watch_inbox WHERE watch_id=w.id
        AND accepted_at > t-interval '1 minute';
    IF n >= 60 THEN RAISE EXCEPTION 'watch event rate' USING ERRCODE='54000'; END IF;
    SELECT count(*) INTO n FROM {schema}.watch_inbox e JOIN {schema}.watches v ON v.id=e.watch_id
        WHERE v.owner_account_id=w.owner_account_id AND e.accepted_at>t-interval '1 minute';
    IF n >= 120 THEN RAISE EXCEPTION 'owner event rate' USING ERRCODE='54000'; END IF;
    SELECT count(*) INTO n FROM {schema}.watch_inbox e JOIN {schema}.watches v ON v.id=e.watch_id
        WHERE v.tenant_id=w.tenant_id AND e.accepted_at>t-interval '1 minute';
    IF n >= 300 THEN RAISE EXCEPTION 'tenant event rate' USING ERRCODE='54000'; END IF;
    IF (SELECT count(*) FROM {schema}.watch_inbox WHERE watch_id=w.id) >= 1000
       OR (SELECT count(*) FROM {schema}.watch_inbox) >= 100000
       OR (SELECT coalesce(sum(octet_length(payload)),0) FROM {schema}.watch_inbox
           WHERE watch_id=w.id)+octet_length(p_payload)>524288
       OR (SELECT coalesce(sum(octet_length(e.payload)),0) FROM {schema}.watch_inbox e
           JOIN {schema}.watches v ON v.id=e.watch_id WHERE v.owner_account_id=w.owner_account_id)
           +octet_length(p_payload)>1048576
       OR (SELECT coalesce(sum(octet_length(e.payload)),0) FROM {schema}.watch_inbox e
           JOIN {schema}.watches v ON v.id=e.watch_id WHERE v.tenant_id=w.tenant_id)
           +octet_length(p_payload)>4194304
       OR (SELECT coalesce(sum(octet_length(payload)),0) FROM {schema}.watch_inbox)
           +octet_length(p_payload)>16777216 THEN
        RAISE EXCEPTION 'evidence quota' USING ERRCODE='54000';
    END IF;
    -- New evidence must not join a batch whose reader retention already expired.
    SELECT f.wake_key INTO k FROM {schema}.watch_firings f JOIN {schema}.task_continuations q
      ON q.idempotency_key=f.wake_key WHERE f.watch_id=w.id AND f.revision=w.revision
      AND f.generation=c.generation AND f.created_at>t-interval '7 days'
      AND q.status='pending' AND q.started_at IS NULL
      ORDER BY f.created_at DESC LIMIT 1 FOR UPDATE OF q;
    IF k IS NULL THEN
        SELECT count(*) INTO n FROM {schema}.watch_firings WHERE watch_id=w.id
            AND created_at>t-interval '1 day';
        IF n >= least(w.max_turns_per_day,24) THEN
            RAISE EXCEPTION 'watch daily cap' USING ERRCODE='54000'; END IF;
        SELECT count(*) INTO n FROM {schema}.watch_firings f
          JOIN {schema}.watches v ON v.id=f.watch_id
            WHERE v.owner_account_id=w.owner_account_id AND f.created_at>t-interval '1 day';
        IF n >= 100 THEN RAISE EXCEPTION 'owner daily cap' USING ERRCODE='54000'; END IF;
        k := gen_random_uuid();
        INSERT INTO {schema}.task_continuations
          (tenant_id,platform,parent_channel_id,thread_id,requester_account_id,
           requester_external_user_id,target_ma_agent_id,target_name,
           requested_work,reason,idempotency_key,available_at)
        VALUES(w.tenant_id,w.platform,w.parent_channel_id,w.thread_id,w.owner_account_id,
          w.owner_external_id,w.agent_id,w.agent_name,'Page evidence is ready.',
          CASE WHEN w.delivery='thread' THEN 'watch_fired' ELSE 'watch_report' END,k,t);
        INSERT INTO {schema}.watch_firings(wake_key,watch_id,revision,generation,created_at)
          VALUES(k,w.id,w.revision,c.generation,t);
    END IF;
    INSERT INTO {schema}.watch_inbox(watch_id,producer,event_id,payload,wake_key,accepted_at)
      VALUES(w.id,w.producer,p_event::text,p_payload,k,t);
    UPDATE {schema}.watch_firings SET
      events=(CASE WHEN jsonb_array_length(events)>=20 THEN events-0 ELSE events END)
        ||jsonb_build_array(jsonb_build_object('event_id',p_event::text,'text',p_payload)),
      omitted=omitted+CASE WHEN jsonb_array_length(events)>=20 THEN 1 ELSE 0 END
      WHERE wake_key=k;
    UPDATE {schema}.watches SET fire_count=fire_count+1 WHERE id=w.id;
    UPDATE {schema}.watches SET baseline=jsonb_build_object('value',p_value),
      lease_owner=NULL,lease_expires_at=NULL,consecutive_errors=0,backoff_until=NULL,
      next_check_at=CASE WHEN max_fires IS NOT NULL AND fire_count>=max_fires THEN NULL
        ELSE t+make_interval(secs=>(source_state->>'interval')::int) END
      WHERE id=w.id;
    RETURN k;
END
$body$;
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    schema = bind.dialect.identifier_preparer.quote_schema(
        bind.scalar(sa.text("SELECT current_schema()"))
    )
    op.execute("ALTER TABLE watches ADD COLUMN delivery text NOT NULL DEFAULT 'thread'")
    op.execute("ALTER TABLE watches ALTER COLUMN session_id DROP NOT NULL")
    op.create_check_constraint(
        "ck_watches_delivery", "watches", "delivery IN ('thread', 'dm', 'new_thread')"
    )
    # A thread watch is bound to one live session; the others never are.
    op.create_check_constraint(
        "ck_watches_delivery_session",
        "watches",
        "(delivery = 'thread') = (session_id IS NOT NULL)",
    )
    # Page events insert 'watch_fired' wakes only, so they keep thread delivery.
    op.create_check_constraint(
        "ck_watches_delivery_source",
        "watches",
        "delivery = 'thread' OR source IN ('poll', 'mcp_poll')",
    )
    op.execute("ALTER TABLE watch_firings ADD COLUMN delivery_thread_id text")
    op.execute("ALTER TABLE watch_firings ADD COLUMN delivery_session_id text")
    op.drop_constraint("ck_task_continuations_reason", "task_continuations", type_="check")
    op.create_check_constraint(
        "ck_task_continuations_reason",
        "task_continuations",
        "reason IN ('task_handoff', 'private_input_applied', 'timer', "
        "'github_access_ready', 'worker_finished', 'watch_fired', 'job_finished', "
        "'watch_report')",
    )
    op.execute(POLL_SQL.format(schema=schema))
    op.execute(
        f"REVOKE ALL ON FUNCTION {schema}.accept_watch_poll_result(uuid,text,jsonb,text) "
        "FROM PUBLIC,daimon_sidecar,daimon_watch_ingress_owner"
    )


def downgrade() -> None:
    raise NotImplementedError("Watch evidence and delivery history must be retained")
