-- People: who each source identity is, and how that was proved. {{data}} is the store's schema.
--
-- An identity is one user of a provider's workspace (`<provider>/<workspace>:user:<subject>`).
-- A source's listing records it; a proof links it to a Daimon account. A person reads as their
-- account plus every identity linked to it in the reading tenant, so a link is a grant: it is
-- written only by a proof the provider or platform verified, or by a data admin acting on
-- someone else (daimon.data.identity). Nothing here is granted to a reader.

create table {{data}}.identities (
  tenant_id uuid not null,
  provider text not null check (provider ~ '^[^/:[:space:]]+$'),
  workspace text not null check (workspace ~ '^[^/:[:space:]]+$'),
  subject text not null check (subject ~ '^[^[:space:]]+$'),
  account_id uuid,                  -- null: known, linked to nobody
  proof text check (proof in ('platform', 'github', 'oauth', 'slack', 'microsoft', 'imported',
                              'admin', 'email', 'auto')),
  email text,
  email_verified boolean not null default false,   -- the provider verified it: a link candidate
  name text not null default '',
  listed boolean not null default false,           -- in the source's last account listing
  active boolean not null default true,            -- as that listing said
  linked_by uuid,                   -- the data admin of an admin or email link
  linked_at timestamptz,
  created_at timestamptz not null default now(),
  primary key (tenant_id, provider, workspace, subject),
  check ((account_id is null) = (proof is null)),
  check (provider <> 'microsoft' or not email_verified)   -- an Entra email proves nothing
);
create index on {{data}}.identities (account_id) where account_id is not null;
create index on {{data}}.identities (tenant_id, lower(email)) where email_verified;

-- Every admin decision on a pair (approved, rejected, unlinked). An account or subject named
-- here is never linked by an auto-link in that workspace again; a rejected pair is never a
-- candidate again.
create table {{data}}.link_decisions (
  tenant_id uuid not null,
  provider text not null,
  workspace text not null,
  subject text not null,
  account_id uuid not null,
  decision text not null check (decision in ('approved', 'rejected', 'unlinked')),
  decided_by uuid,
  decided_at timestamptz not null default now(),
  primary key (tenant_id, provider, workspace, subject, account_id)
);
create index on {{data}}.link_decisions (account_id);

-- Who may link others: granted by the tenant's owner or another data admin.
create table {{data}}.data_admins (
  tenant_id uuid not null,
  account_id uuid not null,
  granted_by uuid,
  created_at timestamptz not null default now(),
  primary key (tenant_id, account_id)
);

-- What a person is told about links others made or ended for them; read by the app.
create table {{data}}.identity_notices (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null,
  account_id uuid not null,
  kind text not null check (kind in ('linked', 'unlinked')),
  detail jsonb not null default '{}',
  created_at timestamptz not null default now(),
  seen_at timestamptz
);
create index on {{data}}.identity_notices (account_id) where seen_at is null;

-- A person's own sign-in at a source's provider (OAuth code or device flow). Its tokens are
-- sealed by the app. made_link: this connection made the identity's link (else it was there).
create table {{data}}.person_connections (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null,
  account_id uuid not null,
  source_id uuid not null references {{data}}.sources on delete cascade,
  provider text not null,
  workspace text not null,
  subject text not null,
  credential text,
  scopes text[] not null default '{}',
  access_expires_at timestamptz,
  refresh_expires_at timestamptz,
  status text not null default 'active' check (status in ('active', 'reconnect')),
  last_ok_at timestamptz,
  last_error text,
  generation int not null default 1,   -- moves at every connect: an older token exchange is dropped
  made_link boolean not null default true,
  created_at timestamptz not null default now(),
  unique (account_id, source_id),
  unique (source_id, subject)
);

-- A started sign-in until it ends or expires; secret: the sealed PKCE verifier or device code.
create table {{data}}.connect_pending (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null,
  account_id uuid not null,
  source_id uuid not null references {{data}}.sources on delete cascade,
  provider text not null,
  flow text not null check (flow in ('code', 'device')),
  state_hash text unique,
  secret text not null,
  expires_at timestamptz not null
);

-- The client a provider registered for this deployment (dynamic registration), sealed.
create table {{data}}.connect_clients (
  provider text primary key,
  credential text not null
);

-- ---- erasure -----------------------------------------------------------------------------------

-- Accounts erased since the store existed, written by the account erasure in the purge's
-- transaction. With the account lock the app takes first, no write lands after an erasure.
create table {{data}}.erased_accounts (
  account_id uuid primary key,
  erased_at timestamptz not null default now()
);
do $$
begin
  -- Core's tombstones, where this role may read them.
  if to_regclass('erased_account') is not null
     and has_table_privilege('erased_account', 'select') then
    execute 'insert into {{data}}.erased_accounts (account_id) select account_id from erased_account
             on conflict do nothing';
  end if;
