-- The store's first tables: sources and their content, the access lists that guard it, the
-- row policies, scopes and grants, and the audit trail. {{data}} is the store's schema.
--
-- Tenancy is per source. Tenant-owned rows carry tenant_id; content reaches its tenant only
-- through source_id, and every policy and every security-definer function checks
-- visible_sources(), which is empty without a read context. Evidence never crosses sources: a
-- group's members grant only the lists of the source whose unit read them. Policies name no role, so a
-- restore with --no-privileges keeps them; 0001_grants.sql puts the grants back.
--
-- Every function pins its search_path to the store, so the bodies can name tables plainly.

-- ---- sources and content -------------------------------------------------------------------

create table {{data}}.sources (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null,
  kind text not null,
  name text not null,
  provider text,                    -- the namespace of the source's subjects; null: its kind
  config jsonb not null default '{}',
  credential text,                  -- sealed by the app (MultiFernet); never granted to a reader
  default_scope text not null,
  status text not null default 'active' check (status in ('active', 'paused', 'error', 'disabled')),
  -- Read only while 'published'. 'shadow': copied but not shown yet; 'retired': closed.
  publication text not null default 'shadow' check (publication in ('shadow', 'published', 'retired')),
  owner_account_id uuid,            -- a person's own source: erased with them
  created_by uuid,
  created_at timestamptz not null default now()
);
create index on {{data}}.sources (tenant_id);
create index on {{data}}.sources (owner_account_id) where owner_account_id is not null;

-- A record of a source. external_id is its key there, `<kind>:<id>`. acl: its access list
-- (written by set_record_acl only); null: the record is read by its scope.
create table {{data}}.records (
  id uuid primary key default gen_random_uuid(),
  source_id uuid not null references {{data}}.sources on delete cascade,
  external_id text not null,
  kind text not null,
  title text not null default '',
  body text not null default '',
  url text,
  author text,
  occurred_at timestamptz not null,
  scope text not null,
  payload jsonb not null default '{}',
  hash text,
  ingested_at timestamptz not null default now(),
  tsv tsvector generated always as
    (setweight(to_tsvector('english', title), 'A') || to_tsvector('english', body)) stored,
  acl int,
  unique (source_id, external_id),
  unique (id, source_id)
);
create index on {{data}}.records using gin (tsv);
create index on {{data}}.records (source_id, occurred_at desc);
create index records_acl on {{data}}.records (acl) where acl is not null;

-- A chunk reads as its record: its source, scope and access list are copied from it by
-- triggers, never written by the caller.
create table {{data}}.chunks (
  id uuid primary key default gen_random_uuid(),
  record_id uuid not null,
  source_id uuid not null,
  ord int not null,
  text text not null,
  ctx text not null default '',
  scope text not null,
  acl int,
  tsv tsvector generated always as (to_tsvector('english', ctx || ' ' || text)) stored,
  foreign key (record_id, source_id) references {{data}}.records (id, source_id) on delete cascade,
  unique (record_id, ord)
);
create index on {{data}}.chunks using gin (tsv);
create index on {{data}}.chunks (source_id);
create index chunks_acl on {{data}}.chunks (acl) where acl is not null;

create function {{data}}.chunk_follows_record() returns trigger language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
begin
  select r.source_id, r.scope, r.acl into new.source_id, new.scope, new.acl
    from records r where r.id = new.record_id;
  return new;
end $$;
create trigger follows_record before insert or update on {{data}}.chunks
  for each row execute function {{data}}.chunk_follows_record();

create function {{data}}.record_moves_chunks() returns trigger language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
begin
  update chunks set scope = new.scope, acl = new.acl where record_id = new.id;
  return null;
end $$;
create trigger moves_chunks after update of scope, acl on {{data}}.records for each row
  when (old.scope is distinct from new.scope or old.acl is distinct from new.acl)
  execute function {{data}}.record_moves_chunks();

-- ---- access lists ----------------------------------------------------------------------------
-- A copy (a record, or a later per-connector row keyed by source_id and external_id) carries
-- one `acl` id. An ACL is a set of source principals (a leaf) or a conjunction of leaves. Who
-- holds an ACL is materialized (acl_grants, holder_sets) by publish, intern_acl and the delete
-- triggers; a reader's visible ACL ids are filled per audience into audience_acls and checked
-- on every statement.

