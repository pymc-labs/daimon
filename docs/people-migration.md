# Move people in

Two operator commands bring people's choices and earlier conversations from
another bot into one Discord server. Both read a version 1 JSON file of at most
1 MiB, check that its `guild_id` is the server of `--tenant`, and run in one
transaction. Repeating a file is safe.

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
daimon dms import dms.json --tenant TENANT_UUID
```

The file holds each person's earlier turns, `user` or `assistant`, in any
order:

```json
{"version": 1, "guild_id": "123456789012345678", "source_name": "Helper",
 "exported_at": "2026-03-01T12:00:00Z",
 "users": [{"external_user_id": "111",
            "turns": [{"role": "user", "text": "Hi", "at": "2026-03-01T10:00:00Z"}]}]}
```

A person's first private conversation here starts from theirs without saying so. The latest twelve turns, within 16,000
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
daimon watches import watches.json --tenant TENANT_UUID \
  --agent-id AGENT_ID --agent-name NAME --urls-out urls.json
daimon watches confirm WATCH_UUID TENANT_UUID OWNER_ACCOUNT_UUID AGENT_ID PARENT_CHANNEL_ID THREAD_ID
daimon watches confirm-timers --tenant TENANT_UUID --agent-id AGENT_ID --agent-name NAME
```

The file lists at most 25 watches, plus `timers`. A callback watch keeps its
token's SHA-256 and also gets a new private link. The links go only to
`--urls-out` (mode 0600), as `{source id: url}`, merged with an earlier file;
they are never printed. Set `DAIMON_WATCHES__CALLBACK_BASE_URL` first. Owners
without an identity get one.

The file must name the server (`guild_id`) of
`--tenant`, and the agent must belong to it. A callback watch ends at its old
expiry; a permanent one or one past the `callback_watch_lifetime` setting (30 days by
default) ends that long after it is confirmed, or never when the setting is
unlimited, and past ones are skipped. An owner already at the live-watch limit is skipped
and counted, not an error. Rerunning issues no second link; a watch nobody has
confirmed is updated from the file.

Until a watch is confirmed, a post to either of its links answers a retryable
`503`, so senders try again; a link nobody holds still gets the same `403`. An
import nobody confirms lapses 30 days after it was imported.
Entries and timers marked `paused` stay paused when confirmed: a watch waits for
its owner to resume it, and a timer is never scheduled.

Imported watches start paused and report to the owner's private conversation.
Confirm each after checking its owner, channel and thread, which the import
prints. `confirm` and `confirm-timers` check that the agent is an unarchived agent
of the tenant, which must be a live Discord server. A timers-only `import` that is
given `--agent-id` and `--agent-name` checks them too.

Timers are held, not scheduled. `confirm-timers` schedules each one that is
still more than a minute away, once. Reminders set in a private conversation
fire in that person's private messages, ones set in a thread fire in it and
one-off reminders set in a plain channel fire in that channel. A channel reminder
runs unattended, as its owner, and only if the agent can still post there and the
owner can still see it; otherwise it is settled with a reason and nothing is sent.

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
cancels held timers the file no longer lists.

```bash
daimon watches unpause --tenant TENANT_UUID --all-timers   # or --timer-id, --watch-id
daimon watches renew WATCH_UUID --tenant TENANT_UUID       # 30 more days
daimon watches rotate-link WATCH_UUID --tenant TENANT_UUID --urls-out link.json
```

`unpause` releases timers or a confirmed watch that were paused in the old
system. `rotate-link` replaces every link of a callback watch and shows the new
one once, or writes it to `--urls-out` (mode 0600).
