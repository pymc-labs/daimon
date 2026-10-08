# Agent identity on every message

Status: delivery in progress. The per-agent name and avatar paths, setup panel
controls, and deployment switch are implemented. The face generator is being
validated on staging. Owner: the agent-identity effort.

## Switch

`DAIMON_AGENT_IDENTITY__ENABLED` defaults to `false`. With it off, agents
post as the platform bot: Slack keeps the agent name in the footer, Discord
uses ordinary bot posts and requires a mention to start a turn, Teams adds no
name prefix, and setup panels hide avatar controls. No default avatar row is
created. Set it to `true` for the per-agent behavior described below, after
configuring the platform permissions. The setting applies to one deployment,
not to individual tenants; restart its services after changing it.

## Problem

Every agent in an install speaks as the one Daimon bot. The only cue to
who is talking is a name in a Slack footer (`blockkit.py`, `agent · 12s · …`)
or somewhere in a Discord thread, and when someone replies, the reply goes to
whichever agent the channel cascade picks, not to the agent they answered.

Goal: each message shows its agent's name and avatar in the platform's own
message header, on every chunk of a multi-message answer, without a header or
embed in the body; and a reply to an agent's message reaches that agent where
the platform tells us which message was answered.

## What each platform allows

| Platform | Mechanism | Granularity | Limits |
| --- | --- | --- | --- |
| Slack | `chat.postMessage` with `username` and `icon_url` (scope `chat:write.customize`) | per message | `chat.update` has no identity fields; we expect the posted identity to survive an edit and verify that on staging; `files_upload_v2` posts as the bot; replies carry only the thread root, not the answered message |
| Discord | application-owned channel webhook, `username` + `avatar_url` per message, `thread` for threads | per message | needs Manage Webhooks; no webhooks in DMs; a reply's automatic author ping targets the webhook, not the bot; a recreated webhook cannot edit the old one's messages; rate limits are per webhook and dynamic |
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
- Without the scope Slack rejects the call with `missing_scope`. When the
  error's `needed` names `chat:write.customize`, the adapter retries once
  without the two fields and remembers that for the installation's token, so
  an install that has not re-consented degrades to today's look instead of
  failing. A reinstall (new token) clears it.
- Turn posts are recorded in `agent_posted_messages` (today Discord only),
  with `channel_id` the Slack channel and `thread_ts` the thread, so the
  message → agent map covers both platforms.
- The tidy tools' delete and edit of a customized message are checked on
  staging; Slack documents limits on deleting impersonated messages.
- Slack starts turns only on a mention or a DM; an unmentioned thread reply is
  dropped (`app.py:1300`). That stays: answering unmentioned replies is a
  participation change, not identity. Routing on Slack therefore uses the
  thread root and the thread's binding, never "the chunk you answered".

### Discord

- One webhook per text or forum channel, created by the bot with
  `channel.create_webhook(name="Daimon agents")`, so it is application-owned:
  it can carry buttons and select menus, and their interactions come to the
  bot as today. Messages in threads use `thread=`.
- Webhooks are found by listing the channel's webhooks and keeping the one
  whose `application_id` is ours and whose channel matches; creation is
  serialized per channel. The token stays in process memory, never in the
  database or logs. Every process that edits posts (bot, MCP tidy tools,
  restart recovery) resolves the webhook the same way.
- One transport, `DiscordPostTransport`, owns send, edit and delete for agent
  posts: webhook (`wait=True`, so the sent message is returned and recorded)
  when available, else `thread.send` with `**Agent name**` prefixed to the
  first chunk of each answer only. Fallback cases: DMs, missing Manage
  Webhooks, the webhook limit, voice and stage text chats, locked threads.
  Edits and deletes go through the webhook when the message's `webhook_id` is
  ours (with the thread), through the bot otherwise. If our webhook was
  deleted, its old messages can no longer be edited: an edit that fails that
  way posts the update as a new message.
- Callers that assume the bot authored the message move onto the transport
  or accept our webhook as an author: status-card edits, turn-card recovery
  (`turn_card_recovery.py:379`), posted controls (`posted_controls/edit.py:75`),
  the tidy tools (`mcp/tools/discord/_tidy.py:129`, which today rejects webhook
  messages), and feedback and support reactions
  (`feedback_reactions.py:139`). Per-agent ownership checks stay as they are.
- Views: application-owned webhooks can carry interactive components. Views
  are sent with the client's state so they dispatch, and persistent handlers
  are re-registered after a restart as today.
- Gating: the gate's bot and guild checks stay as they are; only "addressed"
  widens. A message is addressed if it mentions the bot (as today) or is a
  genuine reply (`message.reference`) to a post recorded for this tenant and
  channel. Our own webhook messages are rejected explicitly. Admission,
  budget, concurrency and the participation batch run unchanged.
- Rate limits: discord.py's per-webhook buckets and 429 delays are honoured;
  one channel webhook carries concurrent turns and MCP posts, so staging
  checks two concurrent turns in one channel.

### Teams