-- intarray gives sorted-set `&` and `uniq(sort())` in C; without it, SQL fallbacks.
do $$
declare ns text := (select extnamespace::regnamespace::text from pg_extension where extname = 'intarray');
begin
  if ns is not null then
    execute format($f$create function {{data}}.arr_and(a int[], b int[]) returns int[] language sql
      immutable parallel safe set search_path = pg_catalog, %I as 'select a & b'$f$, ns);
    execute format($f$create function {{data}}.arr_sort(a int[]) returns int[] language sql
      immutable parallel safe set search_path = pg_catalog, %I as 'select uniq(sort(a))'$f$, ns);
  else
    create function {{data}}.arr_and(a int[], b int[]) returns int[] language sql immutable parallel safe
      set search_path = pg_catalog as
      'select coalesce(array(select x from (select unnest(a) intersect select unnest(b)) t(x) order by x), ''{}'')';
    create function {{data}}.arr_sort(a int[]) returns int[] language sql immutable parallel safe
      set search_path = pg_catalog as
      'select coalesce(array(select distinct x from unnest(a) x order by x), ''{}'')';
  end if;
end $$;

-- One counter, bumped at the commit of any change that can alter an audience's visible ACLs
-- (except a unit's renewal). A cached audience is valid only while it equals this. A
-- transaction marks itself in acl_epoch_marks; a deferred trigger turns the mark into one
-- update at commit.
create table {{data}}.acl_epoch (id int primary key default 1 check (id = 1), n bigint not null default 1);
insert into {{data}}.acl_epoch default values;
create unlogged table {{data}}.acl_epoch_marks (xact xid8 primary key);

-- A pass, a person's connection or a group read: the unit of evidence. A grant riding on a
-- unit stops when the unit expires, inside Postgres, with no job running. published_seq
-- orders passes: an older one is discarded.
create table {{data}}.evidence_units (
  id int generated always as identity primary key,
  source_id uuid not null references {{data}}.sources on delete cascade,
  kind text not null,
  ref text not null,
  expires_at timestamptz not null default 'epoch',
  published_seq bigint not null default 0,
  unique (source_id, kind, ref)
);

-- A leaf (one source's principals) or a conjunction ('all': two or more of the source's
-- leaves). Id 0 is nobody, and the only ACL without a source.
create table {{data}}.acls (
  id int generated by default as identity primary key,
  source_id uuid references {{data}}.sources on delete cascade,
  kind text not null check (kind in ('leaf', 'all')),
  personal boolean not null default false,   -- one person's private record: read only with personal_ok
  rows int not null default 0,               -- records on it, refreshed nightly: the audience's visible fraction
  key_hash text not null unique,
  check ((id = 0) = (source_id is null))
);
insert into {{data}}.acls (id, source_id, kind, key_hash) values (0, null, 'leaf', 'nobody');
select setval(pg_get_serial_sequence('{{data}}.acls', 'id'), 1, false);

-- A leaf's refs. A direct ref is a subject (`<provider>/<workspace>:<kind>:<id>`, `account:<uuid>`
-- or '*'), its grant riding on unit_id; a group ref names a group whose members come from
-- group_members, each with its own unit.
create table {{data}}.acl_refs (
  acl_id int not null references {{data}}.acls on delete cascade,
  ref text not null,
  is_group boolean not null default false,
  unit_id int references {{data}}.evidence_units on delete cascade
);
create unique index acl_refs_key on {{data}}.acl_refs (acl_id, ref, coalesce(unit_id, 0));
create index on {{data}}.acl_refs (ref);

create table {{data}}.acl_conjunctions (
  acl_id int primary key references {{data}}.acls on delete cascade,
  children int[] not null check (cardinality(children) >= 2)
);

-- Source-side truth per record: the ACL it has, when that was read, until when, and a hold.
-- The copies are written from this by set_record_acl only.
create table {{data}}.record_access (
  source_id uuid not null references {{data}}.sources on delete cascade,
  external_id text not null,
  acl_id int not null,
  read_at timestamptz not null,
  until timestamptz,
  held_at timestamptz,
  held_since timestamptz,
  held_why text,
  primary key (source_id, external_id)
);
create index on {{data}}.record_access (acl_id) where held_at is not null or until is not null;

create table {{data}}.group_members (
  group_ref text not null,
  subject text not null,
  unit_id int not null references {{data}}.evidence_units on delete cascade,
  primary key (group_ref, subject, unit_id)
);
create index on {{data}}.group_members (unit_id);
create index on {{data}}.group_members (subject);

create table {{data}}.acl_grants (
  holder text not null,
  acl_id int not null references {{data}}.acls on delete cascade,
  unit_id int references {{data}}.evidence_units on delete cascade
);
create unique index acl_grants_key on {{data}}.acl_grants (holder, acl_id, coalesce(unit_id, 0));
create index on {{data}}.acl_grants (acl_id);
create index on {{data}}.acl_grants (unit_id);

-- A holder's ACL ids as one sorted array, and the units they ride on, expired ones too: the
-- fill re-reads a holder whose unit expired, so a renewal needs no rebuild.
create table {{data}}.holder_sets (
  holder text primary key,
  acls int[] not null default '{}',
  units int[] not null default '{}'
);

