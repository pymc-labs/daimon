"""Page watches beyond Discord: the principal platform, permanent and new-thread page watches.

A web watch reports into a web thread but runs as the owner's linked chat principal, so
`watches.principal_platform` names the principal's platform (NULL: the watch's own). The
page and poll ports join the principal on it. A page watch may be permanent (it still ends
with its page) and may report into a new thread per firing, which needs no live session.

downgrade: destructive
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0225_page_watch_platforms"
down_revision: str | None = "0224_artifacts"
branch_labels: str | None = None
depends_on: str | None = None

# Frozen SQL: 0164's accept_watch_page_event and accept_watch_poll_result, joining the
# principal on `coalesce(principal_platform, platform)`; a page watch's live-session check
# and its 'watch_fired' reason apply to thread delivery only, as the poll port's do.
PAGE_SQL = """CREATE OR REPLACE FUNCTION {schema}.accept_watch_page_event(
    p_watch uuid, p_page uuid, p_key_id text, p_stamp bigint, p_event uuid,
    p_payload text, p_signature text, p_key text
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $body$
DECLARE
    w {schema}.watches%ROWTYPE;
    c {schema}.watch_capabilities%ROWTYPE;
    k uuid;
    t timestamptz := clock_timestamp();
    n bigint;
    watch_cap integer;
    owner_cap integer;
    expected bytea;
    supplied bytea;
    difference integer := 0;
    i integer;
BEGIN
    IF NOT coalesce((SELECT enabled FROM {schema}.watch_controls WHERE scope='global'),false)
       THEN RAISE EXCEPTION 'watch producers disabled' USING ERRCODE='42501'; END IF;
    IF p_payload IS NULL OR octet_length(p_payload) NOT BETWEEN 1 AND 4096
       OR p_event IS NULL OR p_page IS NULL OR p_key IS NULL
       OR octet_length(p_key) NOT BETWEEN 32 AND 256
       OR p_key_id IS NULL OR p_key_id !~ '^[A-Za-z0-9_-]{{1,64}}$'
       OR p_signature IS NULL OR p_signature !~ '^[0-9a-f]{{64}}$'
       OR p_stamp IS NULL OR abs(extract(epoch FROM t)-p_stamp) > 300 THEN
        RAISE EXCEPTION 'page signature refused' USING ERRCODE='42501';
    END IF;
    expected := public.hmac(convert_to('daimon-page-v1' || chr(10) || p_watch::text
       || chr(10) || p_page::text || chr(10) || p_key_id || chr(10) || p_stamp::text
       || chr(10) || p_event::text || chr(10)
       || encode(public.digest(convert_to(p_payload,'UTF8'),'sha256'),'hex'),'UTF8'),
       convert_to(p_key,'UTF8'),'sha256');
    supplied := decode(p_signature,'hex');
    FOR i IN 0..31 LOOP
        difference := difference | (get_byte(expected,i) # get_byte(supplied,i));
    END LOOP;
    IF difference <> 0 THEN
        RAISE EXCEPTION 'page signature refused' USING ERRCODE='42501';
    END IF;
    -- One global source lock makes rate/byte/row quotas exact across replicas.
    PERFORM pg_advisory_xact_lock(813749201);
    IF NOT coalesce((SELECT enabled FROM {schema}.watch_controls WHERE scope='global'),false)
       THEN RAISE EXCEPTION 'watch producers disabled' USING ERRCODE='42501'; END IF;
    SELECT * INTO w FROM {schema}.watches WHERE id=p_watch FOR UPDATE;
    t := clock_timestamp();
    IF abs(extract(epoch FROM t)-p_stamp)>300 THEN
        RAISE EXCEPTION 'page signature refused' USING ERRCODE='42501'; END IF;
    IF NOT FOUND OR w.source <> 'page_event' OR w.page_id <> p_page
       OR w.status <> 'active' OR (w.expires_at IS NOT NULL AND w.expires_at <= t)
       OR EXISTS (SELECT 1 FROM {schema}.watch_controls WHERE NOT enabled AND
          scope IN ('tenant:'||w.tenant_id::text,'owner:'||w.owner_account_id::text)) THEN
        RAISE EXCEPTION 'page registration refused' USING ERRCODE='42501';
    END IF;
    SELECT * INTO c FROM {schema}.watch_capabilities k WHERE k.watch_id=w.id
      AND k.key_id=p_key_id AND k.operation='page_event' AND k.revoked_at IS NULL
      AND k.expires_at > t AND digest=encode(public.digest(convert_to(p_key,'UTF8'),'sha256'),'hex')
      ORDER BY generation DESC LIMIT 1;
    IF NOT FOUND THEN RAISE EXCEPTION 'page key refused' USING ERRCODE='42501'; END IF;
    IF w.rule ? 'projection' THEN
        p_payload := {schema}.project_watch_document(p_payload::jsonb,w.rule->>'projection')::text;
        IF p_payload IS NULL OR p_payload IN ('null','false','""','[]','{{}}') THEN
            RETURN NULL; END IF;
    END IF;
    IF EXISTS (SELECT 1 FROM {schema}.watch_inbox
      WHERE producer=w.producer AND event_id=p_event::text) THEN
        RAISE EXCEPTION 'page event replay' USING ERRCODE='23505';
    END IF;
    IF (w.max_fires IS NOT NULL AND w.fire_count >= w.max_fires) OR
       NOT EXISTS (SELECT 1 FROM {schema}.accounts a JOIN {schema}.tenants g
          ON g.id=a.tenant_id WHERE a.id=w.owner_account_id AND a.tenant_id=w.tenant_id
          AND g.archived_at IS NULL AND g.provision_status='ready') OR
       NOT EXISTS (SELECT 1 FROM {schema}.platform_principals p WHERE p.tenant_id=w.tenant_id
          AND p.platform=coalesce(w.principal_platform,w.platform)
          AND p.external_id=w.owner_external_id
          AND p.account_id=w.owner_account_id) OR
       (w.delivery='thread' AND NOT EXISTS (SELECT 1 FROM {schema}.thread_sessions s
          WHERE s.tenant_id=w.tenant_id
          AND s.account_id=w.owner_account_id AND s.platform=w.platform
          AND s.thread_id=w.thread_id AND s.channel_id=w.parent_channel_id
          AND s.ma_agent_id=w.agent_id AND s.ma_session_id=w.session_id
          AND s.status='live')) THEN
        RAISE EXCEPTION 'page binding refused' USING ERRCODE='42501';
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
           WHERE watch_id=w.id)+octet_length(p_payload)>131072
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
        -- A tenant row's ceiling overrides the global one; neither passes the hard limits.
        SELECT least(coalesce((SELECT max_turns_per_watch_per_day FROM {schema}.watch_controls
            WHERE scope='tenant:'||w.tenant_id::text),(SELECT max_turns_per_watch_per_day
            FROM {schema}.watch_controls WHERE scope='global'),24),240),
          least(coalesce((SELECT max_turns_per_owner_per_day FROM {schema}.watch_controls
            WHERE scope='tenant:'||w.tenant_id::text),(SELECT max_turns_per_owner_per_day
            FROM {schema}.watch_controls WHERE scope='global'),100),1000)
          INTO watch_cap,owner_cap;
        SELECT count(*) INTO n FROM {schema}.watch_firings WHERE watch_id=w.id
            AND created_at>t-interval '1 day';
        IF n >= least(w.max_turns_per_day,watch_cap) THEN
            RAISE EXCEPTION 'watch daily cap' USING ERRCODE='54000'; END IF;
        SELECT count(*) INTO n FROM {schema}.watch_firings f
          JOIN {schema}.watches v ON v.id=f.watch_id
            WHERE v.owner_account_id=w.owner_account_id AND f.created_at>t-interval '1 day';
        IF n >= owner_cap THEN RAISE EXCEPTION 'owner daily cap' USING ERRCODE='54000'; END IF;
        k := gen_random_uuid();
        INSERT INTO {schema}.task_continuations
          (tenant_id,platform,parent_channel_id,thread_id,requester_account_id,
           requester_external_user_id,target_ma_agent_id,target_name,
           requested_work,reason,idempotency_key,available_at)
        VALUES(w.tenant_id,w.platform,w.parent_channel_id,w.thread_id,w.owner_account_id,
          w.owner_external_id,w.agent_id,w.agent_name,'Registered OS page evidence is ready.',
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
    RETURN k;
END
$body$;
"""

POLL_SQL = """CREATE OR REPLACE FUNCTION {schema}.accept_watch_poll_result(
    p_watch uuid, p_lease text, p_value jsonb, p_payload text
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $body$
DECLARE
    w {schema}.watches%ROWTYPE;
    c record;
    k uuid;
    t timestamptz := clock_timestamp();
    n bigint;
    watch_cap integer;
    owner_cap integer;
    p_event uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(813749201);
    SELECT * INTO w FROM {schema}.watches WHERE id=p_watch FOR UPDATE;
    IF NOT FOUND OR w.source NOT IN ('poll','mcp_poll') OR w.status <> 'active'
       OR (w.expires_at IS NOT NULL AND w.expires_at <= t)
       OR w.lease_owner IS DISTINCT FROM p_lease
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
          AND p.platform=coalesce(w.principal_platform,w.platform)
          AND p.external_id=w.owner_external_id
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
        -- A tenant row's ceiling overrides the global one; neither passes the hard limits.
        SELECT least(coalesce((SELECT max_turns_per_watch_per_day FROM {schema}.watch_controls
            WHERE scope='tenant:'||w.tenant_id::text),(SELECT max_turns_per_watch_per_day
            FROM {schema}.watch_controls WHERE scope='global'),24),240),
          least(coalesce((SELECT max_turns_per_owner_per_day FROM {schema}.watch_controls
            WHERE scope='tenant:'||w.tenant_id::text),(SELECT max_turns_per_owner_per_day
            FROM {schema}.watch_controls WHERE scope='global'),100),1000)
          INTO watch_cap,owner_cap;
        SELECT count(*) INTO n FROM {schema}.watch_firings WHERE watch_id=w.id
            AND created_at>t-interval '1 day';
        IF n >= least(w.max_turns_per_day,watch_cap) THEN
            RAISE EXCEPTION 'watch daily cap' USING ERRCODE='54000'; END IF;
        SELECT count(*) INTO n FROM {schema}.watch_firings f
          JOIN {schema}.watches v ON v.id=f.watch_id
            WHERE v.owner_account_id=w.owner_account_id AND f.created_at>t-interval '1 day';
        IF n >= owner_cap THEN RAISE EXCEPTION 'owner daily cap' USING ERRCODE='54000'; END IF;
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

PAGE_SIGNATURE = "accept_watch_page_event(uuid,uuid,text,bigint,uuid,text,text,text)"
POLL_SIGNATURE = "accept_watch_poll_result(uuid,text,jsonb,text)"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    schema = bind.dialect.identifier_preparer.quote_schema(
        bind.scalar(sa.text("SELECT current_schema()"))
    )
    op.add_column("watches", sa.Column("principal_platform", sa.Text(), nullable=True))
    # A page watch ends with its page, so it may be permanent without an admin.
    op.drop_constraint("ck_watches_permanent", "watches", type_="check")
    op.create_check_constraint(
        "ck_watches_permanent",
        "watches",
        "expires_at IS NOT NULL OR source IN ('callback', 'page_event') OR "
        "(permanent_approved_by IS NOT NULL AND source IN ('poll', 'mcp_poll'))",
    )
    op.drop_constraint("ck_watches_delivery_source", "watches", type_="check")
    op.create_check_constraint(
        "ck_watches_delivery_source",
        "watches",
        "delivery = 'thread' OR source IN ('poll', 'mcp_poll', 'callback') "
        "OR (source = 'page_event' AND delivery = 'new_thread')",
    )
    op.execute(PAGE_SQL.format(schema=schema))
    op.execute(POLL_SQL.format(schema=schema))
    # Restate 0164's ownership and grants, so the audit's approved shape holds.
    op.execute(
        f"REVOKE ALL ON FUNCTION {schema}.{POLL_SIGNATURE} "
        "FROM PUBLIC,daimon_sidecar,daimon_watch_ingress_owner"
    )
    op.execute(f"GRANT CREATE ON SCHEMA {schema} TO daimon_watch_ingress_owner")
    op.execute(f"REVOKE ALL ON FUNCTION {schema}.{PAGE_SIGNATURE} FROM PUBLIC")
    op.execute(f"ALTER FUNCTION {schema}.{PAGE_SIGNATURE} OWNER TO daimon_watch_ingress_owner")
    op.execute(f"GRANT EXECUTE ON FUNCTION {schema}.{PAGE_SIGNATURE} TO daimon_sidecar")
    op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM daimon_watch_ingress_owner")


def _frozen_0164() -> tuple[str, str]:
    """0164's poll and page function bodies, which this revision replaced."""
    path = Path(__file__).with_name("0164_watch_lifetime_caps.py")
    spec = importlib.util.spec_from_file_location("frozen_0164_watch_lifetime_caps", path)
    assert spec is not None and spec.loader is not None, "0164 is a file beside this one"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.POLL_SQL, module.PAGE_SQL


def downgrade() -> None:
    """Back to Discord-only, expiring, thread-delivery page watches; refused while any
    other kind exists, since the old constraints and functions cannot hold them."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    kept = bind.scalar(
        sa.text(
            "SELECT count(*) FROM watches WHERE principal_platform IS NOT NULL OR platform = 'web'"
            " OR (source = 'page_event' AND (expires_at IS NULL OR delivery <> 'thread'))"
        )
    )
    if kept:
        raise NotImplementedError("Web, permanent and new-thread page watches must be retained")
    schema = bind.dialect.identifier_preparer.quote_schema(
        bind.scalar(sa.text("SELECT current_schema()"))
    )
    op.drop_constraint("ck_watches_delivery_source", "watches", type_="check")
    op.create_check_constraint(
        "ck_watches_delivery_source",
        "watches",
        "delivery = 'thread' OR source IN ('poll', 'mcp_poll', 'callback')",
    )
    op.drop_constraint("ck_watches_permanent", "watches", type_="check")
    op.create_check_constraint(
        "ck_watches_permanent",
        "watches",
        "expires_at IS NOT NULL OR source = 'callback' OR "
        "(permanent_approved_by IS NOT NULL AND source IN ('poll', 'mcp_poll'))",
    )
    for sql in _frozen_0164():
        op.execute(sql.format(schema=schema))
    op.execute(
        f"REVOKE ALL ON FUNCTION {schema}.{POLL_SIGNATURE} "
        "FROM PUBLIC,daimon_sidecar,daimon_watch_ingress_owner"
    )
    op.execute(f"GRANT CREATE ON SCHEMA {schema} TO daimon_watch_ingress_owner")
    op.execute(f"REVOKE ALL ON FUNCTION {schema}.{PAGE_SIGNATURE} FROM PUBLIC")
    op.execute(f"ALTER FUNCTION {schema}.{PAGE_SIGNATURE} OWNER TO daimon_watch_ingress_owner")
    op.execute(f"GRANT EXECUTE ON FUNCTION {schema}.{PAGE_SIGNATURE} TO daimon_sidecar")
    op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM daimon_watch_ingress_owner")
    op.drop_column("watches", "principal_platform")
