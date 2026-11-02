# Watches

A watch reads a source on a schedule and wakes an agent only when the result
changes. Polling itself spends no AI turn. Read this to turn watches on, choose
what they may read and manage them for a deployment.

Watches are off by default. Polling works for Discord `read_thread` and
`read_channel`, and for read tools on MCP servers an operator lists. Signed
page-event callbacks are accepted but not yet produced by any host, so
`create_watch(source="page_event")` is refused.

## How a watch works

An owner asks an agent in an attended conversation to call `create_watch` with
a tool, its arguments, a projection, a rule and an interval. The owner approves
a card that names the host and tool. The first poll saves a baseline and wakes
nobody.

Later polls compare the projected value with the baseline:

| Rule | Fires when |
| --- | --- |
| `change` | the value is new and not empty or false (zero counts) |
| `added` | an array gains elements; `key` such as `.rows[].id` compares by that field |
| `updated` | an object gains or changes a key |

Removals alone never fire. With `added`, the optional `evidence` list (up to 8
field paths) picks what each new item reports. Projections accept `.`,
`.field.nested`, `.rows[0].content` and one `[]` step such as
`.rows[].content`.

A firing queues a turn that may only read and report. Shell, built-in tools,
skills, management tools and writes are unavailable. Every model call,
approval card and report rechecks the owner's access, balance, monthly cap and
channel budget.

## Where a watch reports

`delivery` picks one of three places. The owner and recipient always come from
the conversation, never from the request.

- **This thread**, the default.
- **The owner's DM** (`delivery="dm"`, Discord only). Each report runs as a
  fresh DM turn under the same read-only rules.
- **A new public thread per report** (`delivery="new_thread"`, Discord only).
  Daimon checks the owner may create threads in the parent.

Turned-off DMs or lost thread rights pause the watch.

## Manage watches

Owners use `list_watches`, `show_watch`, `pause_watch`, `resume_watch`,
`preview_watch_edit`, `update_watch`, `rotate_watch_key` and `delete_watch`.
Creating, resuming, editing and rotating a key need an approval card from the
owner, even with tool safety off.

An edit shows each change as old and new on its card. Queued reports from
before it are dropped, and the next poll saves a new baseline. Admins may
show, pause or delete another owner's watch, and `list_watches(scope="all")`
lists the whole server. Only the owner can resume or edit.

A watch lasts 1 to 30 days, with at most 25 live watches per owner. An owner
who is an admin can make up to 5 watches permanent with `permanent=true`. A
permanent watch paused for 30 days expires.

A watch pauses and tells its owner privately when:

- the owner loses access to the source or destination;
- its tool is taken off the deployment's list (`tool_unlisted`);
- an MCP server's listing of the tool changes (`source_changed`);
- an owner key it needs is missing (`credential_missing`);
- it fails at least five times in a row over at least 24 hours (`failed`).

Errors back off from the interval up to one day. Pausing, editing, rotating a
key or deleting cancels queued reports.

## Choose what can be polled