-- The read context of one transaction (access.scoped), keyed by backend and transaction, so
-- a row left by an earlier transaction on the connection never applies. Written by the
-- store's owner before it switches to the reader role; never granted to a reader.
create unlogged table {{data}}.read_contexts (
  pid int primary key,
  xact xid8 not null,
  tenant_ids uuid[] not null,
  scopes text[] not null,
  readers jsonb not null,           -- subjects: `account:<uuid>` or a platform subject
  personal_ok boolean not null,
  limits int[],
  salt text not null,
  key text not null
);

-- Disposable caches: a lost row only means the next statement fills again.
create unlogged table {{data}}.audience_sets (
  key text primary key,
  epoch bigint not null,
  units int[] not null,
  skipped int[] not null default '{}',   -- units expired at the fill: the key is stale once one renews
  visible_fraction float8 not null
);
create unlogged table {{data}}.audience_acls (
  key text not null,
  acl_id int not null,
  primary key (key, acl_id)
);

-- THE MEMBERSHIP LOCK. One transaction-level advisory lock serializes every transaction that
-- derives or removes grants: publish, intern_acl's slow path, acl_rebuild, acl.unit when it
-- inserts, and the BEFORE DELETE statement triggers on sources, evidence_units and acls. It is
-- the first lock such a transaction takes and is held to its end; nothing else waits for it,
-- so its holders run one at a time and never wait on a transaction that waits for it.
create function {{data}}.membership_lock() returns void language sql
  set search_path = {{data}}, pg_catalog, pg_temp as
  $$select pg_advisory_xact_lock(hashtext('{{data}}.membership'), 0)$$;

create function {{data}}.bump_epoch() returns void language sql
  set search_path = {{data}}, pg_catalog, pg_temp as
  $$insert into acl_epoch_marks (xact) values (pg_current_xact_id()) on conflict do nothing$$;

create function {{data}}.epoch_commit() returns trigger language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
begin
  update acl_epoch set n = n + 1;
  delete from acl_epoch_marks where xact = new.xact;
  return null;
end $$;
create constraint trigger epoch_commit after insert on {{data}}.acl_epoch_marks
  deferrable initially deferred for each row execute function {{data}}.epoch_commit();

create function {{data}}.bump_epoch_trigger() returns trigger language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
begin
  perform bump_epoch();
  return null;
end $$;
create trigger acl_epoch after update on {{data}}.sources for each row
  when (old.publication is distinct from new.publication or old.provider is distinct from new.provider
        or old.tenant_id is distinct from new.tenant_id or old.status is distinct from new.status)
  execute function {{data}}.bump_epoch_trigger();
create trigger acl_epoch_delete after delete on {{data}}.sources for each row
  execute function {{data}}.bump_epoch_trigger();

-- A holder left with no grant has no row, so no subject outlives its last grant.
create function {{data}}.rebuild_holders(holders text[]) returns void language sql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
  delete from holder_sets s where s.holder = any (holders)
    and not exists (select 1 from acl_grants g where g.holder = s.holder);
  insert into holder_sets (holder, acls, units)
    select g.holder, arr_sort(array_agg(g.acl_id)),
           coalesce(array_agg(distinct g.unit_id order by g.unit_id) filter (where g.unit_id is not null), '{}')
    from acl_grants g where g.holder = any (holders) group by g.holder order by g.holder
  on conflict (holder) do update set acls = excluded.acls, units = excluded.units
$$;

create function {{data}}.delete_lock() returns trigger language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
begin
  perform membership_lock();
  return null;
end $$;
create trigger delete_lock before delete on {{data}}.sources for each statement
  execute function {{data}}.delete_lock();
create trigger delete_lock before delete on {{data}}.evidence_units for each statement
  execute function {{data}}.delete_lock();
create trigger delete_lock before delete on {{data}}.acls for each statement
  execute function {{data}}.delete_lock();

-- Grants that leave without publish or intern_acl (a source delete cascades to its units and
-- ACLs): rebuild those holders' sets and move the epoch in the same transaction. The writers
-- set daimon_data.acl_maint while they delete grants themselves, since they rebuild already.
create function {{data}}.grants_deleted() returns trigger language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
declare
  holders text[];
begin
  if coalesce(current_setting('daimon_data.acl_maint', true), '') = 'on' then
    return null;
  end if;
  select array_agg(distinct holder) into holders from gone;
  if holders is not null then
    perform rebuild_holders(holders);
    perform bump_epoch();
  end if;
  return null;
end $$;
create trigger grants_deleted after delete on {{data}}.acl_grants referencing old table as gone
  for each statement execute function {{data}}.grants_deleted();

-- A unit's whole pass, atomically. `groups` is the unit's complete batch {group: [subjects]}:
-- a group missing from it loses every member that unit gave it. An older pass (seq not above
-- published_seq) changes nothing; a pass with no change only renews the unit.
create function {{data}}.publish(unit int, seq bigint, groups jsonb default '{}', fetched_at timestamptz default now(),
                                 lifetime interval default interval '1 hour') returns boolean language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
