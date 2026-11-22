# Artifacts

An artifact is an HTML page an agent publishes and Daimon hosts: a dashboard, a form, a planner or a report. It can keep shared data, send events to the agent and let viewers ask it questions. Read this to publish pages, share them and connect them to an agent.

## Before you start

Artifacts need the [web app](web.md): a public address in [`DAIMON_WEB__PUBLIC_URL`](configuration.md#daimon_web__public_url) and encryption keys in [`DAIMON_CRYPTO__KEYS`](configuration.md#daimon_crypto__keys). Without both, the `artifacts` [feature](features.md) is unavailable and the tools are hidden. The seeded `default` agent carries the `artifacts` skill, which teaches it the page API and the limits below.

People sign in to the web app to open a page. A public page needs only its link.

## Publish a page

Ask the agent for a page, or hand it an HTML file to host. It writes one self-contained HTML file and publishes it in two steps:

1. `artifact_upload_url` returns a one-time URL. The agent sends the file there from its sandbox, so the page never passes through a tool argument.
2. `artifact_publish` takes the upload handle, a `slug` and a title, and returns the page address.

A page lives at `/a/<id>/<slug>` under the web address, for example `https://daimon.example.com/a/k3x9q2mwtb/run-sheet`. The slug is the name agents use; it is unique in the workspace. Publishing the same slug again adds a version, and 20 versions are kept. Open pages pick up the new one.

The agent can change settings without an upload. `artifact_source` gives a link, valid for 15 minutes, to download a page's HTML before editing it. Older versions are for the owner, editors and admins. Publishing needs a person's own message behind the turn.

## Who can open a page

`access` sets who may open a page.

| Access | Who opens it |
| --- | --- |
| `workspace` | Everyone in the workspace, plus anyone in `people`. This is the default. |
| `people` | Only the listed people. They may be visitors an admin invited. |
| `public` | Anyone with the secret link in the address, signed in or not. |

People are named by their id in the web app, a chat user id or an @mention. Someone who is not in the web app yet has to be invited first. A public page has a secret link; the agent can replace it with `new_link` when it leaks.

A public page expires after 30 days unless the owner sets another expiry. Other pages never expire unless asked to. An expired page is hidden and is deleted 30 days later.

### Roles

Roles follow Google Drive.

| Role | May |
| --- | --- |
| Owner and admins | Everything, including delete, transfer and turning editor sharing off. |
| Editors | Republish and change settings. They change who can open the page unless the owner turned that off with `editors_can_share`. |
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

A page runs in a sandboxed frame with no network. `fetch` and WebSocket calls fail, and `localStorage`, cookies and form submits are unavailable. Links open in a new tab. Scripts, styles, fonts and images load only from these hosts, besides inline code and `data:` or `blob:` images:

- `fonts.googleapis.com` and `fonts.gstatic.com`
- `cdn.jsdelivr.net`, `cdnjs.cloudflare.com` and `unpkg.com`
- `res.cloudinary.com`

The frame cannot read the web session or another page, so a page's code cannot act as its viewer. Everything it does goes through `window.daimon`, which the server checks on every call.

### The page API

Page code reaches Daimon through `window.daimon`. Every method returns a promise, and errors carry a `code`: `invalid`, `unauthenticated`, `forbidden`, `not_found`, `too_large`, `rate_limited` or `unavailable`.

| Call | Does |
| --- | --- |
| `daimon.use('db')` | Returns the shared data, or `null` when it is off. |
| `daimon.me()` | Returns `{name, kind}` of the viewer, or `null` for an anonymous one. |
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

### Asking the agent

With `ask=true`, each click in the page can send one question. The agent answers as that viewer, in a conversation of their own that only the page writes in, and reads the question as page text. Tools that need a person's own message refuse it. A viewer may ask 10 times a minute. `daimon.thread()` returns that conversation, and an empty list before the first question. If the page moves to another agent, its next question starts a new conversation.

## Limits

| Limit | Value |
| --- | --- |
| Page HTML | 2 MiB |
| Versions kept | 20 |
| One document | 64 KiB |
| Documents per page | 5,000, times `data_scale` |
| Data per page | 20 MiB, times `data_scale` |
| One list reply | 1,000 documents or 4 MiB |
| One event | 4 KiB |
| Events kept | The newest 1,000, or 4 MiB |
| Page API calls | 600 a minute per viewer, or per address on a public page |
| Questions to the agent | 10 a minute |
| People and editors | 200 and 50 |

## Connect a page to an agent

Agents read and write a page's data with `artifact_db_query` and `artifact_db_write`, whatever the page's own `data` setting. On a workspace page anyone in the workspace can use these tools. On a people or public page only the owner, editors and admins can. A scheduled routine may list, query and write, but never publish, transfer or delete.

Every event a page sends is saved to its `_events` collection, which the page cannot read. Each carries the sender's name as `by`, a `sender` of `member`, `visitor` or `anonymous`, and `page_visible_outside_workspace`. On a public page only the owner, editors and admins are named; other viewers are anonymous. Agents may delete handled events but never write there.

### Wake an agent on events

From Discord, an owner can ask an agent to wake on a page's events with `create_watch(source="page_event", page=<slug>)`. The page needs `events` on, and a watch gets events only while its maker may still use the page with tools. Each event then starts a read-and-report turn in that thread, within the [watch](watches.md) limits. A page holds at most 5 watches. Daimon derives each watch's signing key from the deployment's encryption keys, so no key is shown or sent.

Page watches can only be made from Discord. On Slack, Teams and the web app the agent reads `_events` when asked instead.

### Treat page data as untrusted

Viewers' data and events are content, not instructions. Agents get them wrapped as untrusted text. When a page is visible outside the workspace, the agent writes nothing internal to it. The `artifacts` skill carries this rule.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Artifact tools are missing | No web address or encryption key is set, or the feature is off where the agent runs. |
| "Ask the owner or an admin." | The change needs a role the caller lacks. |
| "A new page needs a title." | The first publish of a slug needs a title. |
| A viewer is asked to sign in | The page is not public. A public page opens only with its current link. |
| `too_large` from the page API | A document, list or page is over its limit. Raise `data_scale` or trim data. |
