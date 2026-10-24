"""OS-signed page-event watches and a constrained transactional ingress.

downgrade: unsupported
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0119_watches"
down_revision = "0118_lead_intake"
branch_labels = None
depends_on = None

# Keep this body frozen: migrations must not change when application code changes.
# The schema is fixed at installation, never supplied by the invoking identity.
ACCEPT_SQL = """
CREATE FUNCTION {schema}.accept_watch_page_event(
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
       OR w.status <> 'active' OR w.expires_at <= t
       OR EXISTS (SELECT 1 FROM {schema}.watch_controls WHERE NOT enabled AND
          scope IN ('tenant:'||w.tenant_id::text,'owner:'||w.owner_account_id::text)) THEN
        RAISE EXCEPTION 'page registration refused' USING ERRCODE='42501';
    END IF;
    SELECT * INTO c FROM {schema}.watch_capabilities WHERE watch_id=w.id
      AND key_id=p_key_id AND operation='page_event' AND revoked_at IS NULL
      AND expires_at > t AND digest=encode(public.digest(convert_to(p_key,'UTF8'),'sha256'),'hex')
      ORDER BY generation DESC LIMIT 1;
    IF NOT FOUND THEN RAISE EXCEPTION 'page key refused' USING ERRCODE='42501'; END IF;
    IF EXISTS (SELECT 1 FROM {schema}.watch_inbox
      WHERE producer=w.producer AND event_id=p_event::text) THEN
        RAISE EXCEPTION 'page event replay' USING ERRCODE='23505';
    END IF;
    IF (w.max_fires IS NOT NULL AND w.fire_count >= w.max_fires) OR
       NOT EXISTS (SELECT 1 FROM {schema}.accounts a JOIN {schema}.tenants g
          ON g.id=a.tenant_id WHERE a.id=w.owner_account_id AND a.tenant_id=w.tenant_id
          AND g.archived_at IS NULL AND g.provision_status='ready') OR
       NOT EXISTS (SELECT 1 FROM {schema}.platform_principals p WHERE p.tenant_id=w.tenant_id
          AND p.platform=w.platform AND p.external_id=w.owner_external_id
          AND p.account_id=w.owner_account_id) OR
       NOT EXISTS (SELECT 1 FROM {schema}.thread_sessions s WHERE s.tenant_id=w.tenant_id
          AND s.account_id=w.owner_account_id AND s.platform=w.platform
          AND s.thread_id=w.thread_id AND s.channel_id=w.parent_channel_id
          AND s.ma_agent_id=w.agent_id AND s.ma_session_id=w.session_id AND s.status='live') THEN
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
          w.owner_external_id,w.agent_id,w.agent_name,'Registered OS page evidence is ready.',
          'watch_fired',k,t);
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

