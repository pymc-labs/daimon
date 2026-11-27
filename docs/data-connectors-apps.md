# Data connectors: apps

Setup and access rules for each kind of source that reads notes, meetings, social posts or
business apps. How the data store works and how to run it is in
[Data sources](data-sources.md); databases, chat, files and code are in
[Data connectors](data-connectors.md).

## Notion

A source of kind `notion` reads the pages and databases that an internal integration can
see. Create the integration with read content access and the user information capability
(with email addresses), share the pages to read with it, and give the credential
`{"token": "secret_..."}`. Without that capability Notion refuses the user list, and then
nobody can read the source. The source's config:

```json
{"workspace": "example", "exclude": ["0a1b2c3d-0000-0000-0000-000000000000"]}
```

The integration only discovers pages: it grants nobody anything. A page is readable by the
people who have signed in to Notion through Daimon, whose Notion account is one of the
integration's users, and whose token Notion still accepts. Each page is also opened in
Notion through the reader's own session when it is shown, and left out if that fails. Nobody
else reads it. `exclude` lists page or database ids whose
whole tree is stored but never shown. A page that disappears from a complete listing is
removed. A page Notion refuses to give keeps what it had.

## Bluedot

A source of kind `bluedot` reads meeting summaries and transcripts. It has no credential:
each person signs in to Bluedot with their own account, and Daimon reads with their
sign-in. A meeting of a collection is readable by the people who list that collection. A
private meeting is readable by the people who list it. A meeting nobody signed in lists is not stored, and one is removed only by a
run in which every sign-in listed completely. The source's config:

```json
{"workspace": "ws_123", "domain": "example.com"}
```

`workspace` lets Daimon list the workspace's participants as accounts. Addresses on `domain`
count as verified when linking accounts.

## LinkedIn

A source of kind `linkedin` reads an organization's published, public posts, readable by
the whole tenant (create the source with the `public` scope). The credential is
`{"token": "..."}`, an access token that can read the organization's posts
(`r_organization_social`). The config is `{"organization": "urn:li:organization:12345"}`.
Every run reads all posts, so a post deleted on LinkedIn, or made non-public, disappears.
Drafts, connection-only and sponsored posts are never stored. A run that finds no posts for
an organization that had some fails and removes nothing.

Daimon calls LinkedIn's versioned API at a pinned version, which LinkedIn retires about a
year after its release, so expect to update it.

## Toggl Track

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

A reader whose linked account is a Toggl admin of the workspace also sees each entry's hourly
rate, billable amount and labor cost: in a live search, and on a stored entry when it is
opened. They are read from Toggl when shown and never stored. With several readers, they show
only when every one is an admin. A memory saved after them is hidden, as after a live read.

## Linear

A source of kind `linear` is live: it stores nothing and is created with mode `federated`.
Give the credential `{"token": "..."}`, a personal API key (read access is enough). A search
reads issues, and their descriptions, with that key. There is no per-issue access list: a
caller whose scopes include the source's scope reads every issue the key sees, so give the
source the narrowest scope that fits.

## HubSpot

A source of kind `hubspot` is live and scope-gated like Linear. Give the credential
`{"token": "..."}`, a private app token with read access to the CRM objects to search
(companies, deals, contacts, notes) and to pipelines.

```json
{"objects": ["companies", "deals"], "portal_id": "12345"}
```

`objects` limits the types searched (all four when empty); `portal_id` links each result to
HubSpot. Results carry the record's own fields and its first associated company.