[`DAIMON_WATCHES__POLLABLE_TOOLS`](configuration.md#daimon_watches__pollable_tools)
is the only thing that allows a poll. Each entry names a `server`, a `tool`, a
`credential`, `returns_chat` and an optional `max_interval_floor_s` and
`label`.

- `server` is `daimon-mcp` for Daimon's own readers, otherwise an `https` MCP
  server URL. URLs are compared in canonical form, never by server name.
- Native entries must be `read_thread` or `read_channel` with credential
  `none`.
- `agent` polls with the agent's stored key for that URL. `owner` polls with
  the owner's own read-only key, sent once through the private
  `request_watch_credential` form.

> **Warning:** A server can say a tool only reads and still write. Its
> read-only hint can refuse a poll but never allow one, so list only tools you
> trust.

The default lists `read_thread` and `read_channel` with a 60-second floor. An
empty value means the default, and `[]` allows no polls. Name tools that
return chat or archive text in
[`DAIMON_WATCHES__CHAT_TOOLS`](configuration.md#daimon_watches__chat_tools),
so they are never polled on an MCP server.

Discord reads must name a source other than the reporting thread. Bot and
assistant messages are ignored, so a report never triggers its own watch. The
agent needs the read enabled without approval in its Daimon toolset. An MCP
server poll needs the server still attached at the same URL with the tool
enabled.

## Limits

| Limit | Value |
| --- | --- |
| Interval | the listed floor (at least 60 s) to 86,400 s |
| Reports per day | 24 per watch, 100 per owner, raised up to 240 and 1,000 |
| Raw read | 256 KiB, depth 32, 20,000 JSON nodes, never stored |
| Saved baseline | 64 KiB |
| One report | 16 KiB and 20 items, each clipped to 1 KiB |
| Evidence kept | 7 days |
| MCP server load | 2 polls at once and 30 a minute per URL |

An owner who is an admin can ask for more than 24 reports a day with
`max_turns_per_day`, up to the ceiling. A capped watch keeps polling and fires
again when older firings leave the rolling day. The owner gets one message per
day about it.

Operators change the ceilings for every watch or one tenant:

```bash
daimon watches limits set --scope global --per-watch 48 --per-owner 200
daimon watches limits set --scope tenant:TENANT_UUID --per-watch 2
daimon watches limits show --tenant TENANT_UUID
```

## Turn watches on

Watches need the `sidecar` and `watch-executor` services, run from the same
image as the others, after migrations.

1. Provision a database login granted only `daimon_sidecar`, with no
   ownership or schema rights. Set it in
   [`DAIMON_SIDECAR__DATABASE_URL`](configuration.md#daimon_sidecar__database_url),
   with TLS across hosts. Startup refuses a login with more privileges.
2. Set a random 32 to 256 byte secret as
   [`DAIMON_WATCHES__EXECUTOR_SECRET`](configuration.md#daimon_watches__executor_secret)
   on the executor and as `DAIMON_SIDECAR__EXECUTOR_SECRET` on the sidecar.
3. Keep the executor private on port 8767, with no published port or proxy
   route. For MCP server watches it needs outbound HTTPS to the listed URLs.
4. Put the sidecar behind a TLS proxy that exposes only the callback paths.
5. Set [`DAIMON_WATCHES__ENABLED`](configuration.md#daimon_watches__enabled)
   to `true`, then turn on the database `global` gate.

To run the executor outside Compose:

```bash
uvicorn daimon.adapters.mcp.watch_executor:create_watch_executor_app \
  --factory --host 127.0.0.1 --port 8767 --no-access-log
```

Both gates must be on before any poll runs. Turning off either stops new
reports, and turning off the database gate also cancels queued ones.
Re-enabling does not bring them back. Do not enable proxy, header or body
logging on the sidecar.

## Operate watches

These commands are read-only:

```bash
daimon watches list --tenant TENANT_UUID --json
daimon watches notices --watch WATCH_UUID --json
```

`list` shows each watch's owner, status, source, destination, expiry and
firings in the past day. It never prints instructions, keys, baselines or
evidence. `notices` shows each private owner notice attempt and its Discord
message id, never the text.

A notice that fails retries after 5, 10, 20 and 40 minutes, then hourly, and
is dropped after a day. A retry first looks for the earlier message, so a
restart never sends one twice. A notice Discord will never deliver, such as
to a closed DM, is recorded as `refused` and not retried. Receipts are kept 30
days.

## Import watches from another system

`daimon watches import` loads a reviewed JSON manifest of at most 1 MiB. It
reads only the file. Every import starts paused and unconfirmed, and
rerunning it is idempotent.

```bash
daimon watches import /private/watches.json
daimon watches confirm WATCH_UUID TENANT_UUID OWNER_ACCOUNT_UUID AGENT_ID PARENT_CHANNEL_ID THREAD_ID
```

Each entry carries `version: 1` fields for the source, tool, arguments,
projection, rule, interval, expiry and an `os_binding` naming the owner and
exact destination. Map those to verified Daimon identities. Never export
signing keys, saved credentials, leases or unfinished wakes.

`discord_read_messages` maps to `read_thread`, for thread ids only.
`discord_read_channel_since` maps to `read_channel` and drops `since`. Other
tools import as `source_unmapped`, cannot be resumed, and their owner gets a
private notice. Confirm a watch only after checking every binding field.