PRUNE_SQL = """
CREATE FUNCTION {schema}.prune_watch_evidence() RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $body$
    DELETE FROM {schema}.watch_inbox WHERE accepted_at<clock_timestamp()-interval '7 days';
    UPDATE {schema}.watch_firings SET events='[]'::jsonb
      WHERE created_at<clock_timestamp()-interval '7 days' AND events<>'[]'::jsonb;
$body$;
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # These declarations intentionally mirror only this revision's schema.
    op.create_table(
        "watches",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id", sa.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "owner_account_id",
            sa.UUID(),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        *(
            sa.Column(n, sa.Text(), nullable=False)
            for n in (
                "owner_external_id",
                "platform",
                "parent_channel_id",
                "thread_id",
                "agent_id",
                "agent_name",
                "session_id",
                "instructions",
                "producer",
            )
        ),
        sa.Column("page_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source", sa.Text(), nullable=False, server_default="page_event"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        *(
            sa.Column(n, JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb"))
            for n in ("baseline", "rule", "source_state")
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.Text()),
        *(
            sa.Column(n, sa.DateTime(timezone=True))
            for n in ("lease_expires_at", "next_check_at", "backoff_until", "rate_window_at")
        ),
        *(
            sa.Column(n, sa.Integer(), nullable=False, server_default="0")
            for n in ("consecutive_errors", "fire_count", "rate_count")
        ),
        sa.Column("max_turns_per_day", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("max_fires", sa.Integer()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("producer"),
    )
    op.create_table(
        "watch_capabilities",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "watch_id", sa.UUID(), sa.ForeignKey("watches.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "watch_firings",
        sa.Column(
            "wake_key",
            sa.UUID(),
            sa.ForeignKey("task_continuations.idempotency_key", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "watch_id", sa.UUID(), sa.ForeignKey("watches.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("events", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("omitted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_watch_firings_watch_id", "watch_firings", ["watch_id"])
    op.create_table(
        "watch_inbox",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "watch_id", sa.UUID(), sa.ForeignKey("watches.id", ondelete="CASCADE"), nullable=False
        ),
        *(sa.Column(n, sa.Text(), nullable=False) for n in ("producer", "event_id", "payload")),
        sa.Column(
            "wake_key",
            sa.UUID(),
            sa.ForeignKey("watch_firings.wake_key", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "accepted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("producer", "event_id", name="uq_watch_inbox_event"),
    )
    op.drop_constraint("ck_task_continuations_reason", "task_continuations", type_="check")
    op.create_check_constraint(
        "ck_task_continuations_reason",
        "task_continuations",
        "reason IN ('task_handoff', 'private_input_applied', 'timer', "
        "'github_access_ready', 'worker_finished', 'watch_fired')",
    )
    op.create_table(
        "watch_controls",
        sa.Column("scope", sa.Text(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute("INSERT INTO watch_controls(scope,enabled) VALUES('global',false)")
    op.create_index("watch_inbox_accepted_idx", "watch_inbox", ["accepted_at"])
    op.create_index("watch_inbox_watch_idx", "watch_inbox", ["watch_id"])
    bind = op.get_bind()
    schema = bind.dialect.identifier_preparer.quote_schema(
        bind.scalar(sa.text("SELECT current_schema()"))
    )
    # Existing roles are accepted only if already safe; never silently inherit a
    # legacy application's authority or make a live login privileged by accident.
    for role in ("daimon_sidecar", "daimon_watch_ingress_owner"):
        op.execute(
            sa.text(
                """DO $audit$ BEGIN
          IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='"""
                + role
                + """') THEN
            CREATE ROLE """
                + role
                + """ NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
              NOREPLICATION NOBYPASSRLS;
          END IF;
          IF EXISTS(SELECT 1 FROM pg_roles r WHERE r.rolname='"""
                + role
                + """' AND
             (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication
               OR r.rolbypassrls OR r.rolcanlogin))
             OR EXISTS(SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member
                WHERE r.rolname='"""
                + role
                + """') THEN
            RAISE EXCEPTION 'unsafe existing watch database role';
          END IF;
        END $audit$;"""
            )
        )
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public")
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM PUBLIC,daimon_sidecar")
    op.execute(
        f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} "
        "FROM daimon_sidecar,daimon_watch_ingress_owner"
    )
    op.execute(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {schema} FROM daimon_sidecar")
    op.execute(f"GRANT USAGE ON SCHEMA {schema},public TO daimon_watch_ingress_owner")
    for table in (
        "watches",
        "watch_capabilities",
        "watch_controls",
        "accounts",
        "tenants",
        "platform_principals",
        "thread_sessions",
    ):
        op.execute(f"GRANT SELECT ON {schema}.{table} TO daimon_watch_ingress_owner")
    op.execute(f"GRANT UPDATE(fire_count) ON {schema}.watches TO daimon_watch_ingress_owner")
    for table in ("watch_inbox", "watch_firings"):
        op.execute(
            f"GRANT SELECT,INSERT,UPDATE,DELETE ON {schema}.{table} TO daimon_watch_ingress_owner"
        )
    op.execute(
        f"GRANT SELECT,INSERT,UPDATE(status) ON {schema}.task_continuations "
        "TO daimon_watch_ingress_owner"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.digest(bytea,text),public.hmac(bytea,bytea,text) "
        "TO daimon_watch_ingress_owner"
    )
    op.execute(ACCEPT_SQL.format(schema=schema))
    op.execute(PRUNE_SQL.format(schema=schema))
    op.execute(f"GRANT CREATE ON SCHEMA {schema} TO daimon_watch_ingress_owner")
    for signature in (
        "accept_watch_page_event(uuid,uuid,text,bigint,uuid,text,text,text)",
        "prune_watch_evidence()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {schema}.{signature} FROM PUBLIC")
        op.execute(f"ALTER FUNCTION {schema}.{signature} OWNER TO daimon_watch_ingress_owner")
        op.execute(f"GRANT EXECUTE ON FUNCTION {schema}.{signature} TO daimon_sidecar")
    op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM daimon_watch_ingress_owner")
    op.execute(f"GRANT USAGE ON SCHEMA {schema} TO daimon_sidecar")


def downgrade() -> None:
    raise NotImplementedError("Watch evidence and delivery history must be retained")
