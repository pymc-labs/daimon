# Teams adapter

The Teams adapter answers in 1:1 chats and in channel threads where it is
@mentioned. Registration steps live in
[teams-app-manifest.yaml](teams-app-manifest.yaml).

### Ingress is HTTP, not a dial-out

Discord (gateway) and Slack (Socket Mode) dial out; Teams does not. The
adapter runs a FastAPI listener on `DAIMON_TEAMS__PORT` (default `3978`). The
Microsoft SDK owns `POST /api/messages` and validates the Bot Framework JWT
before daimon code runs. The same listener serves `/healthz` and `/readyz`.
`DAIMON_TEAMS__ENABLED=false` makes `/api/messages` answer 503, and bodies over
64 KiB are refused before parsing. The `teams` compose service (opt-in `teams`
profile) publishes the port; the messaging endpoint must reach it.

### Identity: one Entra organisation

A deployment serves one Entra organisation, `DAIMON_TEAMS__TENANT_ID`. At boot
the adapter provisions that tenant and reconciles its defaults; turns are
denied until the reconcile succeeds. The adapter fails closed unless the
conversation tenant and the channel-data tenant both equal the configured one
and the sender has a well-formed `aad_object_id`. Users need no setup: their
principal is created on first contact.

### Where it answers

- **1:1 chat.** Every message is a turn. Send `new` to start a fresh
  conversation. DMs go through the platform-neutral DM routing in
  `daimon.core.dm_routing`; with one organisation per deployment there is
  always exactly one tenant, so no picker is shown.
- **Channels.** Only messages that @mention the bot. Each root post is its own
  thread and session; replies in that thread continue it.
- **Group chats** get a short refusal.

A turn shows one status card, edited in place, with a Cancel button only the
author can use. The answer replaces the card, split across messages when long,
with Teams' thumbs up/down feedback on the last one. Messages sent while a turn
runs are queued and run as one follow-up per author.

### Capacity

Each tenant runs three turns at once by default
(`DAIMON_TEAMS__MAX_CONCURRENT_TURNS_PER_TENANT`). A new thread over the limit
gets a retry-later reply without starting a turn.

### Restart behaviour

On shutdown the adapter stops admitting, gives running turns 15 seconds, then
cancels them. The next boot edits every card a restart cut off to an
interrupted notice before admitting new turns. Teams cannot list a
conversation's messages, so a card whose send never returned an id cannot be
found and is left as is.

### Not supported yet

Reactions, conversation history replay, file delivery, continuations,
routines and settings panels.
