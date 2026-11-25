-- Revision 0242's downgrade: drop what 0005_ops.sql and its grants made and put back 0001's
-- visible_sources, so a later upgrade makes it again.
drop table if exists {{data}}.declared_state, {{data}}.declared_bindings, {{data}}.parity_runs;
drop function if exists {{data}}.parity_visible(uuid, uuid),
  {{data}}.acls_for(jsonb, boolean, int[], uuid[], uuid);
drop trigger if exists generation on {{data}}.sources;
drop trigger if exists guard_bound on {{data}}.sources;
drop trigger if exists guard_bound on {{data}}.scopes;
drop trigger if exists guard_bound on {{data}}.grants;
drop trigger if exists publish_guard on {{data}}.sources;
drop function if exists {{data}}.source_generation(), {{data}}.guard_bound(), {{data}}.publish_guard();
create or replace function {{data}}.visible_sources() returns setof uuid language sql stable security definer rows 10
  set search_path = {{data}}, pg_catalog, pg_temp as $$
  select s.id from sources s join read_contexts c on s.tenant_id = any (c.tenant_ids)
  where c.pid = pg_backend_pid() and c.xact = pg_current_xact_id_if_assigned()
    and s.publication = 'published' and s.status <> 'disabled'
$$;
drop function if exists {{data}}.source_fresh(uuid), {{data}}.source_lifetime(int, int, jsonb);
alter table {{data}}.sources drop column if exists mode, drop column if exists fresh_floor_minutes,
  drop column if exists fresh_ceiling_minutes, drop column if exists generation,
  drop column if exists bound, drop column if exists declared_key;
alter table {{data}}.scopes drop column if exists bound;
alter table {{data}}.grants drop column if exists bound;
delete from {{data}}.schema_version where file = '0005_ops.sql';
