-- Operations: how stale a source may get, sources read live, the parity evidence a source is
-- published on, and what a deployment's declared file owns. {{data}} is the store's schema.
--
-- Everything here is inert until used: a source has no freshness limit, is stored, and is not
-- bound unless someone sets it.

-- ---- sources: freshness, mode, generation and the declared file's mark ------------------------

alter table {{data}}.sources
  add column mode text not null default 'stored' check (mode in ('stored', 'federated')),
  -- A source is hidden once its last complete sync is older than its lifetime: three sync
  -- intervals, at least the floor and at most the ceiling. No ceiling: never hidden.
  add column fresh_floor_minutes int not null default 60 check (fresh_floor_minutes >= 0),
  add column fresh_ceiling_minutes int check (fresh_ceiling_minutes > 0),
  -- Moves whenever what the source reads from, or as, changes: older parity runs prove nothing.
  add column generation int not null default 1,
  add column bound boolean not null default false,   -- owned by the declared file
  add column declared_key text;                      -- its id in that file
create unique index on {{data}}.sources (tenant_id, declared_key) where declared_key is not null;

alter table {{data}}.scopes add column bound boolean not null default false;
alter table {{data}}.grants add column bound boolean not null default false;

create function {{data}}.source_lifetime(floor_minutes int, ceiling_minutes int, cfg jsonb)
  returns interval language sql immutable set search_path = {{data}}, pg_catalog, pg_temp as $$
  select make_interval(mins => least(ceiling_minutes, greatest(floor_minutes, 3 *
    case when cfg ->> 'sync_minutes' ~ '^[0-9]{1,6}$' then (cfg ->> 'sync_minutes')::int else 60 end)))
$$;

-- Whether a source may be read now. A live source stores nothing and is always fresh.
create function {{data}}.source_fresh(src uuid) returns boolean language sql stable
  set search_path = {{data}}, pg_catalog, pg_temp as $$
  select s.fresh_ceiling_minutes is null or s.mode = 'federated' or exists (
    select 1 from source_cursors k where k.source_id = s.id and k.last_sync_at >
      statement_timestamp() - source_lifetime(s.fresh_floor_minutes, s.fresh_ceiling_minutes, s.config))
  from sources s where s.id = src
$$;

-- 0001's, with the freshness rule.
create or replace function {{data}}.visible_sources() returns setof uuid language sql stable security definer rows 10
  set search_path = {{data}}, pg_catalog, pg_temp as $$
  select s.id from sources s join read_contexts c on s.tenant_id = any (c.tenant_ids)
  where c.pid = pg_backend_pid() and c.xact = pg_current_xact_id_if_assigned()
    and s.publication = 'published' and s.status <> 'disabled' and source_fresh(s.id)
$$;

create function {{data}}.source_generation() returns trigger language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
begin
  if (new.kind, new.provider, new.config, new.credential, new.default_scope, new.mode)
     is distinct from (old.kind, old.provider, old.config, old.credential, old.default_scope, old.mode) then
    new.generation := old.generation + 1;
  end if;
  return new;
end $$;
create trigger generation before update on {{data}}.sources
  for each row execute function {{data}}.source_generation();

-- ---- the declared file -----------------------------------------------------------------------

create table {{data}}.declared_state (
  id int primary key default 1 check (id = 1),
  revision int not null,
  applied_at timestamptz not null default now(),
  unknown text[] not null default '{}'          -- sections no handler took, kept for the log
);

-- What the file last said of a source; credentials map each field to a fingerprint, never a value.
create table {{data}}.declared_bindings (
  source_id uuid primary key references {{data}}.sources on delete cascade,
  revision int not null,
  publish boolean not null default false,
  replaces text[] not null default '{}',
  credentials jsonb not null default '{}'
);

-- Bound rows change only inside the declared apply (it sets daimon_data.declared for its
-- transaction). A guard against slips through the app, not a boundary: deletes are left open
-- so erasure and tenant purge always work. A retired bound source never reopens.
create function {{data}}.guard_bound() returns trigger language plpgsql
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
create trigger guard_bound before update on {{data}}.sources
  for each row execute function {{data}}.guard_bound();
create trigger guard_bound before update on {{data}}.scopes
  for each row execute function {{data}}.guard_bound();
create trigger guard_bound before update on {{data}}.grants
  for each row execute function {{data}}.guard_bound();

-- ---- parity: Daimon's side of a comparison with the source itself -------------------------------

-- One comparison of one source: people compared, records checked, and what disagreed. wrong_more:
-- shown here, hidden by the source (blocks publishing). wrong_less: hidden here, shown by the
-- source (a warning). unresolved: why there is no verdict (counts only).
create table {{data}}.parity_runs (
  id uuid primary key default gen_random_uuid(),
  source_id uuid not null references {{data}}.sources on delete cascade,
  generation int not null,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  people int not null default 0,
  checked int not null default 0,
  wrong_more int not null default 0,
  wrong_less int not null default 0,
  unresolved text,
  ok boolean not null default false
);
create index on {{data}}.parity_runs (source_id, started_at desc);

-- A source never goes from shadow to published while its newest run at its current generation
-- found it showing more than the source does. The full gate (a clean, recent run) is
-- daimon.data.parity.publish.
create function {{data}}.publish_guard() returns trigger language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
begin
  if new.publication = 'published' and old.publication <> 'published' and exists (
      select 1 from (select wrong_more from parity_runs where source_id = new.id
                     and generation = new.generation order by started_at desc, id desc limit 1) r
      where r.wrong_more > 0) then
    raise exception 'source % shows people more than its source does: not published', new.id
      using errcode = '42501';
  end if;
  return new;
