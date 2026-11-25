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

A database off loopback needs `sslmode=require` or stronger in the DSN, and `require` is
added when it is unset. Connections to private addresses are refused. To reach a database on your own network,
list its host name or address range in `DAIMON_DATA__PRIVATE_HOSTS`.

### Discord

A source of kind `discord` reads one server with a bot of its own, never Daimon's chat bot. Create
an application, add a bot, and turn on the Server Members intent. Invite the bot with View
Channel and Read Message History, and add Manage Threads to read private threads. The
credential is `{"token": "<bot token>"}`. The source's config:

```json
{"workspace": "123456789012345678", "exclude": ["staff-only", "123456789012345679"],
 "rewalk_days": 7}
```

`workspace` is the server id. `exclude` lists channel or thread ids or names to leave out.
A day's messages in a channel or thread become one record, split past 20,000 characters.
Messages from the last `rewalk_days` days (7 by default) are read again once per sync cycle,
so edits and deletes show up.

A record is readable by the members whom the server lets read its channel, worked out from
roles and permission overwrites. A private thread is readable by its members who can read
the channel, and by those who manage threads. The bot lists every human member as an account,
without email addresses. Attachments are listed on the record and fetched on demand.

### Slack

A source of kind `slack` reads one workspace with its own bot token, never Daimon's chat
bot. Create an app with the scopes `channels:read`, `channels:history`, `users:read` and
`files:read`, add `groups:read` and `groups:history` for private channels, and install it.
Invite the bot to each channel to read. The optional `users:read.email` scope lists people
with their confirmed email addresses. The credential is `{"token": "xoxb-..."}`. The
source's config:

```json
{"workspace": "T0123456789", "private": "yes", "exclude": ["random"], "rewalk_days": 30}
```

`workspace` is the team id, and the token must belong to it. `private` adds the private
channels the bot is in. `exclude` lists channel ids or names to leave out. A channel's
messages become one record per day, and a thread becomes records of its own, rebuilt when it
changes. Messages from the last `rewalk_days` days (30 by default) are read again once per
sync cycle.

A public channel is readable by the workspace's active members and by the guests in it. A
private channel is readable by its active members. Direct messages and group direct messages
are never read. Deactivated people, bots and people from other workspaces read nothing.

### Google Drive

A source of kind `gdrive` reads the files of a Google Workspace domain's shared drives and,
when asked, its My Drives. It acts through a service account with domain-wide delegation:

1. Create a service account and enable the Drive and Admin SDK APIs in its project.
2. In the Workspace admin console, under domain-wide delegation, authorize the account's
   client ID for `https://www.googleapis.com/auth/drive.readonly`,
   `https://www.googleapis.com/auth/admin.directory.user.readonly` and
   `https://www.googleapis.com/auth/admin.directory.group.readonly`.
3. Give the credential the account's key file as `{"service_account_key": "<the JSON>"}`.
   Without a key, set `service_account` in the config instead; Daimon then signs as its own
   Google runtime identity, which needs the Token Creator role on that account.
4. Make `admin` a super admin or a delegated admin who can read users and groups. For
   shared drives, add that admin to each drive as a manager.

```json
{"domain": "example.com", "admin": "admin@example.com", "my_drives": "yes",
 "exclude_folders": ["1AbC..."], "relist_hours": 24}
```

`drives` lists shared drive IDs (all when empty). `my_drives: yes` also reads the My Drive of
every active person, or only of the emails in `users`. `include_folders` keeps only files
under those folders; `exclude_folders` are never read, with everything below them. `max_mb`
(20) caps the file size read.

Google Docs, Slides and Sheets (first tab) are read as text, and text and Office files through
attachment extraction; other files keep their title only. A file is readable by the people its
sharing names: people of the domain by primary email, groups by their members (read from
the directory by this source), and the whole domain when a share is discoverable. Link-only
shares, outside people and expiring shares grant nothing, and a file nobody inside can read is
not stored. Changes arrive through each scope's change feed. Everything is listed again every
`relist_hours`, which is also how soon a new share on a folder reaches the files below it.

A person's read is checked in Drive again when a record is shown: a file Drive no longer opens
to them is left out, even before the next sync. Each request checks a limited number of files
and shows only those that pass.

### Google Calendar

