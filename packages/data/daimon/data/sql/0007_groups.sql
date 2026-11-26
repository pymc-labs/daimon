-- Source groups and personal reads. {{data}} is the store's schema.
--
-- A source group is the sources of one tenant and one subject namespace whose config names the
-- same `group` (one Google connection: its directory, Drive and Calendar). Group membership read
-- by any of them grants the access lists of all of them; a source with no group, or in another
-- group, tenant or namespace, never grants another's. Before this, evidence never crossed sources.
-- Inert until a source names a group.

create function {{data}}.shares_evidence(unit_source uuid, acl_source uuid) returns boolean
  language sql stable set search_path = {{data}}, pg_catalog, pg_temp as $$
  select unit_source = acl_source or exists (
    select 1 from sources u join sources a on a.tenant_id = u.tenant_id
      and coalesce(a.provider, a.kind) = coalesce(u.provider, u.kind)
      and a.config->>'group' = u.config->>'group'
    where u.id = unit_source and a.id = acl_source and coalesce(u.config->>'group', '') <> '')
$$;

-- 0001's, with evidence shared within a source group.
create or replace function {{data}}.publish(unit int, seq bigint, groups jsonb default '{}', fetched_at timestamptz default now(),
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
        join evidence_units u on u.id = m.unit_id and shares_evidence(u.source_id, a.source_id)
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

create or replace function {{data}}.intern_acl(src uuid, refs jsonb default '[]', all_of int[] default null,
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
      join evidence_units u on u.id = m.unit_id and shares_evidence(u.source_id, src)
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

create or replace view {{data}}.acl_grant_truth as
  select m.subject as holder, r.acl_id, m.unit_id
    from {{data}}.group_members m join {{data}}.acl_refs r on r.is_group and r.ref = m.group_ref
    join {{data}}.acls a on a.id = r.acl_id join {{data}}.evidence_units u on u.id = m.unit_id
    where {{data}}.shares_evidence(u.source_id, a.source_id)
  union
  select r.ref, r.acl_id, r.unit_id from {{data}}.acl_refs r where not r.is_group;

-- A source that joins or leaves a group (or moves tenant or namespace) changes whose evidence
-- counts for its lists: the grants are derived again, under the membership lock.
create function {{data}}.source_group_moved() returns trigger language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
begin
  perform acl_rebuild(true);
  return null;
end $$;
create trigger source_group_moved after update on {{data}}.sources for each row
  when (old.config->>'group' is distinct from new.config->>'group'
        or old.provider is distinct from new.provider or old.tenant_id is distinct from new.tenant_id)
  execute function {{data}}.source_group_moved();

-- A person who turned off one of their personal parts in a tenant: Daimon does not read it as
-- them (`drive`: their own Drive; `calendar`: their calendar). Read and written by the app.
create table {{data}}.personal_off (
  tenant_id uuid not null,
  account_id uuid not null,
  part text not null check (part in ('drive', 'calendar')),
  created_at timestamptz not null default now(),
  primary key (tenant_id, account_id, part)
);
revoke all on {{data}}.personal_off from public;
