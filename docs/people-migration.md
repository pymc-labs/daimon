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
