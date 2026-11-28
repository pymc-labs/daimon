-- Idempotent setup every apply and repair runs first. {{data}} and {{views}} are filled in.
create schema if not exists {{data}};
create schema if not exists {{views}};

-- Roles are cluster-global. A creation that races another database's is fine.
do $$
declare r text;
begin
  foreach r in array array['daimon_data_reader', 'daimon_data_sql', 'daimon_data_view_owner', 'daimon_data_parity']
  loop
    if not exists (select from pg_roles where rolname = r) then
      begin
        execute format('create role %I nologin', r);
      exception when duplicate_object then null;
      end;
    end if;
    -- The applying role must hold them to own and grant on their behalf.
    begin
      execute format('grant %I to current_user', r);
    exception when others then
      raise notice 'data store: cannot grant role % to %: %', r, current_user, sqlerrm;
    end;
  end loop;
end $$;

grant usage on schema {{data}} to daimon_data_reader, daimon_data_view_owner, daimon_data_parity;
grant usage on schema {{views}} to daimon_data_reader, daimon_data_sql, daimon_data_view_owner, daimon_data_parity;
grant create on schema {{views}} to daimon_data_view_owner;

create table if not exists {{data}}.schema_version (
  file text primary key,
  applied_at timestamptz not null default now()
);

-- Optional: search features use whichever of these this server can offer. intarray overloads
-- `int4[]` operators, so it goes in the store's schema, off every other search_path.
do $$
declare e text;
begin
  foreach e in array array['vector', 'intarray', 'pg_textsearch']
  loop
    begin
      execute format('create extension if not exists %I', e)
        || case when e = 'intarray' then ' schema {{data}}' else '' end;
    exception when others then
      raise notice 'data store: extension % unavailable: %', e, sqlerrm;
    end;
  end loop;
end $$;