A source of kind `gcal` is live: it stores nothing and is created with mode `federated`. A
search reads the asker's own primary calendar, acting as them through the same domain-wide
delegation as Drive (add `https://www.googleapis.com/auth/calendar.readonly` to the
authorized scopes, and the Calendar API to the project), so an event shows only to the people
Google lists on it. Give the credential and config as for Drive (`service_account_key` or
`service_account`; `domain`, `admin`). People are matched through their Google link, which a
Drive source of the same domain (or Google sign-in) provides.

```json
{"domain": "example.com", "admin": "admin@example.com"}
```

With several readers an event shows only if every one of their calendars has it. A reader the
source cannot act as (not linked, suspended) gets nothing. Events of the last 90 days and the
future are searched.

### GitHub

A source of kind `github` reads an organization's issues, pull requests, discussions and
repository files. Give the credential `{"token": "..."}`, a fine-grained token (or an App
installation token) for the organization with read access to contents, issues, pull requests,
discussions and metadata, to administration (to list collaborators) and to organization
members.

```json
{"org": "your-org", "content": ["issues", "pulls", "discussions", "files"],
 "file_paths": ["docs", "README.md"], "repos": ["site"], "relist_hours": 168}
```

`content` defaults to issues and pull requests (with their conversation comments);
`file_paths` lists the files or folders of markdown and text files to read when `files` is
on; `repos` limits the repositories (all when empty). A record is readable by the people
GitHub lists as able to read its repository (collaborators with read access: owners, team
members, direct and outside collaborators), plus every organization member while the
repository is public or internal. The list is read again every run and each record is checked
against it when shown, so a repository made private or a person removed takes effect at once.
A repository whose collaborators cannot be listed publishes nobody, and its records go dark
within three hours. GitHub verifies no address: a person who signs in with GitHub is linked in every
organization that lists their id; a data admin links anyone else. Each run reads what changed; every `relist_hours` it reads everything, which is
how a deleted or transferred issue goes. Review comments and reviews are not read.

### GitHub markdown

A source of kind `github_markdown` mirrors the markdown and text files of one repository, one
record each, readable by the whole of the scope the source is created with. The credential
is `{"token": "..."}`, a fine-grained token with read access to the repository's contents.

```json
{"repo": "your-org/notes", "folders": ["docs", "notes"], "branch": "main"}
```

`folders` lists files or folders to read (every text file when empty). A run compares the head
commit with the last one read, reads only the changed files and removes those that are gone.
The source's `redaction_set` rules are applied as the files are stored.

### Notion

A source of kind `notion` reads the pages and databases that an internal integration can
see. Create the integration with read content access, share the pages to read with it, and
give the credential `{"token": "secret_..."}`. The source's config:

```json
{"workspace": "example", "exclude": ["0a1b2c3d-0000-0000-0000-000000000000"]}
```

The integration only discovers pages: it grants nobody anything. A page is readable by the
people who have signed in to Notion through Daimon (their own sign-in, each time Notion must
still accept their token), and by nobody else. `exclude` lists page or database ids whose
whole tree is stored but never shown. A page that disappears from a complete listing is
removed. A page Notion refuses to give keeps what it had.

### Bluedot

A source of kind `bluedot` reads meeting summaries and transcripts. It has no credential:
each person signs in to Bluedot with their own account, and Daimon reads with their
sign-in. A meeting of a collection is readable by the people who list that collection. A
private meeting is readable by the people who list it, its uploader and the people it is
shared with. A meeting nobody signed in lists is not stored, and one is removed only by a
run in which every sign-in listed completely. The source's config:

```json
{"workspace": "ws_123", "domain": "example.com"}
```

`workspace` lets Daimon list the workspace's participants as accounts. Addresses on `domain`
count as verified when linking accounts.

### LinkedIn

A source of kind `linkedin` reads an organization's published, public posts, readable by
the whole tenant (create the source with the `public` scope). The credential is
`{"token": "..."}`, an access token that can read the organization's posts
(`r_organization_social`). The config is `{"organization": "urn:li:organization:12345"}`.
Every run reads all posts, so a post deleted on LinkedIn, or no longer public, disappears.
Drafts, connection-only and sponsored posts are never stored. A run that finds no posts for
an organization that had some fails and removes nothing.

