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

A person's first private conversation here starts from theirs, and Discord says
"Continuing your earlier conversation." The latest twelve turns, within 16,000
characters, are the conversation's history. Older turns, within 8,000
characters, are quoted as untrusted context. The conversation waits with no
expiry and is used once; a new conversation after that starts empty, and
importing the file again never brings it back.

People who already have a private conversation here are skipped, and the
command reports them. Erasing a person's data also erases a conversation still
waiting for them.

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
without an identity get one. Watches end at their old expiry; permanent ones
and ones over 30 days end 30 days after import, and past ones are skipped.
Rerunning adds nothing and issues no second link.

Imported watches start paused and report to the owner's private conversation.
Confirm each after checking its owner, channel and thread, which the import
prints.

Timers are held, not scheduled. `confirm-timers` schedules each one that is
still more than a minute away, once. Private-conversation reminders need that
person's private conversation here; others are skipped, as are any past the 25
pending a person may hold. It prints a count per outcome. A settled timer keeps
no text.
