# Data sources

The data store keeps connected sources and their synced content in their own
Postgres schemas, apart from Daimon's tables. It is off by default and does nothing
until you turn it on and set it up.

## Set it up

The data store needs PostgreSQL 16 or later.

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
hours. A source makes at most 60 upstream requests a minute unless its config sets
`rate_per_minute` (0 removes the limit). The worker needs `DAIMON_CRYPTO__KEYS` to open
source credentials.

## Kinds of source

Setup and access rules for each kind are on two pages: [databases, chat, files and
code](data-connectors.md), and [notes, meetings, social and business
apps](data-connectors-apps.md).

## People and their accounts

A record a source shares with some of its users reads for a Daimon account only through a
link to one of those users. Daimon links an account only on a proof the source or chat
platform verified: the person's own chat account, a GitHub link, Sign in with Slack or
Microsoft, or the person signing in to the source (Bluedot and Notion).

A data admin can link someone else's account, never their own. The person gets a notice
and the audit trail records it. A link a data admin ends or rejects stays ended when the
person signs in again. An email the source and the person both verified makes a
suggested link, and a source can link those itself for one email domain. A Microsoft email
never counts, since a directory user can set it.

Google sign-in for data sources needs a Google OAuth client in
[`DAIMON_DATA__GOOGLE_OAUTH_CLIENT_ID`](configuration.md#daimon_data__google_oauth_client_id).

## Freshness

A source has a freshness floor and ceiling in minutes. Its lifetime is three sync intervals,
at least the floor and at most the ceiling. Once its last complete sync is older than that,
its records are hidden from every read until the next complete sync. Without a ceiling a
source is never hidden.

A connector can also check a record with the source at read time, and a source that
cannot is read as stored. A live source is asked at search time instead: it stores nothing,
only callers holding its scope reach it, and one that fails or is slow is skipped.

## Publishing

A new source starts in shadow, where no one reads it. Publishing it needs evidence. If the
connector can ask the source whether a person may open a record, Daimon compares a sample
of linked people and records and needs a clean run no older than 36 hours. Any case of Daimon
showing more than the source does blocks publishing; showing less only warns. Other sources
publish after a complete sync.

## In the web app

With the feature on, the web app has a Data section for search and browsing, which shows each
person only what they may read. Owners and admins add, pause, publish and retire sources under
Integrations > Data sources. Data admins, named by the owner or another data admin, manage
scopes, grants, identity links and redaction sets under Admin > Data access, but never grant
themselves a scope. Declared sources show as bound and are read-only there. People link and
connect their own accounts on their Profile.

## Declared config

Set `DAIMON_DATA__DECLARED` to a YAML or JSON file to declare sources, scopes and grants
for each workspace. Daimon applies it at start and every 10 minutes.

```yaml
revision: 4
tenants:
  - workspace: discord:<server id>
    scopes: [{id: shared:eng, kind: shared, name: Engineering}]
    sources:
      - {id: wiki, kind: postgres, name: Wiki, credentials: {dsn: "env:WIKI_DSN"}, publish: true}
```

A `workspace` is Daimon's workspace id, or `<platform>:<id>` with the platform's own id for
it, so one file serves every deployment; one not found is skipped. Declared rows are
read-only in the app. Daimon never deletes one: a removed entry stays, and
`retire: true` retires a source for good. A source is created in shadow and published only
with `publish: true` and a clean run. `replaces` retires the sources it names, by declared id or name. A `revision`
below the last applied one is ignored. Credentials are `env:NAME` or `file:/path`, never values.

## Who can read a record

`daimon data access-check --tenant <id> --source <id>` shows a source's state and freshness.
Add `--record <id>` for the record's holders, and `--account <id>` to say whether that account
can read it now, and why not.

## Vector search

Embedding search needs the `vector` extension. The default Postgres image lacks it, and
the store then falls back to full-text search. To use it, build the image from
`deploy/postgres` (Postgres 18 with pgvector) through the compose override:

```bash
docker compose -f docker-compose.yml -f docker-compose.data.yml up -d --build postgres
```

It keeps the data volume, so an existing database carries over. Run
`daimon data init` afterwards to create the extension.

Set `DAIMON_DATA__EMBED_URL` and `DAIMON_DATA__EMBED_MODEL` (and `_KEY`) to an
OpenAI-compatible embeddings API, and the sync worker embeds new chunks after each run and
once a day for gaps. `DAIMON_DATA__EMBED_DIM` is the vector length kept, and the HNSW index
is built over it. `DAIMON_DATA__EMBED_PROVIDER` names the providers an OpenRouter-style
router may use, comma-separated, or `openai` for OpenAI's own API; unset or `openai` sends
a plain request. Usage rows name `openai`, or `selfhosted` for any other.
`DAIMON_DATA__EMBED_QUERY_PREFIX` goes before a question when it is embedded.

Embedding and reranking are billed like any model call. The usage rows are
`data_embedding` and `data_rerank`, priced from the model's entry in the rate table
(`text-embedding-3-small`, `text-embedding-3-large`, `qwen/qwen3-embedding-8b`,
`perplexity/pplx-embed-v1-0.6b`, `semantic-ranker-fast-005`, one query per 100 records
reranked); a model without an entry is never called. Ingest is billed
to the source's workspace, a question to whoever asked. A workspace out of balance gets
full-text answers only, and its new chunks wait to be embedded until it tops up.

## Search and reading

Search combines full text, BM25 (where `pg_textsearch` is installed), vectors and, when the
question names a date ("last week", "in March"), a time window, fused by reciprocal rank.
`DAIMON_DATA__RERANK=vertex` and `DAIMON_DATA__RERANK_PROJECT` add the Vertex AI Ranking
API on the best candidates, using the host's cloud identity. `DAIMON_DATA__SEARCH_SECONDS`
bounds a search; it returns what the arms have found when time runs out, and notes what was
cut.

Callers browse a source's records, read one with its text and files, and run read-only SQL
over `data_views`. That SQL runs as `daimon_data_sql`, which reaches the views only: no access
list, credential or other team's rows. Like any role it reads the system catalogs, so it sees
rough row counts of the store's tables, not what they say. SQL leaves out sources that check
reads with the source. While names are hidden from someone the answer is for, files and SQL
are refused, and a search naming a hidden name finds nothing.

## Evals and probes

`daimon data eval GOLD...` runs a gold set (JSON or YAML) through search. Each question names
its caller (`tenant_id`, and an `account_id` or explicit `scopes` and `readers`) and the
records that answer it (`source` name or id, `external_id`, a `grade` of 1 to 3). The search
runs as that caller.

Recall and nDCG at 5 and 10 are scored for the fused list and for each arm (`fts`, `vec`,
`time`, read from the hits' ranks before any rerank). Only records the caller can read count.
An unreadable record is not a miss, and a question with none readable is left out and counted.
The estimated spend prints first and the run refuses above `--max-usd`. It stops once the
metered spend reaches that cap. Query spend bills the caller's tenant. `--out` writes the
JSON report; the text summary prints either way.

`daimon data probe NAME --tenant ID` runs one read-only check and prints JSON. It never
writes the store. The checks:

- `grants`: each scope's holders.
- `scopes`: records by source and scope, and those in a scope nobody defined.
- `person` (`--account`): what that person holds and reads.
- `health`: each source's state, last sync and embedding coverage.
- `drift`: chunks off their record, records without chunks, sources not synced in 48 hours or failing.

## After a restore

A restore with `--no-privileges`, as `scripts/backup/postgres.sh` does, drops the
store's grants and view ownership. Run `daimon data init --repair` on the recovered
database. To script it, set `DAIMON_AFTER_RESTORE` to a command the restore runs at
its end; the backup image has `psql` but no `daimon` command.

A dump that holds the `vector` extension restores only onto an image that has it.