declare
  current_seq bigint;
  changed text[];
  touched text[];
begin
  perform membership_lock();
  select published_seq into current_seq from evidence_units where id = unit for update;
  if not found then
    raise exception 'no evidence unit %', unit;
  end if;
  if seq <= current_seq then
    return false;
  end if;
  with new_members as (
    select distinct g.key as group_ref, s.v as subject
    from jsonb_each(groups) g(key, value) cross join lateral jsonb_array_elements_text(g.value) s(v)),
  removed as (
    delete from group_members m where m.unit_id = unit and not exists
      (select 1 from new_members n where n.group_ref = m.group_ref and n.subject = m.subject)
    returning m.subject),
  added as (
    insert into group_members (group_ref, subject, unit_id)
      select n.group_ref, n.subject, unit from new_members n where not exists
        (select 1 from group_members m where m.unit_id = unit and m.group_ref = n.group_ref and m.subject = n.subject)
    returning subject)
  select coalesce(array_agg(distinct s), '{}') into changed
    from (select subject s from removed union all select subject from added) x;
  if cardinality(changed) > 0 then
    perform set_config('daimon_data.acl_maint', 'on', true);
    with want as (
      select m.subject as holder, r.acl_id, m.unit_id from group_members m
        join acl_refs r on r.is_group and r.ref = m.group_ref join acls a on a.id = r.acl_id
        join evidence_units u on u.id = m.unit_id and u.source_id = a.source_id
        where m.unit_id = unit and m.subject = any (changed)
      union
      select r.ref, r.acl_id, r.unit_id from acl_refs r where not r.is_group and r.unit_id = unit and r.ref = any (changed)),
    gone as (
      delete from acl_grants g where g.unit_id = unit and g.holder = any (changed) and not exists
        (select 1 from want w where w.holder = g.holder and w.acl_id = g.acl_id)
      returning g.holder),
    new_grants as (
      insert into acl_grants (holder, acl_id, unit_id) select holder, acl_id, unit_id from want
        on conflict do nothing returning holder)
    select coalesce(array_agg(distinct h), '{}') into touched
      from (select holder h from gone union all select holder from new_grants) x;
    perform set_config('daimon_data.acl_maint', 'off', true);
    if cardinality(touched) > 0 then
      perform rebuild_holders(touched);
      perform bump_epoch();
    end if;
  end if;
  update evidence_units set published_seq = seq, expires_at = fetched_at + lifetime where id = unit;
  return true;
end $$;

create function {{data}}.acl_key(src uuid, refs jsonb, all_of int[], personal boolean) returns text
  language sql immutable set search_path = {{data}}, pg_catalog, pg_temp as $$
  select encode(sha256(convert_to(case when all_of is not null
    then src::text || E'\nall\n' || arr_sort(all_of)::text
    else src::text || E'\nleaf\n' || personal::text || E'\n' || (
      select jsonb_agg(d order by d::text) from (select distinct value as d from jsonb_array_elements(refs)) x)::text end,
    'utf8')), 'hex')
$$;

-- The ACL id of a definition, without writing anything or taking a lock; null: none yet.
create function {{data}}.acl_lookup(src uuid, refs jsonb default '[]', all_of int[] default null,
                                    personal boolean default false) returns int language sql stable
  set search_path = {{data}}, pg_catalog, pg_temp as $$
  select id from acls where key_hash = acl_key(src, refs, all_of, personal)
$$;

-- A leaf from refs, or a conjunction of the same source's leaves; one definition of a source
-- is one id. The fast path (it exists) writes nothing and takes no lock.
create function {{data}}.intern_acl(src uuid, refs jsonb default '[]', all_of int[] default null,
                                    personal boolean default false) returns int language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
declare
  found_id int;
  new_id int;
  kids int[];
  def text;
  added text[];
