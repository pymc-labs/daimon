-- Idempotent, run by every apply once 0001_store.sql is in: the grants a restore with
-- --no-privileges drops, and the parts that wait for an optional extension.

-- Embeddings, where the vector extension is installed (now or by a later `daimon data init`).
-- A side table, so the store has one shape with or without it; a vector reads as its chunk.
do $$
declare ns text := (select extnamespace::regnamespace::text from pg_extension where extname = 'vector');
begin
  if ns is not null and to_regclass('{{data}}.chunk_vectors') is null then
    execute format('create table {{data}}.chunk_vectors (
      chunk_id uuid primary key references {{data}}.chunks on delete cascade,
      embedding %I.vector not null)', ns);
    alter table {{data}}.chunk_vectors enable row level security;
    create policy visible on {{data}}.chunk_vectors for select
      using (exists (select 1 from {{data}}.chunks c where c.id = chunk_id));
  end if;
end $$;

-- The BM25 index, where pg_textsearch is installed. Built here only while chunks is empty:
-- on a populated table a plain build would hold writes, so `daimon data reindex` builds it.
do $$
begin
  if exists (select 1 from pg_extension where extname = 'pg_textsearch')
     and to_regclass('{{data}}.chunks_bm25') is null and not exists (select 1 from {{data}}.chunks) then
    execute 'create index chunks_bm25 on {{data}}.chunks using bm25 (text) with (text_config = ''english'')';
  end if;
exception when others then
  raise notice 'data store: no BM25 index: %', sqlerrm;
end $$;

-- Functions run only as the store's owner, except the readers' definers below.
revoke all on all functions in schema {{data}} from public;
grant execute on function {{data}}.visible_sources(), {{data}}.scopes(), {{data}}.audience_key(),
  {{data}}.visible_acls(), {{data}}.lapsed_record_ids(), {{data}}.visible_fraction() to daimon_data_reader;

-- A reader sees content through the policies; never a source's config or credential.
revoke all on {{data}}.sources, {{data}}.records, {{data}}.chunks from daimon_data_reader;
grant select (id, tenant_id, kind, name, provider, default_scope, status, publication, created_at)
  on {{data}}.sources to daimon_data_reader;
grant select on {{data}}.records, {{data}}.chunks to daimon_data_reader;
do $$
begin
  if to_regclass('{{data}}.chunk_vectors') is not null then
    revoke all on {{data}}.chunk_vectors from daimon_data_reader;
    grant select on {{data}}.chunk_vectors to daimon_data_reader;
  end if;
end $$;
