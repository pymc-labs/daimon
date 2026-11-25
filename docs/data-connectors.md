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

A source of kind `gdrive` reads the files of a Google Workspace domain's shared drives and,
when asked, its My Drives. It acts through a service account with domain-wide delegation:

1. Create a service account and enable the Drive and Admin SDK APIs in its project.
2. In the Workspace admin console, under domain-wide delegation, authorize the account's
   client ID for `https://www.googleapis.com/auth/drive.readonly`,
   `https://www.googleapis.com/auth/admin.directory.user.readonly` and
   `https://www.googleapis.com/auth/admin.directory.group.readonly`.
3. Give the credential the account's key file as `{"service_account_key": "<the JSON>"}`.
   Without a key, Daimon signs as its own Google runtime identity, which needs the Token
   Creator role on the account. Set `service_account` in the config, and list the account
   for the domain in `DAIMON_DATA__GOOGLE_KEYLESS_ACCOUNTS` (`example.com=sa@project.iam.gserviceaccount.com`).
   An account the deployment does not list is refused, and with no entry no source can sign
   without a key.
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
not stored.

Changes arrive through each scope's change feed. A person's feed never removes a file, since
another scope may hold it; a shared drive's feed does. Everything is listed again every
`relist_hours`, which is also how soon a new share on a folder reaches the files below it
and how soon a deleted My Drive file goes.

A person's read is checked in Drive again when a record is shown: a file Drive no longer opens
to them is left out, even before the next sync. Each request checks a limited number of files
and shows only those that pass.

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
on; `repos` limits the repositories (all when empty).

A record is readable by the people GitHub lists as able to read its repository
(collaborators with read access: owners, team members, direct and outside collaborators),
plus every organization member while the repository is public or internal. The list is read
again every run and each record is checked
against it when shown, so a repository made private or a person removed takes effect at once.
A repository whose collaborators cannot be listed publishes nobody, and its records go dark
within three hours.

GitHub verifies no address: a person who signs in with GitHub is linked in every
organization that lists their id, and a data admin links anyone else. Each run reads what
changed; every `relist_hours` it reads everything, which is how a deleted or transferred
issue goes. Review comments and reviews are not read.

## GitHub markdown

A source of kind `github_markdown` mirrors the markdown and text files of one repository, one
record each, readable by the whole of the scope the source is created with. The credential
is `{"token": "..."}`, a fine-grained token with read access to the repository's contents.

```json
{"repo": "your-org/notes", "folders": ["docs", "notes"], "branch": "main"}
```

`folders` lists files or folders to read (every text file when empty). A run compares the head
commit with the last one read, reads only the changed files and removes those that are gone.
The source's `redaction_set` rules are applied as the files are stored.
