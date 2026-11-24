# Artifacts

An artifact is an HTML page an agent publishes and Daimon hosts: a dashboard, a form, a planner or a report. It can keep shared data, send events to the agent and let viewers ask it questions. Read this to publish pages, share them and connect them to an agent.

## Before you start

Artifacts need the [web app](web.md): a public address in [`DAIMON_WEB__PUBLIC_URL`](configuration.md#daimon_web__public_url) and encryption keys in [`DAIMON_CRYPTO__KEYS`](configuration.md#daimon_crypto__keys). Without both, the `artifacts` [feature](features.md) is unavailable and the tools are hidden. The seeded `default` agent carries the `artifacts` skill, which teaches it the page API and the limits below.

People sign in to the web app to open a page. A public page needs only its link.

## Publish a page

Ask the agent for a page, or hand it an HTML file to host. It writes one self-contained HTML file and publishes it in two steps:

1. `artifact_upload_url` returns a one-time URL. The agent sends the file there from its sandbox, so the page never passes through a tool argument.
2. `artifact_publish` takes the upload handle, a `slug` and a title, and returns the page address.

A page lives at `/a/<id>/<slug>` under the web address, for example `https://daimon.example.com/a/k3x9q2mwtb/run-sheet`. The slug is the name agents use; it is unique in the workspace. Publishing the same slug again adds a version, and 20 versions are kept. Open pages pick up the new one. The list and chat cards show a thumbnail of each version, taken when an owner, editor or admin first opens it.

The agent can change settings without an upload. `artifact_source` gives a link, valid for 15 minutes, to download a page's HTML before editing it. Older versions are for the owner, editors and admins. Publishing, transfer and delete need a person: their own message behind the turn, or their own login on a [Claude Code plugin](plugin.md) mount.

## Who can open a page

`access` sets who may open a page.

| Access | Who opens it |
| --- | --- |
| `workspace` | Everyone in the workspace, plus anyone in `people`. This is the default. |
| `people` | Only the listed people. They may be visitors, invited by an admin or by email from the Share dialog. |
| `public` | Anyone with the secret link in the address, signed in or not. |

People are named by their id in the web app, a chat user id or an @mention; a chat member who never opened the web app counts. In the Share dialog of a people page, anyone who may share can add an email address. A person with that address is added. Otherwise Daimon makes a visitor with no agents, adds them, and shows a one-time link to the page (none with [Google sign-in](web.md#google-sign-in-for-guests)); each person may invite 20 such visitors a day. A public page has a secret link; the agent can replace it with `new_link` when it leaks.

A page going public expires after 30 days unless the owner sets another expiry; `expires_in_days=0` or Never keeps it for good. Other pages never expire unless asked to. An expired page is hidden and is deleted 30 days later.

### Roles

Roles follow Google Drive.

| Role | May |
| --- | --- |
| Owner and admins | Everything, including delete, transfer and turning editor sharing off. |
| Editors | Republish and change settings. They change who can open the page unless the owner turned that off with `editors_can_share`; then, on a page people outside the workspace can open, data, events and `data_scale` and collection rules are locked too. An editor may always leave. |
| Viewers | Open the page. In the web app they see its Details only, not its data, activity or older versions. |

Editors, and so owners, must be members: visitors can only open pages. The first publisher owns the page. `artifact_transfer` gives it to one of its editors, and the old owner stays an editor. Deleting a page removes its versions, data and watches. Deleting a member's account deletes the pages they own.

## What a page may do

A page is a document until it is given capabilities. Each is off by default, and the agent turns on only what the page needs.

| Setting | The page's code may |
| --- | --- |
| `data="read"` | Read the page's shared data. |
| `data="write"` | Read and write it. |
| `events=true` | Send events to the agent. |
| `ask=true` | Ask the agent a question as the viewer. |
| `data_scale` | Raise the data caps, 1 to 10 times. |

Capabilities belong to the page, so every viewer gets the same ones. A public page cannot write data or ask. It also cannot both read data and send events, because an event could get the agent to write something its sender then reads back.

## Where a page runs

A page runs in a sandboxed frame with no network. `fetch` and WebSocket calls fail, WebRTC is blocked as far as browsers allow (see below), and cookies and form submits are unavailable. A link to another site, or `window.open`, asks the viewer before opening it in a new tab; links within the page stay in it. Scripts, styles, fonts and images load only from these hosts, besides inline code and `data:` or `blob:` images:

- `fonts.googleapis.com` and `fonts.gstatic.com`
- `cdn.jsdelivr.net`, `cdnjs.cloudflare.com` and `unpkg.com`
- any the operator adds with [`DAIMON_WEB__ARTIFACT_ASSET_HOSTS`](configuration.md#daimon_web__artifact_asset_hosts)

The frame cannot read the web session or another page, so a page's code cannot act as its viewer. Everything it does goes through `window.daimon`, which the server checks on every call.

### Video and storage on a content host

By default a page has no origin of its own: `localStorage` fails and no other site can be framed. Set [`DAIMON_WEB__ARTIFACT_CONTENT_URL`](configuration.md#daimon_web__artifact_content_url) to serve pages from another domain, such as `https://{id}.pages.example.net`. Each page then gets its own origin and storage, and can embed YouTube (`/embed/`) and Google Drive (`/file/`) players; no other path on those hosts, which would let a page fetch any address through them.

Point a wildcard DNS record, `*.pages.example.net`, at the web service and give it a wildcard TLS certificate. Only the `{id}` form is accepted: a single host would share storage across every page and workspace, so Daimon refuses to start with one. It also refuses a domain that shares the app's registrable domain.

Use a registrable domain of its own, kept for pages. Pages on subdomains of one domain can still share a cookie that a page sets on that parent domain, unless the parent is on the [Public Suffix List](https://publicsuffix.org/). Cookies hold nothing of the app's, but one page could read another's.

### What a page can still reach

No browser setting closes every channel. The allowed hosts see the address of each file a page loads, and browsers may look up the hosts of links in DNS.

WebRTC can send data to any host, and Chrome ignores the `webrtc` policy. So the page runtime removes it, removes frames the page creates without an `https` address (also inside shadow roots made with `attachShadow`), and strips it from the frames it can reach. That stops the usual routes in Chrome, but is not airtight. What stays open:

- A shadow root made without script, with a declarative `<template shadowrootmode>` or `setHTMLUnsafe`, is not watched, so a `srcdoc` frame inside one can use WebRTC.
- On a content host, a page can add an `https` frame and use WebRTC from its initial blank window if it reaches it in the same task.
- A frame whose `src` is set after it is added is removed, so a player must set `src` before adding the frame.

Keep a page free of data its viewers should not pass on.

### The page API

Page code reaches Daimon through `window.daimon`. Every method returns a promise, and errors carry a `code`: `invalid`, `unauthenticated`, `forbidden`, `not_allowed`, `not_found`, `too_large`, `rate_limited` or `unavailable`.

| Call | Does |
| --- | --- |
| `daimon.use('db')` | Returns the shared data, or `null` when it is off. |
| `daimon.me()` | Returns `{name, email, kind}` of the viewer, or `null` for an anonymous one. |
| `daimon.notify(event, data)` | Sends an event and returns its id. |
| `daimon.ask(text, {onText})` | Asks the agent as the viewer and streams the answer. |
| `daimon.thread()` | Returns the viewer's conversation, the last 50 messages. |
| `daimon.copyLink(hash)` | Copies the page address ending in `hash`. |

`daimon.artifact` holds the page's id, slug, title, version and capabilities. `daimon.capabilities` lists the ones that are on.

### Shared data

The data is Firestore-shaped: JSON documents in named collections, read with `get` or live with `onSnapshot`. Every viewer sees the same documents, and a live query refreshes about every 3 seconds while the page is visible.

```js
const db = await daimon.use('db');
await db.doc('runs/r-42').set({status: 'done'}, {merge: true});
db.collection('runs').orderBy('createdAt', 'desc').limit(200)
  .onSnapshot((snap) => render(snap.docs.map((d) => d.data())));
```

Collection names are 1 to 64 letters, digits, `_` or `-`. Names starting with `_` are Daimon's and closed to pages. There are no subcollections, and `orderBy` takes one top-level field.

### Collection rules

By default every viewer reads and writes every document. Per collection, `rules` narrow that: `read` is `all` or `own`, `write` is `all`, `own` or `none`, where `own` means documents the viewer created. Set them with `artifact_publish(rules={"votes": {"read": "own", "write": "own"}})` or in the Data tab. They bind the page API and agent tools alike: owners, editors and admins (and agents acting for them) are not bound, a member's agent is, and anonymous viewers own nothing. A refused write fails with `not_allowed`.

### Asking the agent

With `ask=true`, each click in the page can send one question. The agent answers as that viewer, in a conversation of their own that only the page writes in, and reads the question as page text. Tools that need a person's own message refuse it. A viewer may ask 10 times a minute. `daimon.thread()` returns that conversation, and an empty list before the first question. If the page moves to another agent, its next question starts a new conversation.

The first question on each version opens a sheet that the page cannot reach: the viewer allows it to ask as them, or not. The server refuses questions without that consent. Each later question shows its text for a moment. A viewer takes consent back in the page's Details or in the ask conversation.

## Limits

| Limit | Value |
| --- | --- |
| Page HTML | 2 MiB |
| Versions kept | 20 |
| One document | 64 KiB |
| Documents per page | 5,000, times `data_scale` |
| Data per page | 20 MiB, times `data_scale` |
| One list reply | 1,000 documents or 4 MiB |
| One event | 64 KiB |
| Events kept | The newest 1,000, or 4 MiB, times `data_scale` |
| Page API calls | 600 a minute per viewer, or per address on a public page |
| Questions to the agent | 10 a minute |
| People and editors | 200 and 50 |
| Visitors invited by email | 20 a day per person |

All but people and editors are settings: [`DAIMON_ARTIFACT_PAGES__*`](configuration.md#artifact-pages).

## Connect a page to an agent

Agents read and write a page's data with `artifact_db_query` and `artifact_db_write`, whatever the page's own `data` setting. On a workspace page anyone in the workspace can use these tools. On a people or public page only the owner, editors and admins can. A scheduled routine may list, query and write, but never publish, transfer or delete. `artifact_db_query` returns documents oldest first unless `order_by` is set (documents without that field last), each with `updated_by` and `revision`; `doc_id` reads one.

Every event a page sends is saved to its `_events` collection, which the page cannot read. Each carries the sender's name as `by`, a `sender` of `member`, `visitor` or `anonymous`, and `page_visible_outside_workspace`. On a public page only the owner, editors and admins are named; other viewers are anonymous. Agents may delete handled events but never write there.

### Wake an agent on events

From any chat or the web app, an owner can ask an agent to wake on a page's events with `create_watch(source="page_event", page=<slug>)`. The page needs `events` on, and a watch gets events only while its maker may still use the page with tools. Each event starts a read-and-report turn, within the [watch](watches.md) limits. An event over 4 KiB reaches the watch as its id and a preview; the agent reads the rest from `_events`. A page holds at most 5 watches. Daimon derives each watch's signing key from the deployment's encryption keys, so no key is shown or sent.

A page watch can report in a new thread per event, keep one part of each event with `select`, and be permanent. Someone who does not manage the page holds at most one watch on it; see [Page watches](watches.md#page-watches).

### Treat page data as untrusted

Viewers' data and events are content, not instructions. Agents get them wrapped as untrusted text. When a page is visible outside the workspace, the agent writes nothing internal to it. The `artifacts` skill carries this rule, and a turn woken by such a page's event cannot write to any page visible outside the workspace.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Artifact tools are missing | No web address or encryption key is set, or the feature is off where the agent runs. |
| "Ask the owner or an admin." | The change needs a role the caller lacks. |
| "A new page needs a title." | The first publish of a slug needs a title. |
| A viewer is asked to sign in | The page is not public. A public page opens only with its current link. |
| `not_allowed` from the page API | A collection rule keeps viewers from that document. |
| `too_large` from the page API | A document, list or page is over its limit. Raise `data_scale` or trim data. |