### Toggl Track

A source of kind `toggl` reads a workspace's time entries, projects and clients. Give the
credential `{"token": "..."}`, the API token of a workspace admin (it reads the workspace's
members, project memberships, projects, clients and the time entry report; no rate, amount,
cost or currency is kept).

```json
{"workspace": "1234567", "history_from": "2022-01-01"}
```

`workspace` is the numeric workspace id; `history_from` is the first day to read (default
2010-01-01). An entry is readable by its author while their account is active, by every
owner and admin, and by the managers of its project; projects and clients by owners and
admins. Project lead, group roles and public projects grant nothing. Toggl verifies no
address, so a data admin links each member by hand.

Each run reads the entries changed since
the last, a daily pass re-reads the last year and a weekly one everything from
`history_from`; an entry or project deleted in Toggl goes with the pass that covers it.

Created with mode `federated`, the same kind is read live: nothing is stored and a search
reads the last 90 days from Toggl, showing each reader only what they may read. A reader who
is not linked, or whose account is inactive, gets nothing; with several readers an entry
shows only if every one may read it.

### Linear

A source of kind `linear` is live: it stores nothing and is created with mode `federated`.
Give the credential `{"token": "..."}`, a personal API key (read access is enough). A search
reads issues, and their descriptions, with that key. There is no per-issue access list: a
caller whose scopes include the source's scope reads every issue the key sees, so give the
source the narrowest scope that fits.

### HubSpot

A source of kind `hubspot` is live and scope-gated like Linear. Give the credential
`{"token": "..."}`, a private app token with read access to the CRM objects to search
(companies, deals, contacts, notes) and to pipelines.

```json
{"objects": ["companies", "deals"], "portal_id": "12345"}
```

`objects` limits the types searched (all four when empty); `portal_id` links each result to
HubSpot. Results carry the record's own fields and its first associated company.

## People and their accounts

A record a source shares with some of its users reads for a Daimon account only through a
link to one of those users. Daimon links an account only on a proof the source or chat
platform verified: the person's own chat account, a GitHub link, Sign in with Slack or
Microsoft, or the person signing in to the source (Bluedot and Notion).

A data admin can link someone else's account, never their own. The person gets a notice
and the audit trail records it. An email the source and the person both verified makes a
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
Integrations > Data sources, and manage scopes, grants, identity links, data admins and
redaction sets under Admin > Data access. Declared sources show as bound and are read-only
there. People link and connect their own accounts on their Profile.

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
over `data_views`. That SQL runs as `daimon_data_sql`, which reaches the views and nothing
else, so it never sees an access list, a credential or another workspace's rows. Like any
role, it can read the system catalogs, so it can see roughly how many rows each of the
store's tables holds across every workspace, though not what they say.

## Evals and probes

`daimon data eval GOLD...` runs a gold set (JSON or YAML) through search. Each question names
its caller (`tenant_id`, and an `account_id` or explicit `scopes` and `readers`) and the
records that answer it (`source` name or id, `external_id`, a `grade` of 1 to 3). The search
runs as that caller, and recall and nDCG at 5 and 10 are scored for the fused list and for
each arm (`fts`, `vec`, `time`, read from the hits' ranks before any rerank) on the expected
records the caller can read; a record they cannot read is not a miss, and a question with
none readable is left out and counted. The estimated spend prints first and the run refuses
above `--max-usd`; it stops once the metered spend reaches that cap. Query spend bills the
caller's tenant. `--out` writes the JSON report; the text summary prints either way.

`daimon data probe NAME --tenant ID` runs one read-only check and prints JSON: `grants` (each
scope's holders), `scopes` (records by source and scope, and those in a scope nobody defined),
`person` (`--account`: what that person holds and reads), `health` (each source's state, last
sync and embedding coverage) and `drift` (chunks off their record, records without chunks,
sources not synced in 48 hours or failing). Probes never write the store.

## After a restore

A restore with `--no-privileges`, as `scripts/backup/postgres.sh` does, drops the
store's grants and view ownership. Run `daimon data init --repair` on the recovered
database. To script it, set `DAIMON_AFTER_RESTORE` to a command the restore runs at
its end; the backup image has `psql` but no `daimon` command.

A dump that holds the `vector` extension restores only onto an image that has it.
