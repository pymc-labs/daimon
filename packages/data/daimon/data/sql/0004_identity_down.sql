-- Revision 0241's downgrade: drop what 0004_identity.sql made and put back 0001's reader
-- functions, so a later upgrade makes it again.
drop trigger if exists refuse_erased on {{data}}.grants;
drop table if exists {{data}}.identities, {{data}}.link_decisions,
  {{data}}.identity_notices, {{data}}.person_connections, {{data}}.connect_pending,
  {{data}}.connect_clients, {{data}}.erased_accounts;
drop function if exists {{data}}.refuse_erased(), {{data}}.forget_person(uuid),
  {{data}}.reader_subjects(jsonb, uuid[]), {{data}}.linked_subjects(uuid, uuid[]);
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
create or replace function {{data}}.acls_for(readers jsonb, personal_ok boolean, limits int[], tenant_ids uuid[])
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
revoke all on function {{data}}.reader_subjects(jsonb) from public;
delete from {{data}}.schema_version where file = '0004_identity.sql';