begin
  if all_of is not null then
    kids := arr_sort(all_of);
    if cardinality(kids) < 2 or exists (select 1 from unnest(kids) k left join acls a on a.id = k
                                        where a.id is null or a.kind <> 'leaf' or a.source_id is distinct from src) then
      raise exception 'a conjunction needs two or more leaves of its source';
    end if;
    def := acl_key(src, refs, kids, personal);
  else
    if jsonb_typeof(refs) <> 'array' or jsonb_array_length(refs) = 0 then
      raise exception 'a leaf needs refs';
    end if;
    if exists (select 1 from jsonb_array_elements(refs) x left join evidence_units u on u.id = (x->>'unit')::int
               where x->>'unit' is not null and u.source_id is distinct from src) then
      raise exception 'a ref rides on an evidence unit of its own source only';
    end if;
    def := acl_key(src, refs, null, personal);
  end if;
  select id into found_id from acls where key_hash = def;
  if found then
    return found_id;
  end if;
  perform membership_lock();
  select id into found_id from acls where key_hash = def;   -- another writer may have made it meanwhile
  if found then
    return found_id;
  end if;
  if all_of is not null then
    insert into acls (source_id, kind, personal, key_hash) values (src, 'all', personal, def) returning id into new_id;
    insert into acl_conjunctions (acl_id, children) values (new_id, kids);
    return new_id;
  end if;
  insert into acls (source_id, kind, personal, key_hash) values (src, 'leaf', personal, def) returning id into new_id;
  insert into acl_refs (acl_id, ref, is_group, unit_id)
    select new_id, x->>'ref', coalesce((x->>'group')::boolean, false),
           case when coalesce((x->>'group')::boolean, false) then null else (x->>'unit')::int end
    from jsonb_array_elements(refs) x on conflict do nothing;
  with want as (
    select r.ref as holder, r.acl_id, r.unit_id from acl_refs r where r.acl_id = new_id and not r.is_group
    union
    select m.subject, r.acl_id, m.unit_id from acl_refs r join group_members m on m.group_ref = r.ref
      join evidence_units u on u.id = m.unit_id and u.source_id = src
      where r.acl_id = new_id and r.is_group),
  new_grants as (
    insert into acl_grants (holder, acl_id, unit_id) select holder, acl_id, unit_id from want
      on conflict do nothing returning holder)
  select coalesce(array_agg(distinct holder), '{}') into added from new_grants;
  if cardinality(added) > 0 then
    perform rebuild_holders(added);
    perform bump_epoch();
  end if;
  return new_id;
end $$;

-- A copy's acl is written here only (guard_acl). Guards on rows outside this file name it.
create function {{data}}.guard_acl() returns trigger language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
begin
  if coalesce(current_setting('daimon_data.acl_writer', true), '') = 'on' then
    return new;
  end if;
  if (tg_op = 'INSERT' and new.acl is not null) or (tg_op = 'UPDATE' and new.acl is distinct from old.acl) then
    raise exception 'acl is written by set_record_acl only';
  end if;
  return new;
end $$;
create trigger guard_acl before insert or update of acl on {{data}}.records
  for each row execute function {{data}}.guard_acl();

-- A record's ACL as read at `read_start` (`evidence_at`: when its proof was read, if earlier),
-- on the record and on every table in `copies` (keyed by source_id and external_id, with an
-- acl column). An older read never repoints it; a hold (held) hides it from every reader until
-- a later read; a lapsed `until` hides it with no job. Returns the ACL the copies read as.
create function {{data}}.set_record_acl(src uuid, ext text, new_acl int, read_start timestamptz,
                                        new_until timestamptz default null, held boolean default false,
                                        why text default null, copies regclass[] default '{}',
                                        evidence_at timestamptz default null) returns int language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
declare
  ra record_access%rowtype;
  first_read int;
  copy regclass;
  proof timestamptz := coalesce(evidence_at, read_start);
begin
  if new_acl <> 0 and not exists (select 1 from acls where id = new_acl and source_id = src) then
    raise exception 'acl % is not an access list of source %', new_acl, src;
  end if;
  perform set_config('daimon_data.acl_writer', 'on', true);
  insert into record_access (source_id, external_id, acl_id, read_at, until) values (src, ext, new_acl, read_start, new_until)
    on conflict (source_id, external_id) do nothing;
  get diagnostics first_read = row_count;
  select * into ra from record_access where source_id = src and external_id = ext for update;
  if first_read = 0 then
    if held then
      update record_access set held_at = greatest(coalesce(held_at, read_start), read_start),
             held_since = coalesce(held_since, held_at, read_start), held_why = coalesce(why, held_why)
        where source_id = src and external_id = ext;
    elsif read_start > ra.read_at and (ra.held_at is null or read_start > ra.held_at) then
      update record_access set acl_id = case when ra.held_at is null or proof > ra.held_at then new_acl else ra.acl_id end,
             read_at = read_start, until = new_until,
             held_at = case when ra.held_at is null or proof > ra.held_at then null else ra.held_at end,
             held_since = case when ra.held_at is null or proof > ra.held_at then null else ra.held_since end,
             held_why = case when ra.held_at is null or proof > ra.held_at then null else ra.held_why end
        where source_id = src and external_id = ext;
    end if;
  elsif held then
    update record_access set held_at = read_start, held_since = read_start, held_why = why
      where source_id = src and external_id = ext;
  end if;
  select * into ra from record_access where source_id = src and external_id = ext;
  -- A held record's copies keep its list (the lapsed set hides them) or go to 0, never to a
  -- list that is not the record's. An unheld record's copies take its list.
  if ra.held_at is not null then
    update records set acl = 0 where source_id = src and external_id = ext
      and acl is distinct from ra.acl_id and acl is distinct from 0;
  else
    update records set acl = ra.acl_id where source_id = src and external_id = ext and acl is distinct from ra.acl_id;
  end if;
  foreach copy in array copies loop
    if ra.held_at is not null then
      execute format('update %s set acl = 0 where source_id = $1 and external_id = $2'
                     ' and acl is distinct from $3 and acl is distinct from 0', copy) using src, ext, ra.acl_id;
    else
      execute format('update %s set acl = $3 where source_id = $1 and external_id = $2 and acl is distinct from $3', copy)
        using src, ext, ra.acl_id;
    end if;
  end loop;
  perform set_config('daimon_data.acl_writer', 'off', true);
  return case when ra.held_at is not null then 0 else ra.acl_id end;
