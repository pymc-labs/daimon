-- Idempotent, run by every apply once 0003_store.sql is in: the grants and view owners a
-- restore with --no-privileges drops, and the views themselves.
revoke all on {{data}}.datasets, {{data}}.metrics from daimon_data_reader;
grant select on {{data}}.datasets, {{data}}.metrics to daimon_data_reader;

-- The view owner reads as the reader does: its column grants, and the same row policies.
grant daimon_data_reader to daimon_data_view_owner;

revoke all on function {{data}}.protect_view(regclass, text[]) from public;
revoke all on function {{data}}.caller_sql(text, int) from public;
grant execute on function {{data}}.caller_sql(text, int) to daimon_data_reader;

-- A view's row policy calls these as the role reading the view, so caller SQL needs them. Each
-- returns only what this transaction's read context allows, and the role has no USAGE on the
-- schema, so the text cannot name them.
grant execute on function {{data}}.visible_sources(), {{data}}.scopes(),
  {{data}}.visible_acls(), {{data}}.lapsed_record_ids() to daimon_data_sql;

-- caller_sql runs as its owner: the role that reaches only the views. A new owner needs
-- CREATE on the schema for the change, and keeps nothing of it.
do $$
begin
  if (select pg_get_userbyid(proowner) from pg_proc
      where oid = '{{data}}.caller_sql(text, int)'::regprocedure) <> 'daimon_data_sql' then
    grant create on schema {{data}} to daimon_data_sql;
    alter function {{data}}.caller_sql(text, int) owner to daimon_data_sql;
    revoke create on schema {{data}} from daimon_data_sql;
  end if;
end $$;

-- What caller SQL may select from. Access columns, the search vector and the connector's own
-- payload (which ingest redaction never touches) stay out.
select {{data}}.protect_view('{{data}}.sources');
select {{data}}.protect_view('{{data}}.records', array['acl', 'tsv', 'payload']);
select {{data}}.protect_view('{{data}}.datasets');
select {{data}}.protect_view('{{data}}.metrics');
