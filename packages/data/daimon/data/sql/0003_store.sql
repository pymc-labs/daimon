-- Search and read support: the catalog of datasets and metrics a source offers, the helper
-- that publishes a table as a read view, and the one entry point for a caller's SQL.
-- {{data}} is the store's schema, {{views}} its read views.

-- A dataset: a table of a source that caller SQL may read, under the name of its view.
-- columns: [{name, type, description}].
create table {{data}}.datasets (
  source_id uuid not null references {{data}}.sources on delete cascade,
  name text not null,
  title text not null default '',
  description text not null default '',
  columns jsonb not null default '[]',
  scope text not null,
  primary key (source_id, name)
);

-- A named aggregate over a dataset, in SQL (`sum(hours)`).
create table {{data}}.metrics (
  source_id uuid not null,
  dataset text not null,
  name text not null,
  description text not null default '',
  expr text not null,
  primary key (source_id, name),
  foreign key (source_id, dataset) references {{data}}.datasets on delete cascade
);

alter table {{data}}.datasets enable row level security;
create policy visible on {{data}}.datasets for select using (
  source_id in (select {{data}}.visible_sources()) and scope = any ((select {{data}}.scopes())::text[]));

alter table {{data}}.metrics enable row level security;
create policy visible on {{data}}.metrics for select using (
  exists (select 1 from {{data}}.datasets d where d.source_id = metrics.source_id and d.name = metrics.dataset));

-- Publishes a table as the view {{views}}.<its name>: the columns the reader may read, less
-- `hidden`. The view is owned by the view owner, who reads as the reader does (so the table's
-- own row policy applies), and is the only thing caller SQL can select from.
create function {{data}}.protect_view(t regclass, hidden text[] default '{}') returns void
  language plpgsql set search_path = pg_catalog, pg_temp as $$
declare nm text := (select relname from pg_class where oid = t);
        shown text;
begin
  select string_agg(quote_ident(a.attname), ', ' order by a.attnum) into shown
  from pg_attribute a
  where a.attrelid = t and a.attnum > 0 and not a.attisdropped and a.attname <> all (hidden)
    and has_column_privilege('daimon_data_reader', t, a.attnum, 'select');
  if shown is null then
    raise exception 'the reader role reads no column of %', t;
  end if;
  execute format('drop view if exists {{views}}.%I', nm);
  execute format('create view {{views}}.%I with (security_barrier = true) as select %s from %s', nm, shown, t);
  execute format('alter view {{views}}.%I owner to daimon_data_view_owner', nm);
  execute format('revoke all on {{views}}.%I from public', nm);
  execute format('grant select on {{views}}.%I to daimon_data_sql', nm);
end $$;

-- Caller SQL, as a subquery of one statement, run as daimon_data_sql: the owner of this
-- function, which Postgres keeps for its whole body (no SET ROLE or RESET ROLE can leave it),
-- and which reaches the views and nothing else of the store. The loop, not the text, holds
-- the limit, so a comment in `q` cannot cut it off.
create function {{data}}.caller_sql(q text, n int) returns setof json
  language plpgsql security definer set search_path = pg_catalog, {{views}}, pg_temp as $$
declare r json;
        i int := 0;
begin
  if current_user <> 'daimon_data_sql' then
    raise exception 'caller_sql must run as daimon_data_sql';
  end if;
  for r in execute format(E'select row_to_json(t) from (%s\n) t', q) loop
    exit when i >= n;
    i := i + 1;
    return next r;
  end loop;
end $$;