end $$;

-- set_record_acl for a batch: each `{ext, acl, until, held, why}`.
create function {{data}}.set_record_acls(src uuid, copies regclass[], recs jsonb, read_start timestamptz)
  returns int language plpgsql set search_path = {{data}}, pg_catalog, pg_temp as $$
declare
  r jsonb;
  n int := 0;
begin
  for r in select * from jsonb_array_elements(recs) loop
    perform set_record_acl(src, r->>'ext', (r->>'acl')::int, read_start, (r->>'until')::timestamptz,
                           coalesce((r->>'held')::boolean, false), r->>'why', copies);
    n := n + 1;
  end loop;
  return n;
end $$;

-- What the grants and holder sets must be, from the definitions alone (the nightly rebuild).
create view {{data}}.acl_grant_truth as
  select m.subject as holder, r.acl_id, m.unit_id
    from {{data}}.group_members m join {{data}}.acl_refs r on r.is_group and r.ref = m.group_ref
    join {{data}}.acls a on a.id = r.acl_id join {{data}}.evidence_units u on u.id = m.unit_id
    where u.source_id = a.source_id
  union
  select r.ref, r.acl_id, r.unit_id from {{data}}.acl_refs r where not r.is_group;
create view {{data}}.acl_holder_truth as
  select holder, {{data}}.arr_sort(array_agg(acl_id)) as acls,
         coalesce(array_agg(distinct unit_id order by unit_id) filter (where unit_id is not null), '{}') as units
  from {{data}}.acl_grant_truth group by holder;

create function {{data}}.acl_rebuild(repair boolean default true) returns table (what text, n bigint) language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
declare
  missing bigint;
  extra bigint;
  sets bigint;
begin
  perform membership_lock();
  select count(*) into missing from (select holder, acl_id, coalesce(unit_id, 0) from acl_grant_truth
    except select holder, acl_id, coalesce(unit_id, 0) from acl_grants) d;
  select count(*) into extra from (select holder, acl_id, coalesce(unit_id, 0) from acl_grants
    except select holder, acl_id, coalesce(unit_id, 0) from acl_grant_truth) d;
  select count(*) into sets from (
    select coalesce(r.holder, h.holder) from acl_holder_truth r full join holder_sets h on h.holder = r.holder
    where coalesce(r.acls, '{}') <> coalesce(h.acls, '{}') or coalesce(r.units, '{}') <> coalesce(h.units, '{}')) d;
  if repair and missing + extra + sets > 0 then
    perform set_config('daimon_data.acl_maint', 'on', true);
    delete from acl_grants;
    insert into acl_grants (holder, acl_id, unit_id) select holder, acl_id, unit_id from acl_grant_truth;
    delete from holder_sets;
    insert into holder_sets (holder, acls, units) select holder, acls, units from acl_holder_truth;
    perform set_config('daimon_data.acl_maint', 'off', true);
    perform bump_epoch();
  end if;
  return query select 'grant_missing', missing union all select 'grant_extra', extra union all select 'holder_set', sets;
end $$;

-- ---- the audience ----------------------------------------------------------------------------