end $$;
create trigger publish_guard before update of publication on {{data}}.sources
  for each row execute function {{data}}.publish_guard();

-- 0004's acls_for with one source counted as published, so a source still in shadow can be
-- compared; the four-argument form every reader uses is unchanged.
create function {{data}}.acls_for(readers jsonb, personal_ok boolean, limits int[], tenant_ids uuid[], parity_source uuid)
  returns table (acls int[], units int[], skipped int[], fraction float8) language plpgsql stable
  set search_path = {{data}}, pg_catalog, pg_temp as $$
declare
  h record;
  last_ord int;
  reader_acls int[] := '{}';
  reader_holders int := 0;
  visible int[];
  used int[] := '{}';
  skip int[] := '{}';
  expired int[];
  live int[];
  holder_acls int[];
  holder_units int[];
  v_acls int[];
  v_fraction float8;
begin
  if jsonb_typeof(readers) <> 'array' or jsonb_array_length(readers) not between 1 and 200
     or (select count(distinct ord) from reader_subjects(readers, tenant_ids)) <> jsonb_array_length(readers) then
    return query select '{}'::int[], '{}'::int[], '{}'::int[], 0::float8;
    return;
  end if;
  expired := array(select id from evidence_units where expires_at <= statement_timestamp());
  live := array(select id from evidence_units where expires_at > statement_timestamp());
  for h in select s.ord, s.holder, coalesce(hs.acls, '{}') as acls, coalesce(hs.units, '{}') as units
           from reader_subjects(readers, tenant_ids) s left join holder_sets hs on hs.holder = s.holder
           order by s.ord, s.holder loop
    if last_ord is not null and h.ord <> last_ord then
      if reader_holders > 1 then
        reader_acls := arr_sort(reader_acls);
      end if;
      visible := case when visible is null then reader_acls else arr_and(visible, reader_acls) end;
      reader_acls := '{}';
      reader_holders := 0;
    end if;
    last_ord := h.ord;
    holder_acls := h.acls;
    holder_units := h.units;
    if not (h.units <@ live) then    -- an expired or missing unit: this holder's live grants only
      skip := skip || arr_and(h.units, expired);
      select coalesce(array_agg(distinct g.acl_id order by g.acl_id), '{}'),
             coalesce(array_agg(distinct g.unit_id) filter (where g.unit_id is not null), '{}')
        into holder_acls, holder_units
        from acl_grants g left join evidence_units u on u.id = g.unit_id
        where g.holder = h.holder and (g.unit_id is null or u.expires_at > statement_timestamp());
    end if;
    if cardinality(holder_acls) > 0 then
      reader_acls := reader_acls || holder_acls;
      reader_holders := reader_holders + 1;
      used := used || holder_units;
    end if;
  end loop;
  if reader_holders > 1 then
    reader_acls := arr_sort(reader_acls);
  end if;
  visible := case when visible is null then reader_acls else arr_and(visible, reader_acls) end;
  with live_leaves as materialized (
    select a.id, a.rows from unnest(visible) u(id) join acls a on a.id = u.id join sources s on s.id = a.source_id
    where a.kind = 'leaf' and (personal_ok or not a.personal)
      and (s.publication = 'published' or s.id = parity_source) and s.tenant_id = any (tenant_ids)),
  satisfied as (
    select c.acl_id from acl_conjunctions c cross join lateral unnest(c.children) ch(id) join live_leaves l on l.id = ch.id
    group by c.acl_id, c.children having count(*) = cardinality(c.children)),
  eligible as (
    select id, rows from live_leaves
    union all
    select a.id, a.rows from satisfied sc join acls a on a.id = sc.acl_id join sources s on s.id = a.source_id
      where (personal_ok or not a.personal) and (s.publication = 'published' or s.id = parity_source)
        and s.tenant_id = any (tenant_ids)),
  limited as (select id, rows from eligible where limits is null or id = any (limits))
  select coalesce(array_agg(id order by id), '{}'), coalesce(sum(rows), 0)::float8 / greatest((select sum(rows) from acls), 1)
    into v_acls, v_fraction from limited;
  return query select v_acls,
    arr_and(arr_sort(used), (select coalesce(array_agg(id), '{}') from evidence_units where expires_at > statement_timestamp())),
    arr_sort(skip), v_fraction;
end $$;

create or replace function {{data}}.acls_for(readers jsonb, personal_ok boolean, limits int[], tenant_ids uuid[])
  returns table (acls int[], units int[], skipped int[], fraction float8) language sql stable
  set search_path = {{data}}, pg_catalog, pg_temp as $$
  select * from acls_for(readers, personal_ok, limits, tenant_ids, null::uuid)
$$;

-- The records of `src` this person would read if it were published: what the reader role
-- shows, minus the source's own hiding rules (freshness, status) and plus the source counted
-- as published. Run as daimon_data_parity in a read-only transaction.
create function {{data}}.parity_visible(src uuid, account uuid) returns setof text
  language sql stable security definer rows 100
  set search_path = {{data}}, pg_catalog, pg_temp as $$
  select r.external_id from records r, sources s,
    lateral acls_for(jsonb_build_array('account:' || account::text), true, null, array[s.tenant_id], src) v
  where s.id = src and r.source_id = src and r.acl = any (v.acls)
    and not exists (select 1 from record_access x where x.source_id = r.source_id
                    and x.external_id = r.external_id
                    and (x.held_at is not null or x.until <= statement_timestamp()))
$$;
