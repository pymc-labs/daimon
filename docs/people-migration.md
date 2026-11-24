# Move people in

Operator commands bring people's choices, earlier conversations and pages from
another bot into one Discord server. Each reads version 1 JSON files, checks that
the `guild_id` is the server of `--tenant`, and repeating a file is safe. Tag opt-outs
and conversations take one file of at most 1 MiB in one transaction;
[pages](#pages) take a folder.

## Tag opt-outs

```bash
daimon tags import opt-outs.json --tenant TENANT_UUID
```

The file lists the Discord ids of people who never want to be tagged:

```json
{"version": 1, "guild_id": "123456789012345678", "discord_ids": ["111", "222"]}
```

Each person is opted out as if they had asked Daimon never to tag them, and gets an identity
if they have none yet. The command prints how many were new and how many had
already opted out.

## Earlier private conversations

```bash
daimon dm import-history dms.json --tenant TENANT_UUID --agent AGENT_NAME
```

The file holds each person's earlier turns, `user` or `assistant`, in any
order:

```json
{"version": 1, "guild_id": "123456789012345678", "source_name": "Helper",
 "exported_at": "2026-03-01T12:00:00Z",
 "users": [{"external_user_id": "111",
            "turns": [{"role": "user", "text": "Hi", "at": "2026-03-01T10:00:00Z"}]}]}
```

A person's first private conversation here with `--agent`, which must be one of
the tenant's agents, starts from theirs without saying so. One with another agent
leaves it waiting. The latest twelve turns, within 16,000
characters, are the conversation's history. Older turns, within 8,000
characters, are quoted as untrusted context. The conversation waits with no
expiry and is used once; a new conversation after that starts empty, and
importing the file again never brings it back.

People who already have a private conversation here get theirs at their next
`/new`, in the same way, and the command reports them. A person who appears in
several files has their turns merged when the files come from one export, and
the command fails when they do not, so one history never replaces another.
Erasing a person's data also erases a conversation still waiting for them.

## Watches and reminders

```bash
daimon watches import watches-1.json watches-2.json --tenant TENANT_UUID \
  --agent-id AGENT_ID --agent-name NAME --urls-out urls.json
daimon watches go-live --tenant TENANT_UUID --agent-id AGENT_ID --agent-name NAME
```

Each file lists at most 25 watches, plus `timers`. Give every file of one export
in the same run. Each watch commits on its own, so watch ingress waits at most
for one; a rerun finishes a run that stopped. A callback watch keeps its
token's SHA-256 and also gets a new private link. The links go only to
`--urls-out` (mode 0600), as `{source id: url}`, merged with an earlier file;
they are never printed. Set `DAIMON_WATCHES__CALLBACK_BASE_URL` first. Owners
without an identity get one.

Every file must name the server (`guild_id`) of
`--tenant`, and the agent must belong to it. A callback watch ends at its old
expiry, and one already past it is skipped. One with no expiry, or longer than the
`callback_watch_lifetime` setting (30 days by default), ends that long after it
goes live; with the setting at `0` it is permanent. An owner already at the live-watch limit is skipped
and counted, not an error. Rerunning issues no second link; a watch that has not
gone live is updated from the file.

Until a watch goes live, a post to either of its links answers a retryable
`503`, so senders try again; a link nobody holds still gets the same `403`. An
import that never goes live lapses 30 days after it was imported.
Entries and timers marked `paused` stay paused at `go-live`: a watch waits for
its owner to resume it, and a timer is never scheduled.

Imported watches start paused and report to the owner's private conversation.
Check each owner, channel and thread, which the import prints, then run
`go-live`. It checks that the agent is an unarchived agent of the tenant, which
must be a live Discord server. An `import` with timers or callback watches
needs `--agent-id` and `--agent-name` and checks them too. Held timers belong to
that agent: only its `go-live` schedules them and only its `--full` run cancels
them. `daimon watches confirm` is removed; use `go-live`.

`go-live` makes every watch imported for that agent live on the owner, channel
and thread the import printed: polled and callback watches resume. A page watch
first needs a fresh signing key from its owner. A watch that cannot go live, such
as one with an unmapped source, is listed with the reason and stays paused. Rerunning `go-live` is safe and picks up what was
fixed since.

Timers are held, not scheduled. `go-live` schedules each one that is still more
than a minute away, once; one already due is skipped. Reminders set in a private
conversation fire in that person's private messages, ones set in a thread fire in it and
one-off reminders set in a plain channel fire in that channel. A channel reminder
runs unattended, as its owner, and only if the agent can still post there and the
owner can still see it; otherwise it is settled with a reason and nothing is sent.

Reminders in a private conversation or a plain channel fire only for an agent on
Managed Agents. A plain channel's own backend setting, when it has one, wins over the
agent's. For the others `go-live` lists them by name, counts them as `not_scheduled`,
schedules none and leaves them held for a rerun.

Channel reminders whose channel id is not numeric are counted. Any past the 25
pending a person may hold are skipped. Notes and instructions over their limit
are cut and counted; an empty note is fine. The command prints a count per
outcome. A settled timer keeps no text.

A reminder that cannot start because Discord answers with a server error, a rate limit
or a network failure is offered again, up to five times. Once its turn has run, sending
is retried in place a few times and a reminder that still fails is settled: the turn
never runs twice. Any other Discord error settles it at once.

Rerun the import after fixing the file: held timers that nothing has scheduled
are updated. A rerun never pauses again what `unpause` released. With `--full` it also
cancels held timers that no file of the run lists. An export split over several
files may list its timers in only one, so `--full` refuses a run whose files list
no timers.

A privacy purge erases the held rows of the person, but importing the same files
again restores them. Delete the import files, and the `--urls-out` file, after
use.

Reminders and callback watches made natively in a private conversation: the first
deploy settles the reminders already due there, which never fired before, so none
fires at once. A callback watch made in a Discord private conversation reports there.

```bash
daimon watches unpause --tenant TENANT_UUID --all-timers   # or --timer-id, --watch-id
daimon watches renew WATCH_UUID --tenant TENANT_UUID       # 30 more days
daimon watches rotate-link WATCH_UUID --tenant TENANT_UUID --urls-out link.json
```

`unpause` releases timers or a watch that went live paused because they were
paused in the old system; rerun `go-live` to schedule released timers.
`rotate-link` replaces every link of a callback watch and shows the new
one once, or writes it to `--urls-out` (mode 0600), keyed by the old id as
`import --urls-out` is.

## Pages

```bash
daimon artifacts import DIR --tenant TENANT_UUID --agent NAME          # dry run
daimon artifacts import DIR --tenant TENANT_UUID --agent NAME --apply
```

`DIR` holds one `.json` file per page, each at most 64 MiB, and `NAME` is the agent
the pages belong to. Without `--apply` the whole run happens in a transaction that is
rolled back. It prints, per page, whether it would be created, replaced, refused or
skipped as expired, how many members and visitors it would hold, and what would not
work here. It never prints HTML, data or links. With `--apply` each page commits on
its own, and the command exits 1 if any page was refused.

A file is `{"version": 1, "guild_id", "exported_at", "page": {...}}`. Unknown fields are refused.
An export marked `"provisional": true` has ids that are not final: it previews, and `--apply`
refuses it.
In `page`:

- Required: `legacy_url`, `public_id` (10 characters, `a-z2-7`), `slug`, `title`, `created_at`,
  `updated_at`, `owner`, `access` (`workspace`, `people` or `public`), `current_version`, and
  `versions` (`version`, `html`, `created_at`, optional `published_by`).
- Optional: `editors`, `editors_can_share`, `people`, `link_secret`, `expires_at`, `data`
  (`off`, `read` or `write`), `events`, `data_scale`, `data_revision`.
- `documents`: `collection`, `doc_id`, `data`, `updated_by`, `updated_by_ref`, `created_at`,
  `updated_at`. The `_events` collection holds sent events.
- `watches`: `legacy_id`, `instructions`, `owner`, `channel_id`, `thread_id`, `place_type`,
  `delivery`, `select`, `where`, `max_turns_per_day`, `expires_at`, `paused`.

A person is `{"discord_id"}` or `{"email"}`. An email may carry `google_sub`, the Google
account the visitor signs in with.

The page keeps its `public_id`, `slug` and `link_secret`, so existing links work. The
secret is stored encrypted like any page link; a public page without one gets a new link,
and a secret on any other page is ignored. Discord ids become people here, made when
missing. A person known by email becomes a visitor invited by the page's owner, and
`google_sub` pins their Google sign-in unless another person here holds it. Only the
newest versions the deployment keeps are imported, and a version larger than the HTML
limit refuses the page.

`legacy_url` is the page's address in the old system, and the page's `imported_from`.
A rerun finds the page by it and replaces its versions, data, people and imported
watches in place, so its threads and the settings made here stay. A page whose name or
`public_id` is taken by another page, or that was imported under another name, is refused,
as is one whose owner or editor is an [outsider](artifacts.md#who-can-open-a-page): only members edit a page.

A watch is imported as a page watch and ends any callback watch imported with the same
`legacy_id`. One in a thread with no live conversation reports in a new thread instead.
Watches in other places, on a page that sends no events, or past five on one page are
skipped with a warning, as is a daily limit above the workspace's. `where` is an
[event filter](watches.md#page-watches). A `select` that is not a projection path such as
`.data.item` imports the watch paused and without it.

The dry run also warns about what the page loads from hosts [this deployment blocks](artifacts.md#where-a-page-runs),
frames that need a content domain, browser storage without one, and links to
pages outside the import. To send old links to the new pages, list the old host in
[`DAIMON_WEB__ARTIFACT_LEGACY_HOSTS`](configuration.md#daimon_web__artifact_legacy_hosts).

## Repository memory

`scripts/import_repo_memory.py` imports another system's repository memory; its
docstring holds the input formats and steps. With `--tenant`, a creator with no
account here gets a Discord identity in that tenant. The image has no `scripts/`,
so copy the script and its inputs into a worker container and run it there:

```bash
docker cp scripts/import_repo_memory.py WORKER:/tmp/
docker exec WORKER python /tmp/import_repo_memory.py --snapshot /tmp/export.json \
  --repos /tmp/repos.json --tenant TENANT_UUID --manifest /tmp/manifest.json
```

It is a dry run until you add `--apply`.
