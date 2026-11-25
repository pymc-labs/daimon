-- Sync state: each source's runs and cursor, and the files attached to its records. Rows are
-- the sync worker's own (the store's owner); a reader sees only attachments, through the
-- record they belong to. {{data}} is the store's schema.

create table {{data}}.sync_runs (
  id uuid primary key default gen_random_uuid(),
  source_id uuid not null references {{data}}.sources on delete cascade,
  trigger text not null default 'schedule',
  status text not null default 'running' check (status in ('running', 'ok', 'partial', 'error')),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  added int not null default 0,
  updated int not null default 0,
  deleted int not null default 0,
  error text
);
create index on {{data}}.sync_runs (source_id, started_at desc);

-- Where a source's sync stands: the connector's cursor (saved with each checkpoint's commit),
-- the last complete run, and the failures since. requested_at: a sync asked for.
create table {{data}}.source_cursors (
  source_id uuid primary key references {{data}}.sources on delete cascade,
  state jsonb not null default '{}',
  last_sync_at timestamptz,
  last_status text,
  last_error text,
  requested_at timestamptz,
  errors int not null default 0
);

-- A file attached to a record. The bytes are never kept; text read from them is chunked with
-- the record, and a reader asks the source for the file itself.
create table {{data}}.attachments (
  source_id uuid not null,
  external_id text not null,
  ord int not null,
  name text not null,
  content_type text not null default '',
  size int not null default 0,
  extracted boolean not null default false,
  primary key (source_id, external_id, ord),
  foreign key (source_id, external_id) references {{data}}.records (source_id, external_id) on delete cascade
);

alter table {{data}}.attachments enable row level security;
create policy visible on {{data}}.attachments for select using (
  exists (select 1 from {{data}}.records r
          where r.source_id = attachments.source_id and r.external_id = attachments.external_id));
