-- Redaction rules: named rule sets a source applies as it ingests, and the lists that hide names
-- from readers who must not see them. {{data}} is the store's schema.
--
-- Inert until used: no source names a set, and no person is marked.

-- A set is inline (rules from the declared file) or a pinned repo file. A pinned file counts only
-- at its approved blob sha: when the sha upstream differs, `pending` holds the new rules and
-- every source using the set stops syncing until an admin approves that sha.
create table {{data}}.redaction_sets (
  tenant_id uuid not null,
  name text not null check (name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
  rules jsonb,                     -- the approved rules; null: none approved yet
  repo text,                       -- `owner/name` of a pinned file (null: inline rules)
  path text,
  ref text,
  token_ref text,                  -- `env:NAME` or `file:/path` for a private repo, never a value
  pin_sha text,                    -- the file's blob sha as last fetched
  approved_sha text,
  declared_sha text,               -- the sha the declared file last asked to approve
  pending jsonb,                   -- rules of the file at `pin_sha` while that is not approved
  checked_at timestamptz,
  error text,                      -- why the last fetch failed
  approved_by uuid,
  approved_at timestamptz,
  primary key (tenant_id, name),
  check ((repo is null) = (path is null))
);
revoke all on {{data}}.redaction_sets from public;

-- The set a source applies at ingest, by name in its tenant.
alter table {{data}}.sources add column redaction_set text;

-- A person whose reads always hide what a guest's would.
alter table {{data}}.identities add column redacted boolean not null default false;

-- 0005's, with the source's rule set: changing it moves the generation, and a bound source's set
-- changes only in the declared apply.
create or replace function {{data}}.source_generation() returns trigger language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
begin
  if (new.kind, new.provider, new.config, new.credential, new.default_scope, new.mode, new.redaction_set)
     is distinct from (old.kind, old.provider, old.config, old.credential, old.default_scope, old.mode,
                       old.redaction_set) then
    new.generation := old.generation + 1;
  end if;
  return new;
end $$;

-- Deleting a bound row needs the declared apply or erasure (`ports.erasure` sets
-- daimon_data.erasing). Pause, unpublish and retire stay guarded in the app (`manage.act`).
create or replace function {{data}}.guard_bound() returns trigger language plpgsql
  set search_path = {{data}}, pg_catalog, pg_temp as $$
begin
  if tg_op = 'DELETE' then
    if old.bound and coalesce(current_setting('daimon_data.declared', true), '') <> 'on'
       and coalesce(current_setting('daimon_data.erasing', true), '') <> 'on' then
      raise exception 'a % row is managed by the deployment', tg_table_name using errcode = '42501';
    end if;
    return old;
  end if;
  if not old.bound or coalesce(current_setting('daimon_data.declared', true), '') = 'on' then
    return new;
  end if;
  if tg_table_name = 'sources' then
    if (new.tenant_id, new.kind, new.name, new.provider, new.config, new.credential, new.default_scope,
        new.mode, new.fresh_floor_minutes, new.fresh_ceiling_minutes, new.bound, new.declared_key,
        new.redaction_set)
       is distinct from
       (old.tenant_id, old.kind, old.name, old.provider, old.config, old.credential, old.default_scope,
        old.mode, old.fresh_floor_minutes, old.fresh_ceiling_minutes, old.bound, old.declared_key,
        old.redaction_set)
       or (new.status = 'disabled' and old.status <> 'disabled') or old.publication = 'retired'
          and new.publication <> 'retired' then
      raise exception 'source % is managed by the deployment', old.id using errcode = '42501';
    end if;
  elsif to_jsonb(new) - 'created_at' is distinct from to_jsonb(old) - 'created_at' then
    raise exception 'a % row is managed by the deployment', tg_table_name using errcode = '42501';
  end if;
  return new;
end $$;
create trigger guard_bound_delete before delete on {{data}}.sources
  for each row execute function {{data}}.guard_bound();
create trigger guard_bound_delete before delete on {{data}}.scopes
  for each row execute function {{data}}.guard_bound();
create trigger guard_bound_delete before delete on {{data}}.grants
  for each row execute function {{data}}.guard_bound();

-- Sources whose reads a connector re-checks live (`recheck`): caller SQL cannot be re-checked,
-- so its context names them and no read in that transaction sees them.
alter table {{data}}.read_contexts add column hidden_sources uuid[] not null default '{}';
create or replace function {{data}}.visible_sources() returns setof uuid language sql stable security definer rows 10
  set search_path = {{data}}, pg_catalog, pg_temp as $$
  select s.id from sources s join read_contexts c on s.tenant_id = any (c.tenant_ids)
  where c.pid = pg_backend_pid() and c.xact = pg_current_xact_id_if_assigned()
    and s.publication = 'published' and s.status <> 'disabled' and source_fresh(s.id)
    and s.id <> all (c.hidden_sources)
$$;

-- Linked subjects honour the listing for every proof: an account the source deactivated
-- holds nothing, a sign-in included.
create or replace function {{data}}.linked_subjects(account uuid, tenant_ids uuid[]) returns setof text
  language sql stable set search_path = {{data}}, pg_catalog, pg_temp as $$
  select provider || '/' || workspace || ':user:' || subject from identities
  where account_id = account and tenant_id = any (tenant_ids) and active
$$;