-- The holders each reader stands for, by the reader's position. A reader is `account:<uuid>`
-- or a platform subject, and holds itself and '*'. Anything else resolves to nobody, which
-- empties the whole audience. Identity linking replaces this to add a person's linked subjects.
create function {{data}}.reader_subjects(readers jsonb) returns table (ord int, holder text) language sql stable
  set search_path = {{data}}, pg_catalog, pg_temp as $$
  with r as (select v, o::int as ord from jsonb_array_elements_text(readers) with ordinality t(v, o)),
  known as (select ord, v from r
            where v ~ '^account:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               or v ~ '^[^/:[:space:]]+/[^/:[:space:]]+:[^:[:space:]]+:[^[:space:]]+$')
  select ord, v from known
  union select ord, '*' from known
$$;

-- The ACLs every reader holds (the intersection over readers), of published sources of
-- `tenant_ids` only; `limits`, when set, narrows them further.
create function {{data}}.acls_for(readers jsonb, personal_ok boolean, limits int[], tenant_ids uuid[])
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
     or (select count(distinct ord) from reader_subjects(readers)) <> jsonb_array_length(readers) then
    return query select '{}'::int[], '{}'::int[], '{}'::int[], 0::float8;
    return;
  end if;
  expired := array(select id from evidence_units where expires_at <= statement_timestamp());
  live := array(select id from evidence_units where expires_at > statement_timestamp());
  for h in select s.ord, s.holder, coalesce(hs.acls, '{}') as acls, coalesce(hs.units, '{}') as units
           from reader_subjects(readers) s left join holder_sets hs on hs.holder = s.holder
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
      and s.publication = 'published' and s.tenant_id = any (tenant_ids)),
  satisfied as (
    select c.acl_id from acl_conjunctions c cross join lateral unnest(c.children) ch(id) join live_leaves l on l.id = ch.id
    group by c.acl_id, c.children having count(*) = cardinality(c.children)),
  eligible as (
    select id, rows from live_leaves
    union all
    select a.id, a.rows from satisfied sc join acls a on a.id = sc.acl_id join sources s on s.id = a.source_id
      where (personal_ok or not a.personal) and s.publication = 'published' and s.tenant_id = any (tenant_ids)),
  limited as (select id, rows from eligible where limits is null or id = any (limits))
  select coalesce(array_agg(id order by id), '{}'), coalesce(sum(rows), 0)::float8 / greatest((select sum(rows) from acls), 1)
    into v_acls, v_fraction from limited;
  return query select v_acls,
    arr_and(arr_sort(used), (select coalesce(array_agg(id), '{}') from evidence_units where expires_at > statement_timestamp())),
    arr_sort(skip), v_fraction;
end $$;

create function {{data}}.audience_key_for(readers jsonb, personal_ok boolean, limits int[], salt text, tenant_ids uuid[])
  returns text language sql immutable set search_path = {{data}}, pg_catalog, pg_temp as $$
  select encode(sha256(convert_to(
    (select coalesce(jsonb_agg(v order by v), '[]') from (select distinct v from jsonb_array_elements_text(readers) v) d)::text
    || '|' || personal_ok::text || '|' || coalesce(arr_sort(limits)::text, '-') || '|' || salt
    || '|' || coalesce((select array_agg(distinct t order by t) from unnest(tenant_ids) t)::text, '{}'), 'utf8')), 'hex')
$$;

-- A filled audience is current while the epoch has not moved, none of its units expired, and
-- none of the units it skipped as expired has been renewed since.
create function {{data}}.audience_valid(k text) returns boolean language sql stable
  set search_path = {{data}}, pg_catalog, pg_temp as $$
  select exists (
    select 1 from audience_sets s join acl_epoch e on e.n = s.epoch
    where s.key = k
      and not exists (select 1 from unnest(s.units) x(id) left join evidence_units u on u.id = x.id
                      where u.id is null or u.expires_at <= statement_timestamp())
      and not exists (select 1 from unnest(s.skipped) x(id) join evidence_units u on u.id = x.id
                      where u.expires_at > statement_timestamp()))
$$;

