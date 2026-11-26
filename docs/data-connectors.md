# Data connectors

Setup and access rules for each kind of source that reads a database, chat, files or code.
How the data store works and how to run it is in [Data sources](data-sources.md); sources
for notes, meetings and business apps are in [Data connectors: apps](data-connectors-apps.md).

## External Postgres

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

## Discord

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

## Slack

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

## Google Drive

A source of kind `gdrive` reads the files of a Google Workspace domain's shared drives, and
lets each person have their own My Drive read as them. It acts through a service account
with domain-wide delegation:

1. Create a service account and enable the Drive and Admin SDK APIs in its project.
2. In the Workspace admin console, under domain-wide delegation, authorize the account's
   client ID for `https://www.googleapis.com/auth/drive.readonly`,
   `https://www.googleapis.com/auth/admin.directory.user.readonly` and
   `https://www.googleapis.com/auth/admin.directory.group.readonly`.
3. Give the credential the account's key file as `{"service_account_key": "<the JSON>"}`.
   Without a key, Daimon signs as its own Google runtime identity, which needs the Token
   Creator role on the account. Set `service_account` in the config, and have the deployment
   list the account for the Daimon workspace in `DAIMON_DATA__GOOGLE_KEYLESS_ACCOUNTS`, as
   `<workspace>=<account>` entries where the workspace is a tenant id or `<platform>:<id>`.
   An account not listed for the source's workspace is refused, whatever domain the source
   names, and with no entry no source can sign without a key.
4. Make `admin` a super admin or a delegated admin who can read users and groups. For
   shared drives, add that admin to each drive as a manager.

```json
{"domain": "example.com", "admin": "admin@example.com", "personal_drive": "yes",
 "exclude_folders": ["1AbC..."], "relist_hours": 24}
```

`drives` lists shared drive IDs (all when empty). `include_folders` keeps only files
under those folders; `exclude_folders` are never read, with everything below them. `max_mb`
(20) caps the file size read.

Google Docs, Slides and Sheets (first tab) are read as text, and text and Office files through
attachment extraction; other files keep their title only. A file is readable by the people its
sharing names: people of the domain by primary email, and groups by their members (read from
the directory by this source or another of its group). A discoverable share grants the whole
domain. Link-only shares, outside people and expiring shares grant nothing, and a file
nobody inside can read is not stored.

A source group is the Google sources of one workspace whose config names the same `group`,
one per Google connection. Group membership read by any of them counts for all of them. A
source with no `group`, or another one, never uses another source's membership.

Changes arrive through each shared drive's change feed. Everything is listed again every
`relist_hours`, which is also how soon a new share on a folder reaches the files below it.

A person's read is checked in Drive again when a record is shown: a file Drive no longer opens
to them is left out, even before the next sync. Each request checks a limited number of files
and shows only those that pass.

### Personal Drive

My Drives are never stored. A person linked to the domain can ask the agent to search their
own My Drive (`data_my_drive`); it reads as them, returning the text of Docs, Slides and
Sheets. Every read shows a card only they can answer, saying that everyone in the
conversation will see the result; **Allow for this conversation** stops asking for the rest
of that conversation (kept in the running process: a restart asks again).

It works in direct messages and rooms wherever data reads are on. `personal_drive: no` turns
it off for the workspace's source; the `data_personal_drive` feature (on by default) turns it
off for a workspace, agent or channel.

Each person can also turn off their own Drive and calendar reads, on their profile page or
in chat (`data_personal_reads`, on a card). Their Drive is then never read as them, and no
calendar is searched for an answer they are part of.

## Google Calendar

A source of kind `gcal` is live: it stores nothing and is created with mode `federated`. A
search reads the asker's own primary calendar, acting as them through the same domain-wide
delegation as Drive, so an event shows only to the people Google lists on it. Add
`https://www.googleapis.com/auth/calendar.readonly` to the authorized scopes, and the
Calendar API to the project. Give the credential and config as for Drive (`service_account_key` or
`service_account`; `domain`, `admin`). People are matched through their Google link, which a
Drive source of the same domain (or Google sign-in) provides.

```json
{"domain": "example.com", "admin": "admin@example.com"}
```

With several readers an event shows only if every one of their calendars has it. A reader the
source cannot act as (not linked, suspended) gets nothing. Events within 90 days either side of
now are searched, and those nearest to now come first.

## GitHub

A source of kind `github` reads an organization's issues, pull requests, discussions and
repository file paths. It signs in as Daimon's [GitHub App](github.md#permissions) and reads
the repositories the workspace connected in that organization, needing no credential. Without
the App, give the credential `{"token": "..."}`, a fine-grained token for the organization
with read access to contents, issues, pull requests, discussions and metadata, to
administration (to list collaborators) and to organization members.

```json
{"org": "your-org", "content": ["issues", "pulls", "discussions", "files"],
 "file_paths": ["docs", "README.md"], "repos": ["site"], "relist_hours": 168}
```

`content` defaults to issues and pull requests (with their conversation comments);
`file_paths` lists the files or folders whose paths are stored when `files` is on (file
contents are never read or stored); `repos` limits the repositories (all when empty).

A record is readable by the people GitHub lists as able to read its repository
(collaborators with read access: owners, team members, direct and outside collaborators),
plus every organization member while the repository is public or internal.

The list is read again every run and each record is checked
against it when shown, so a repository made private or a person removed takes effect at once.
A repository whose collaborators cannot be listed publishes nobody, and its records go dark
within three hours. A repository the source no longer lists is hidden at once; listed again
within 30 days its records show again without a re-read, after that they are deleted.

GitHub verifies no address: a person who signs in with GitHub is linked in every
organization that lists their id, and a data admin links anyone else. Each run reads what
changed; every `relist_hours` it reads everything, which is how a deleted or transferred
issue goes. Review comments and reviews are not read.
