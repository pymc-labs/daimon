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
