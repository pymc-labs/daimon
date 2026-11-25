# Data sources

The data store keeps connected sources and their synced content in their own
Postgres schemas, apart from Daimon's tables. It is off by default and does nothing
until you turn it on and set it up.

## Set it up

Every release migration creates the `data` and `data_views` schemas, the store's tables
and four cluster-wide roles, `daimon_data_reader`, `daimon_data_sql`, `daimon_data_view_owner`
and `daimon_data_parity`, whether or not the feature is on. Nothing reads them until you
set `DAIMON_FEATURES__DATA_SOURCES=on` on every Daimon service; a workspace can then turn
the [`data_sources` feature](features.md#data-sources) off for itself.

If the database user lacks CREATE on the database or CREATEROLE, the migration logs
`data_store_skipped` and Daimon starts anyway. With the feature on, run
`daimon data init` as that user, or with `--admin-url` for one that can.

`daimon data init` is safe to run again. Settings are
[`DAIMON_DATA__*`](configuration.md#daimon_data__schema_name); set
`DAIMON_DATA__SCHEMA_NAME` before the first start, since another name is another,
empty store.

## Sync

The `data-sync` service in `docker-compose.data.yml` keeps sources in step with their
systems. It is a separate process that idles unless the feature is on, and only one worker
schedules at a time, so you can run more than one.

```bash
docker compose -f docker-compose.yml -f docker-compose.data.yml up -d --build data-sync
```

A source syncs every hour, or every `sync_minutes` of its config. A run cut off by its
time budget continues on the next pass, and a failing source backs off from 5 minutes to 6
hours. A source can also limit its upstream requests with `rate_per_minute`. The worker
needs `DAIMON_CRYPTO__KEYS` to open source credentials.

### External Postgres

A source of kind `postgres` reads a view of another database. Give it a read-only login
role, not a superuser or one that bypasses row security, and a credential
`{"dsn": "postgresql://..."}`. The source's config:

```json
{"view": "app.documents", "kind": "document",
 "columns": {"id": "id", "title": "name", "body": "text", "updated_at": "changed_at"},
 "readers": {"column": "reader_ids", "workspace": "acme", "kind": "user"}}
```

`id`, `title`, `body` and `updated_at` are required (a column of another name goes in
`columns`); `occurred_at`, `url`, `author` and `container` are optional. The view's own
filters and row security decide what Daimon sees. Without `readers`, the source's scope
rules apply; with it, the column holds who may read each row. A row that leaves the view is
removed after the next complete run, and a view that suddenly lists nothing is treated as a
failure instead.

Connections to private addresses are refused. To reach a database on your own network,
list its host name or address range in `DAIMON_DATA__PRIVATE_HOSTS`.

## People and their accounts

A record a source shares with some of its users reads for a Daimon account only through a
link to one of those users. Daimon links an account only on a proof the source or chat
platform verified: the person's own chat account, a GitHub link, Sign in with Slack or
Microsoft, or the person signing in to the source.

A data admin can link someone else's account, never their own. The person gets a notice
and the audit trail records it. An email the source and the person both verified makes a
suggested link, and a source can link those itself for one email domain. A Microsoft email
never counts, since a directory user can set it.

Google sign-in for data sources needs a Google OAuth client in
[`DAIMON_DATA__GOOGLE_OAUTH_CLIENT_ID`](configuration.md#daimon_data__google_oauth_client_id).

## Vector search

Embedding search needs the `vector` extension. The default Postgres image lacks it, and
the store then falls back to full-text search. To use it, build the image from
`deploy/postgres` (Postgres 18 with pgvector) through the compose override:

```bash
docker compose -f docker-compose.yml -f docker-compose.data.yml up -d --build postgres
```

It keeps the data volume, so an existing database carries over. Run
`daimon data init` afterwards to create the extension.

## After a restore

A restore with `--no-privileges`, as `scripts/backup/postgres.sh` does, drops the
store's grants and view ownership. Run `daimon data init --repair` on the recovered
database. To script it, set `DAIMON_AFTER_RESTORE` to a command the restore runs at
its end; the backup image has `psql` but no `daimon` command.

A dump that holds the `vector` extension restores only onto an image that has it.
