"""Callback watches, held timers and DM carry-overs: what an operator import brings in.

`accept_watch_callback(watch, token, payload, max_body)` is the one function the sidecar
may call for a bearer-token post. It compares sha256(token) with every live
`watch_capabilities` row of operation `callback` in constant time (no early exit),
refuses every failure the same way (42501), and then applies the same switches,
rate limits, byte quotas and daily caps as the page and poll paths, under the same
global lock. The body is the event as is; it is evidence, never an instruction. The
same body to the same watch within five minutes is one event: a sender's retry gets
the first post's wake and adds nothing.
Only the hash is stored. `max_body` is the deployment's body cap in bytes, kept between
4 KiB and 512 KiB here whatever the caller says; the byte quotas grow with it.

`watches.delivery` other than `thread` is now allowed for the `callback` source too, and
a callback watch may be permanent (`callback_watch_lifetime` set to `0`);
its links then have no expiry either.

`held_timers` keeps imported one-shot timers until an operator confirms them, and
`dm_carry_overs` an earlier private conversation until the person's first DM here.
Settling or taking one empties its words and keeps the row, so a repeated import adds
nothing.

downgrade: destructive
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0206_callback_watch_imports"
down_revision: str | None = "0205_web_search_usage"
branch_labels = None
depends_on = None

SIGNATURE = "accept_watch_callback(uuid,text,text,integer)"

CALLBACK_SQL = """
CREATE OR REPLACE FUNCTION {schema}.accept_watch_callback(
    p_watch uuid, p_token text, p_payload text, p_max_body integer
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $body$
DECLARE
    w {schema}.watches%ROWTYPE;
    c record;
    k uuid;
    fresh uuid := gen_random_uuid();
    t timestamptz := clock_timestamp();
    n bigint;
    watch_cap integer;
    owner_cap integer;
    supplied bytea;
    stored bytea;
    difference integer;
    i integer;
    matched boolean := false;
    found_watch boolean;
    m integer := least(greatest(coalesce(p_max_body, 4096), 4096), 524288);
BEGIN
    IF NOT coalesce((SELECT enabled FROM {schema}.watch_controls WHERE scope='global'),false)
       THEN RAISE EXCEPTION 'callback refused' USING ERRCODE='42501'; END IF;
    IF p_watch IS NULL OR p_payload IS NULL
       OR octet_length(p_payload) NOT BETWEEN 1 AND m
       OR p_token IS NULL OR p_token !~ '^[A-Za-z0-9_-]{{16,128}}$' THEN
        RAISE EXCEPTION 'callback refused' USING ERRCODE='42501';
    END IF;
    -- A wrong token is refused before the global lock, so guessing cannot queue on it.
    -- Every live digest is compared in full; a match never ends the scan early.
    supplied := public.digest(convert_to(p_token,'UTF8'),'sha256');
    FOR c IN SELECT digest FROM {schema}.watch_capabilities
      WHERE watch_id=p_watch AND operation='callback' AND revoked_at IS NULL
        AND (expires_at IS NULL OR expires_at > clock_timestamp()) AND digest ~ '^[0-9a-f]{{64}}$'
    LOOP
        stored := decode(c.digest,'hex');
        difference := 0;
        FOR i IN 0..31 LOOP
            difference := difference | (get_byte(stored,i) # get_byte(supplied,i));
        END LOOP;
        IF difference = 0 THEN matched := true; END IF;
    END LOOP;
    IF NOT matched THEN RAISE EXCEPTION 'callback refused' USING ERRCODE='42501'; END IF;
    matched := false;
    -- One global source lock makes rate/byte/row quotas exact across replicas.
    PERFORM pg_advisory_xact_lock(813749201);
    IF NOT coalesce((SELECT enabled FROM {schema}.watch_controls WHERE scope='global'),false)
       THEN RAISE EXCEPTION 'callback refused' USING ERRCODE='42501'; END IF;
    SELECT * INTO w FROM {schema}.watches WHERE id=p_watch FOR UPDATE;
    found_watch := FOUND;
    t := clock_timestamp();
    supplied := public.digest(convert_to(p_token,'UTF8'),'sha256');
    -- Every live digest is compared in full; a match never ends the scan early.
    FOR c IN SELECT digest FROM {schema}.watch_capabilities
      WHERE watch_id=p_watch AND operation='callback' AND revoked_at IS NULL
        AND (expires_at IS NULL OR expires_at > t) AND digest ~ '^[0-9a-f]{{64}}$'
    LOOP
        stored := decode(c.digest,'hex');
        difference := 0;
        FOR i IN 0..31 LOOP
            difference := difference | (get_byte(stored,i) # get_byte(supplied,i));
        END LOOP;
        IF difference = 0 THEN matched := true; END IF;
    END LOOP;
    -- Only a sender holding a live token learns the watch is waiting: retry later.
    IF found_watch AND matched AND w.source = 'callback' AND w.status = 'paused'
       AND w.source_state->>'reason' = 'awaiting_confirmation'
       AND (w.expires_at IS NULL OR w.expires_at > t) THEN
        RAISE EXCEPTION 'callback held' USING ERRCODE='55000';
    END IF;
    IF NOT found_watch OR NOT matched OR w.source <> 'callback'
       OR w.status <> 'active' OR (w.expires_at IS NOT NULL AND w.expires_at <= t)
       OR (w.max_fires IS NOT NULL AND w.fire_count >= w.max_fires)
       OR EXISTS (SELECT 1 FROM {schema}.watch_controls WHERE NOT enabled AND
          scope IN ('tenant:'||w.tenant_id::text,'owner:'||w.owner_account_id::text))
       OR NOT EXISTS (SELECT 1 FROM {schema}.accounts a JOIN {schema}.tenants g
          ON g.id=a.tenant_id WHERE a.id=w.owner_account_id AND a.tenant_id=w.tenant_id
          AND g.archived_at IS NULL AND g.provision_status='ready')
       OR NOT EXISTS (SELECT 1 FROM {schema}.platform_principals p WHERE p.tenant_id=w.tenant_id
          AND p.platform=w.platform AND p.external_id=w.owner_external_id
          AND p.account_id=w.owner_account_id)
       OR (w.delivery='thread' AND NOT EXISTS (SELECT 1 FROM {schema}.thread_sessions s
          WHERE s.tenant_id=w.tenant_id AND s.account_id=w.owner_account_id
          AND s.platform=w.platform AND s.thread_id=w.thread_id
          AND s.channel_id=w.parent_channel_id AND s.ma_agent_id=w.agent_id
          AND s.ma_session_id=w.session_id AND s.status='live')) THEN
        RAISE EXCEPTION 'callback refused' USING ERRCODE='42501';
    END IF;
    -- A retry of a body held in the last five minutes is accepted again, adding nothing.
    SELECT i.wake_key INTO k FROM {schema}.watch_inbox i
      WHERE i.watch_id=w.id AND i.accepted_at > t-interval '5 minutes' AND i.payload=p_payload
      ORDER BY i.accepted_at DESC LIMIT 1;
    IF FOUND THEN RETURN k; END IF;
    -- Rows and payloads expire after seven days.
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
           WHERE watch_id=w.id)+octet_length(p_payload)>greatest(131072,4*m)
       OR (SELECT coalesce(sum(octet_length(e.payload)),0) FROM {schema}.watch_inbox e
           JOIN {schema}.watches v ON v.id=e.watch_id WHERE v.owner_account_id=w.owner_account_id)
           +octet_length(p_payload)>greatest(1048576,16*m)
       OR (SELECT coalesce(sum(octet_length(e.payload)),0) FROM {schema}.watch_inbox e
           JOIN {schema}.watches v ON v.id=e.watch_id WHERE v.tenant_id=w.tenant_id)
           +octet_length(p_payload)>greatest(4194304,64*m)
       OR (SELECT coalesce(sum(octet_length(payload)),0) FROM {schema}.watch_inbox)
           +octet_length(p_payload)>greatest(16777216,256*m) THEN
        RAISE EXCEPTION 'evidence quota' USING ERRCODE='54000';
    END IF;
    -- New evidence must not join a batch whose reader retention already expired.
    SELECT f.wake_key INTO k FROM {schema}.watch_firings f JOIN {schema}.task_continuations q
      ON q.idempotency_key=f.wake_key WHERE f.watch_id=w.id AND f.revision=w.revision
      AND f.generation=0 AND f.created_at>t-interval '7 days'
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
          w.owner_external_id,w.agent_id,w.agent_name,'Callback evidence is ready.',
          CASE WHEN w.delivery='thread' THEN 'watch_fired' ELSE 'watch_report' END,k,t);
        INSERT INTO {schema}.watch_firings(wake_key,watch_id,revision,generation,created_at)
          VALUES(k,w.id,w.revision,0,t);
    END IF;
    INSERT INTO {schema}.watch_inbox(watch_id,producer,event_id,payload,wake_key,accepted_at)
      VALUES(w.id,w.producer,fresh::text,p_payload,k,t);
    UPDATE {schema}.watch_firings SET
      events=(CASE WHEN jsonb_array_length(events)>=20 THEN events-0 ELSE events END)
        ||jsonb_build_array(jsonb_build_object('event_id',fresh::text,'text',p_payload)),
      omitted=omitted+CASE WHEN jsonb_array_length(events)>=20 THEN 1 ELSE 0 END
      WHERE wake_key=k;
    UPDATE {schema}.watches SET fire_count=fire_count+1 WHERE id=w.id;
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
    op.alter_column("watch_capabilities", "expires_at", nullable=True)
    op.execute(CALLBACK_SQL.format(schema=schema))
    op.execute(f"GRANT CREATE ON SCHEMA {schema} TO daimon_watch_ingress_owner")
    op.execute(f"REVOKE ALL ON FUNCTION {schema}.{SIGNATURE} FROM PUBLIC")
    op.execute(f"ALTER FUNCTION {schema}.{SIGNATURE} OWNER TO daimon_watch_ingress_owner")
    op.execute(f"GRANT EXECUTE ON FUNCTION {schema}.{SIGNATURE} TO daimon_sidecar")
    op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM daimon_watch_ingress_owner")
    op.create_table(
        "held_timers",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_user_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=True),
        sa.Column("place_type", sa.Text(), nullable=False),
        sa.Column("fire_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column(
            "imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("paused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "source_id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "(settled_at IS NULL) = (outcome IS NULL)", name="ck_held_timers_settled"
        ),
    )
    op.create_index("ix_held_timers_account", "held_timers", ["account_id"])
    op.create_table(
        "dm_carry_overs",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("external_user_id", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("exported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("history", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("context", sa.Text(), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "platform", "external_user_id", name="pk_dm_carry_overs"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE", name="fk_dm_carry_overs_tenants"
        ),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    schema = bind.dialect.identifier_preparer.quote_schema(
        bind.scalar(sa.text("SELECT current_schema()"))
    )
    op.execute(f"DROP FUNCTION IF EXISTS {schema}.{SIGNATURE}")
    op.drop_table("dm_carry_overs")
    op.drop_table("held_timers")
    # A callback watch has no meaning without its port; its capabilities cascade.
    op.execute("DELETE FROM watches WHERE source = 'callback'")
    op.drop_constraint("ck_watches_delivery_source", "watches", type_="check")
    op.create_check_constraint(
        "ck_watches_delivery_source",
        "watches",
        "delivery = 'thread' OR source IN ('poll', 'mcp_poll')",
    )
    op.drop_constraint("ck_watches_permanent", "watches", type_="check")
    op.create_check_constraint(
        "ck_watches_permanent",
        "watches",
        "expires_at IS NOT NULL OR "
        "(permanent_approved_by IS NOT NULL AND source IN ('poll', 'mcp_poll'))",
    )
    op.alter_column("watch_capabilities", "expires_at", nullable=False)
