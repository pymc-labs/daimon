-- Revision 0243's downgrade: drop what 0006_redaction.sql made and put back 0005's trigger functions.
drop table if exists {{data}}.redaction_sets;
create or replace function {{data}}.source_generation() returns trigger language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
begin
  if (new.kind, new.provider, new.config, new.credential, new.default_scope, new.mode)
     is distinct from (old.kind, old.provider, old.config, old.credential, old.default_scope, old.mode) then
    new.generation := old.generation + 1;
  end if;
  return new;
end $$;
create or replace function {{data}}.guard_bound() returns trigger language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
begin
  if not old.bound or coalesce(current_setting('daimon_data.declared', true), '') = 'on' then
    return new;
  end if;
  if tg_table_name = 'sources' then
    if (new.tenant_id, new.kind, new.name, new.provider, new.config, new.credential, new.default_scope,
        new.mode, new.fresh_floor_minutes, new.fresh_ceiling_minutes, new.bound, new.declared_key)
       is distinct from
       (old.tenant_id, old.kind, old.name, old.provider, old.config, old.credential, old.default_scope,
        old.mode, old.fresh_floor_minutes, old.fresh_ceiling_minutes, old.bound, old.declared_key)
       or (new.status = 'disabled' and old.status <> 'disabled') or old.publication = 'retired'
          and new.publication <> 'retired' then
      raise exception 'source % is managed by the deployment', old.id using errcode = '42501';
    end if;
  elsif to_jsonb(new) - 'created_at' is distinct from to_jsonb(old) - 'created_at' then
    raise exception 'a % row is managed by the deployment', tg_table_name using errcode = '42501';
  end if;
  return new;
end $$;
alter table {{data}}.sources drop column if exists redaction_set;
alter table {{data}}.identities drop column if exists redacted;
delete from {{data}}.schema_version where file = '0006_redaction.sql';