end $$;

-- Refuses a row naming an erased account in the column tg_argv[0] (`account:<uuid>` or a uuid).
create function {{data}}.refuse_erased() returns trigger language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
declare
  v text := to_jsonb(new) ->> tg_argv[0];
begin
  if v like 'account:%' then
    v := substr(v, 9);
  end if;
  if v ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
     and exists (select 1 from erased_accounts where account_id = v::uuid) then
    raise exception 'the account was erased';
  end if;
  return new;
end $$;
create trigger refuse_erased before insert or update on {{data}}.identities
  for each row execute function {{data}}.refuse_erased('account_id');
create trigger refuse_erased before insert or update on {{data}}.link_decisions
  for each row execute function {{data}}.refuse_erased('account_id');
create trigger refuse_erased before insert or update on {{data}}.data_admins
  for each row execute function {{data}}.refuse_erased('account_id');
create trigger refuse_erased before insert or update on {{data}}.identity_notices
  for each row execute function {{data}}.refuse_erased('account_id');
create trigger refuse_erased before insert or update on {{data}}.person_connections
  for each row execute function {{data}}.refuse_erased('account_id');
create trigger refuse_erased before insert or update on {{data}}.connect_pending
  for each row execute function {{data}}.refuse_erased('account_id');
create trigger refuse_erased before insert or update on {{data}}.grants
  for each row execute function {{data}}.refuse_erased('holder');

-- An erased person's place in the access lists: their refs, memberships and grants go, so no
-- definition names them. A list keeps its key; an id is never reused, so none matches it again.
create function {{data}}.forget_person(account uuid) returns void language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
declare
  who text := 'account:' || account::text;
begin
  perform membership_lock();
  perform set_config('daimon_data.acl_maint', 'on', true);
  delete from acl_grants where holder = who;
  delete from holder_sets where holder = who;
  delete from group_members where subject = who;
  delete from acl_refs where ref = who;
  perform set_config('daimon_data.acl_maint', 'off', true);
  insert into erased_accounts (account_id) values (account) on conflict do nothing;
  perform bump_epoch();
end $$;

-- ---- who a person reads as ---------------------------------------------------------------------

-- A link moves every cached audience.
create trigger acl_epoch_link after insert on {{data}}.identities for each row
  when (new.account_id is not null) execute function {{data}}.bump_epoch_trigger();
create trigger acl_epoch_link_change after update on {{data}}.identities for each row
  when (old.account_id is distinct from new.account_id or old.active is distinct from new.active
        or old.proof is distinct from new.proof) execute function {{data}}.bump_epoch_trigger();
create trigger acl_epoch_unlink after delete on {{data}}.identities for each row
  when (old.account_id is not null) execute function {{data}}.bump_epoch_trigger();

-- The subjects linked to an account in these tenants: active ones, and a person's own sign-in
-- whatever the listing says (it proves itself).
create function {{data}}.linked_subjects(account uuid, tenant_ids uuid[]) returns setof text
  language sql stable set search_path = {{data}}, pg_catalog, pg_temp as $$
  select provider || '/' || workspace || ':user:' || subject from identities
  where account_id = account and tenant_id = any (tenant_ids) and (active or proof = 'oauth')
$$;

-- The holders each reader stands for, by the reader's position: replaces 0001's one-argument
-- version. A reader is `account:<uuid>` (itself, '*' and its linked subjects in `tenant_ids`)
-- or a platform subject (itself and '*'). Anything else resolves to nobody.
create function {{data}}.reader_subjects(readers jsonb, tenant_ids uuid[])
  returns table (ord int, holder text) language sql stable
  set search_path = {{data}}, pg_catalog, pg_temp as $$
  with r as (select v, o::int as ord from jsonb_array_elements_text(readers) with ordinality t(v, o)),
  known as (select ord, v, v ~ '^account:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' as person
            from r
            where v ~ '^account:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               or v ~ '^[^/:[:space:]]+/[^/:[:space:]]+:[^:[:space:]]+:[^[:space:]]+$')
  select ord, v from known
  union select ord, '*' from known
  union select k.ord, s from known k cross join lateral linked_subjects(substr(k.v, 9)::uuid, tenant_ids) s
    where k.person
$$;

-- 0001's acls_for, reading each reader's holders in the audience's tenants only.
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

drop function {{data}}.reader_subjects(jsonb);
