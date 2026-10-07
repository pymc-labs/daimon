# Agent identity on every message

Status: design, 2026-10-07. Owner: the agent-identity effort.

## Problem

Every agent in an install speaks as the one Daimon bot. The only cue to
who is talking is a name in a Slack footer (`blockkit.py`, `agent · 12s · …`)
or somewhere in a Discord thread, and when someone replies, the reply goes to
whichever agent the channel cascade picks, not to the agent they answered.

Goal: each message shows its agent's name and avatar in the platform's own
message header, on every chunk of a multi-message answer, without a header or
embed in the body; and a reply to an agent's message reaches that agent.

## What each platform allows

| Platform | Mechanism | Granularity | Limits |
| --- | --- | --- | --- |
| Slack | `chat.postMessage` with `username` and `icon_url` (scope `chat:write.customize`) | per message | `chat.update` keeps the identity the message was posted with; `files_upload_v2` posts as the bot; the `APP` tag stays |
| Discord | application-owned channel webhook, `username` + `avatar_url` per message, `thread` for threads | per message | needs Manage Webhooks; no webhooks in DMs; `APP` badge stays; replies to a webhook message do not mention the bot; 15 webhooks per channel |
| Teams | Bot Framework sends as the bot's registered identity; no per-message name or avatar | per bot registration | per-message identity needs an Adaptive Card header (an embed), so out |

## Mechanism

### Identity value

`AgentIdentity(name, avatar_url, is_builtin)` resolved once per turn from the
admitted agent and handed to the adapter's lifecycle with the rest of the
turn context. The built-in Daimon agent (setup conversations, the deployment
default) posts with no override, so it keeps the app's own name and icon.

### Slack

- Every post a turn makes on behalf of an agent passes `username=name`,
  `icon_url=avatar_url`: the status card, answer chunks, continuation and
  recovery posts, the wake poller's answer, tool confirmation cards, DM
  answers, and the MCP `send_message` tool on Slack. `chat.update` needs
  nothing: the card that becomes the answer keeps the identity it was posted
  with.
- The footer stops repeating the name (it becomes `12s · in / out · cost`).
- Files uploaded by a turn still show the bot. The answer chunk next to them
  carries the agent; we do not post an extra message per file.
- Ephemerals (`chat.postEphemeral` accepts the same fields) are panel and
  error replies from Daimon itself: unchanged.
- Without the scope Slack rejects the call with `missing_scope`. The adapter
  retries once without the two fields and marks the install as lacking the
  scope for an hour, so an install that has not re-consented degrades to
  today's look instead of failing.
- Turn posts are recorded in `agent_posted_messages` (today Discord only),
  with `channel_id` the Slack channel and `thread_ts` the thread, so the
  message → agent map covers both platforms.

### Discord

- One webhook per text or forum channel, created by the bot with
  `channel.create_webhook(name="Daimon agents")`, so it is application-owned:
  it can carry buttons and select menus, and their interactions come to the
  bot as today. Messages in threads use `thread=`.
- Webhooks are found by listing the channel's webhooks and keeping the one
  whose `user` is the bot; the token stays in process memory, never in the
  database or logs. A 404 on send drops the cache entry and retries once.
- A small `TurnSender` replaces `thread.send` / `message.edit` /
  `message.delete` for turn posts: webhook when available, else `thread.send`
  with `**Agent name**` prefixed to the first chunk of each answer only (DMs,
  missing Manage Webhooks, webhook limit reached). Reactions are added by the
  bot user, which works on webhook messages.
- Persistent views keep working: their `custom_id`s are unchanged and
  interactions on webhook messages from an application-owned webhook reach
  the application.
- Gating: a message that replies to (`message.reference`) a recorded agent
  post is treated as addressed to Daimon even without a mention, because a
  reply to a webhook message cannot ping the bot. Our own webhook messages
  are already dropped (`author.bot`).
- Rate limit: webhooks allow 5 requests per 2 s per webhook; the status card's
  edit debounce already sits under that.

### Teams

Bold name prefix on the first chunk when the agent is not the built-in one.
Nothing else.

### Multi-message answers

Slack and Discord carry identity in the header of every chunk, and both
platforms collapse consecutive messages from the same name and avatar into
one block, so a long answer reads as one message. The fallback prefix appears
only on the first chunk.

## Reply routing

The map is `agent_posted_messages` (exists, migration 0041/0050): every turn
post, tool post and turn-opened thread, with its agent.

Proposed selection order, to be agreed with the operator-model effort, whose
named-agent routing (#409) owns the rules:

1. An explicit name (`@Daimon name:` or a Discord agent role, #409).
2. A bound thread (setup or handoff binding).
3. **Authored:** the message replies to, or sits in a thread whose root is, a
   post recorded for agent A. A is the requested agent.
4. The channel cascade, as today.

Tier 3 feeds the same "requested agent" input that #409's name form feeds, so
the permission, home and `readers: own` checks are #409's, not a copy: an
agent that may not run here falls through to tier 4 with no notice. A Discord
thread opened by a turn is recorded as `auto_thread`, so its later mentions
stay with the agent that opened it.

Where nothing is derivable (a bare channel message), the cascade answers as
today, and the answer now shows who answered.

This lands after #409 merges (after Oct 11) and is built on its code.

## Avatars

- Table `agent_avatars (tenant_id, agent_name, token, sha256, png, source,
  updated_by_account_id, updated_at)`, keyed by tenant and agent name, so an
  agent the resolver recreates keeps its avatar. PNG, 256×256, at most 256 KB.
- Default: generated once on first use, the agent's initials on a colour
  picked from a fixed palette by a hash of the name (Pillow,
  `ImageFont.load_default(size=…)`, no font file shipped). `source='default'`.
- Served publicly by the MCP service at `/avatars/{token}/{sha256[:12]}.png`
  (`token` random, so the URL names no tenant or agent; the hash in the path
  busts Slack's and Discord's caches on change; `Cache-Control: immutable`).
  The avatar of a deleted agent stops resolving when its row is purged.
- Setup panel (Slack `agent_setup`, Discord `agent_setup`): an Avatar row on
  the agent's detail screen with **Change** (an https image URL, fetched once
  server-side with a size cap, decoded, center-cropped and resized) and
  **Reset** (back to the generated one). Admin only, recorded in the panel
  audit.

## Permissions and app changes

- Slack: add `chat:write.customize` to `docs/slack-app-manifest.yaml`; on the
  staging app then production, add the scope and reinstall the app to each
  workspace. Exact steps go in `docs/slack.md`.
- Discord: the bot needs **Manage Webhooks** in channels it answers. Add it to
  the invite permissions integer and grant it to the bot role on existing
  servers. Exact steps go in `docs/self-hosting.md`.

## Delivery

1. Core identity + avatars + Slack per-message identity + Slack turn-post
   recording + manifest.
2. Discord webhook sender, fallback, reply gating.
3. Panel avatar controls on Slack and Discord; Teams prefix.
4. Authored reply routing (after #409).

Each PR merges to main and is checked on staging; screenshots come from a
human click-through, since no bot can see the rendering.

## Not doing

- Embeds or a name line in the body on Slack or Discord.
- Separate Slack apps or Discord bots per agent.
- Teams Adaptive Card headers.