Bold name prefix on the first chunk when the agent is not the built-in one.
Nothing else.

### Multi-message answers

Slack and Discord carry identity in the header of every chunk. Both clients
currently group consecutive messages with the same name and avatar under one
header; that is client rendering, not an API guarantee, and is checked on
staging. The fallback prefix appears only on the first chunk.

## Reply routing

The map is `agent_posted_messages` (migrations 0041 and 0050): turn posts,
tool posts and turn-opened threads, each with its agent. It is incomplete
(error notices, setup turns and files are not recorded) and not permanent
(`daimon audit prune` removes old rows); a reply to an unrecorded or pruned
message routes as today.

The authored candidate, most specific first:

1. Discord: the message `message.reference` points at, if recorded.
2. Either platform: the thread's root post, if recorded (a Discord thread is
   recorded as `auto_thread` when a turn opens it; a Slack thread's root is
   `thread_ts`).

The candidate goes into #409's shared admission as a requested agent with
selection source `authored`, so authorization, homes, `readers: own` and the
binding and live-session conflict rules are #409's, not a copy. The one
difference to agree with the operator-model effort: an authored candidate is
implicit, so where #409 would refuse an explicit name (a bound thread, another
agent's live session, an agent unavailable here), the authored candidate is
dropped and the turn routes as it does today, with no notice. An explicit name
still beats it.

Agreed with the operator-model effort (2026-10-07):

- #409's visibility filter runs first (the roster holds agents with no home or
  whose home is this place's home), so a dropped authored candidate behaves
  exactly like an unknown name and reveals nothing.
- A drop is silent to the person and logged with its reason: `bound_thread`,
  `other_session`, `unavailable` or `hidden`.
- An admitted authored candidate that opens a new thread writes the same
  named-thread handoff binding #409 writes (`create_named_binding_if_absent`,
  on conflict do nothing), so unprompted follow-ups stay with that agent and
  do not raise #388's Hand over card.
- Only `agent_posted_messages` rows from this deployment with a resolved agent
  id are authored: bot notices, other bots and rows whose agent was archived
  give no candidate.
- Everything still passes the same admission as the cascade: turn start,
  writers, `runs_in`, home and budgets.

Where nothing is derivable (a bare channel message), the cascade answers as
today, and the answer now shows who answered.

Named-agent routing is tracked separately from this identity work.

## Avatars

- Table `agent_avatars (tenant_id, agent_name, token, sha256, png, source,
  face_combo, updated_by_account_id, updated_at)`, keyed by tenant and the agent's daimon
  name normalized as #409 normalizes names (NFKC, casefolded), so an agent the
  resolver recreates keeps its avatar. A rename moves the row; archiving or
  deleting the agent, and tenant purge, delete it, so a later agent reusing
  the name starts from a fresh default. Uploads remain 256×256 PNG, at most 256 KB.
- With the identity switch on, the default is a 512×512 mascot face generated
  once at first use. The production mascot supplies the base face and the
  canonical expression sprites supply traced eyes and mouths. Five brow shapes,
  a background palette, headwear and shades provide further variation. Weighted
  candidates are compared as 20 px circular thumbnails against existing faces
  in the tenant; a plain face is preferred when it is sufficiently distinct.
  The selected combination and PNG are stored together, so later agents do not
  change earlier assignments. Reset renders the same combination with a new
  token. The built-in agent keeps its fixed classic platform avatar.
- With the switch off, no default row is created by message posting. The legacy
  initials generator remains available to callers that explicitly request it.
- Served publicly by the MCP service at `/avatars/{token}/{sha256[:12]}.png`
  (`token` random and replaced on every change, so the URL names no tenant or
  agent; the hash busts Slack's and Discord's caches; `Cache-Control:
  immutable`). Avatars are public by nature: anyone who sees a message can
  open its image, and platform and browser caches keep it after we delete it.
  The panel says so. The same route accepts `?size=128` or `?size=512` for a
  resized PNG; a size without a query returns the stored bytes.
- Setup panel: an Avatar row on the agent's detail screen with **Change** and
  **Reset** (back to the generated one). Change takes an uploaded image, never
  a URL we fetch: a Slack modal `file_input` (downloaded from Slack's file API
  with the bot token) and on Discord an attachment option on the agent setup
  command (downloaded from Discord's CDN). The download is bounded in time and
  bytes, decoded with a pixel cap, center-cropped, resized and re-encoded as
  PNG without metadata. Admin only, recorded in the panel audit.

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
human click-through, since no bot can see the rendering. Staging cases beyond
the screenshots: Slack post then edit keeps the identity; tidy delete of a
customized Slack message; two concurrent turns in one Discord channel; a forum
post, a locked thread and a voice-channel text chat; restart recovery of a
webhook card; feedback reactions on a webhook answer.

## Not doing

- Embeds or a name line in the body on Slack or Discord.
- Separate Slack apps or Discord bots per agent.
- Teams Adaptive Card headers.
- Answering unmentioned Slack thread replies.
- Fetching avatars from arbitrary URLs.