-- Fill an audience's visible list, once per key while it stays valid. The app calls it in a
-- short transaction of its own before the read, then once more inside it (no arguments: this
-- transaction's context), where it is almost always a no-op.
create function {{data}}.fill_audience(readers jsonb, personal_ok boolean, limits int[], salt text, tenant_ids uuid[])
  returns int language plpgsql set search_path = {{data}}, pg_catalog, pg_temp as $$
declare
  k text := audience_key_for(readers, personal_ok, limits, salt, tenant_ids);
  cur bigint;
  v record;
begin
  if jsonb_typeof(readers) <> 'array' or jsonb_array_length(readers) not between 1 and 200 then
    return 0;
  end if;
  perform pg_advisory_xact_lock(hashtext('{{data}}.audience'), hashtext(k));
  if audience_valid(k) then
    return (select count(*) from audience_acls where key = k);
  end if;
  select n into cur from acl_epoch;
  select * into v from acls_for(readers, personal_ok, limits, tenant_ids);
  delete from audience_acls where key = k;
  delete from audience_sets where key = k;
  insert into audience_sets (key, epoch, units, skipped, visible_fraction) values (k, cur, v.units, v.skipped, v.fraction);
  insert into audience_acls (key, acl_id) select k, unnest(v.acls);
  return cardinality(v.acls);
end $$;

create function {{data}}.fill_audience() returns int language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
declare
  ctx read_contexts%rowtype;
begin
  select * into ctx from read_contexts where pid = pg_backend_pid() and xact = pg_current_xact_id_if_assigned();
  if not found then
    return 0;
  end if;
  return fill_audience(ctx.readers, ctx.personal_ok, ctx.limits, ctx.salt, ctx.tenant_ids);
end $$;

-- ---- what a reader may call: security definers, each limited to this transaction's context --
-- These read tables a reader cannot. Each returns only what the caller's own read context
-- allows, and nothing without one; every one that returns content ids checks visible_sources().

-- The published, enabled sources of the context's tenants.
create function {{data}}.visible_sources() returns setof uuid language sql stable security definer rows 10
  set search_path = {{data}}, pg_catalog, pg_temp as $$
  select s.id from sources s join read_contexts c on s.tenant_id = any (c.tenant_ids)
  where c.pid = pg_backend_pid() and c.xact = pg_current_xact_id_if_assigned()
    and s.publication = 'published' and s.status <> 'disabled'
$$;

-- The context's scopes. restricted:acl, the scope of rows read through their list, is never one.
create function {{data}}.scopes() returns text[] language sql stable security definer
  set search_path = {{data}}, pg_catalog, pg_temp as $$
  select coalesce((select array(select s from unnest(c.scopes) s where s <> 'restricted:acl') from read_contexts c
                   where c.pid = pg_backend_pid() and c.xact = pg_current_xact_id_if_assigned()), '{}')
$$;

create function {{data}}.audience_key() returns text language sql stable security definer
  set search_path = {{data}}, pg_catalog, pg_temp as $$
  select c.key from read_contexts c
  where c.pid = pg_backend_pid() and c.xact = pg_current_xact_id_if_assigned()
    and jsonb_array_length(c.readers) between 1 and 200 and audience_valid(c.key)
$$;

-- The context's visible ACL ids, of visible sources only.
create function {{data}}.visible_acls() returns setof int language sql stable security definer rows 100
  set search_path = {{data}}, pg_catalog, pg_temp as $$
  select a.acl_id from audience_acls a join acls l on l.id = a.acl_id
  where a.key = audience_key() and l.source_id in (select visible_sources())
$$;

-- Records on a visible list that are held or whose lease lapsed: hidden, with no job to run.
create function {{data}}.lapsed_record_ids() returns setof uuid language sql stable security definer rows 10
  set search_path = {{data}}, pg_catalog, pg_temp as $$
  select r.id from record_access a join records r on r.source_id = a.source_id and r.external_id = a.external_id
  where (a.held_at is not null or a.until <= statement_timestamp())
    and r.acl in (select visible_acls()) and r.source_id in (select visible_sources())
$$;

-- The share of listed records the context may read (search widens its vector walk for a small one).
create function {{data}}.visible_fraction() returns float8 language sql stable security definer
  set search_path = {{data}}, pg_catalog, pg_temp as $$
  select case when audience_key() is null then 0
    when exists (select 1 from acls where rows > 0)
    then coalesce((select s.visible_fraction from audience_sets s where s.key = audience_key()), 0) else 1 end
$$;

-- ---- policies: a reader sees a row of a visible source by its scope, or, with a list, by
-- the audience's list while the record has not lapsed. -----------------------------------------

alter table {{data}}.sources enable row level security;
create policy visible on {{data}}.sources for select using (id in (select {{data}}.visible_sources()));

alter table {{data}}.records enable row level security;
create policy visible on {{data}}.records for select using (
  source_id in (select {{data}}.visible_sources())
  and ((acl is null and scope = any ((select {{data}}.scopes())::text[]))
       or (acl is not null and acl in (select {{data}}.visible_acls())
           and id not in (select {{data}}.lapsed_record_ids()))));

alter table {{data}}.chunks enable row level security;
create policy visible on {{data}}.chunks for select using (
  source_id in (select {{data}}.visible_sources())
  and ((acl is null and scope = any ((select {{data}}.scopes())::text[]))
       or (acl is not null and acl in (select {{data}}.visible_acls())
           and record_id not in (select {{data}}.lapsed_record_ids()))));

-- ---- scopes, grants, audit -------------------------------------------------------------------

create table {{data}}.scopes (
  tenant_id uuid not null,
  id text not null,
  kind text not null check (kind in ('public', 'internal', 'shared', 'client', 'restricted')),
  name text not null,
  description text not null default '',
  created_at timestamptz not null default now(),
  primary key (tenant_id, id)
);

-- Who holds a scope: `account:<uuid>`, or a platform subject such as a role.
create table {{data}}.grants (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null,
  scope text not null,
  holder text not null,
  granted_by uuid,
  created_at timestamptz not null default now(),
  foreign key (tenant_id, scope) references {{data}}.scopes on delete cascade,
  unique (tenant_id, holder, scope)
);

create table {{data}}.audit (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null,
  at timestamptz not null default now(),
  account_id uuid,
  kind text not null,
  target text,
  detail jsonb not null default '{}',
  source_id uuid                    -- no foreign key: the trail outlives its source
);
create index on {{data}}.audit (tenant_id, at desc, id desc);
create index on {{data}}.audit (account_id) where account_id is not null;
