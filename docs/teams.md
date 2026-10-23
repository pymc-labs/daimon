# Teams adapter

Daimon on Microsoft Teams answers in 1:1 chats and in channel threads where
it is @mentioned. It reads the thread it is asked about, works with files,
runs routines, and has the same agents, memory and billing as on Discord and
Slack. This page covers how it behaves and where it differs. To set it up
(Entra, Azure Bot, the app package and the Teams admin center), follow
[Microsoft Teams in the self-hosting guide](self-hosting.md#microsoft-teams-optional).

Teams uses one bot identity for every agent. Answers from an agent other than
the built-in one start with its bold name on the first message chunk.

### Ingress is HTTP, not a dial-out

Discord (gateway) and Slack (Socket Mode) dial out; Teams does not. The
adapter runs a FastAPI listener on `DAIMON_TEAMS__PORT` (default `3978`). The
Microsoft SDK owns `POST /api/messages` and validates the Bot Framework JWT
before Daimon code runs; tokens from any other issuer (such as Entra ID) are
refused with 401 first. The same listener serves `/healthz` and `/readyz`.
With `DAIMON_PRIVATE_CHANNEL_MESSAGES__ENABLED=true` it also serves
`POST /api/graph/notifications` and `/api/graph/lifecycle` for Microsoft Graph
(see [Private and shared channels](#private-and-shared-channels-optional)).
`DAIMON_TEAMS__ENABLED=false` makes `/api/messages` answer 503, and bodies over
64 KiB are refused before parsing. The `teams` compose service (opt-in `teams`
profile) publishes the port; the messaging endpoint must reach it.

### Identity: one Entra organisation

A deployment serves one Entra organisation, `DAIMON_TEAMS__TENANT_ID`. At boot
the adapter provisions that tenant and reconciles its defaults; turns are
denied until the reconcile succeeds. The adapter fails closed unless the
conversation tenant equals the configured one and the sender has a well-formed
`aad_object_id`; in a 1:1 chat the channel-data tenant must match too. A
channel or group chat sender from another tenant is a member (below). Users need no setup: their
principal is created on first contact. Only the commercial Microsoft 365 cloud
is supported; government and China clouds use other Bot Framework hosts.

### Where it answers

- **1:1 chat.** Every message is a turn, in the organisation's tenant. It
  counts as a DM for the access policy, so `dm_memory_read_only` applies.
- **Channels.** Messages that @mention the bot or quote one of its messages
  (Reply on a bot message); the quote reaches the agent in place. Each root
  post is its own thread and session; replies that address it continue that
  thread. A bare @mention asks about the thread; if the thread cannot be
  read, the bot says so instead of starting a turn. A followed thread also
  gets unprompted replies (below). When an admin turned on plain-text
  mentions (`set_plain_text_mentions`), a person's post or reply that types
  `@` and the bot's name without picking it from the list counts as a
  mention too, the name dropped from the text as an @mention is. A name
  followed by "(" or by a dot and a letter is ignored ("@Daimon (urgent)",
  "@Daimon.Thanks"), as are names inside a reply quote.
- **Private and shared channels** work like standard ones once the app is in
  them, except that Teams sends the bot only the posts that @mention it. Out
  of the box a Reply on a bot message needs a mention too, a typed name is
  not seen, and a followed thread gets no unprompted replies; an operator can
  turn all three on (see
  [Private and shared channels](#private-and-shared-channels-optional)). The
  package declares `supportsChannelFeatures: tier1` (manifest 1.30), but
  adding the app to a team does not add it to these channels: each one's
  owner adds it from the channel.
- **Group chats** answer an @mention, a reply or a command, only while
  everyone in the chat is from the organisation (see [Group chats](#group-chats)).
- **Channels with `writers: none`** (ids look like `19:…@thread.tacv2`) and
  their threads get no reply, notice or tool post. One thread is named by
  adding `;messageid=<root post id>`. Where readers are limited, the agent's
  memory is read-only. See [permissions.md](permissions.md).

A turn shows one status card, edited in place, with a Stop button the
author, or a Daimon admin (an owner of the admin team), can use. Stop turns the card into "Stopping…", without the button, until
the turn has stopped and the card says so. The answer replaces the card, split across messages when long,
then a card follows with the summary line (agent, time, cost, money left; see
[billing.md](billing.md)) and the controls below. When a tool call named the
outputs folder, that card waits for file delivery (30 seconds at most, or
until the next turn in the chat starts), so it stays below the files, links
and previews. Delivery reads the file listing
from when the turn's last tool call ended, so it rarely adds more than two
seconds, and Graph thumbnails are requested as each file uploads. Messages sent while a turn
runs are queued, each marked ⌛ until its turn starts (only where the sender
may start a turn: not in a protected place, not for someone off the invoker
allowlist), and run
as one follow-up per author. Work an agent hands off
(`hand_off_task`) runs right after the turn that queued it; if admission
refuses that work, it is dropped without a notice, as on Slack. A retried
delivery runs once, across restarts too (see Restart behaviour).

Answers and `send_message` posts go out as Teams Extended Markdown, so `$…$`
and `$$…$$` render as math. A `$` that opens or closes no math span by
pandoc's dollar rules, such as a price, is escaped to stay literal; code is
left as is. A message that @mentions someone (a completion ping) stays plain
markdown, its math shown as typed.

### Feedback and Ask a person

The card below an answer carries 👍 and 👎 buttons: Teams refuses an edit
with both text and a card, so they cannot sit on the answer itself. 👍 records
the vote; 👎 opens a "What went wrong?" form with optional reasons and text, at
least one required. When support is set up the card also has an Ask a person
button, which opens the `support` form for that answer, spending a credit; the
post links to the card, just below the answer, and asking again about the same
answer spends nothing. A turn that only ran tools gets both on its finished card; a
cancelled turn gets neither. Only people who could start a turn at that
answer can vote or ask. With `DAIMON_SUPPORT__FEEDBACK_TO_SUPPORT` on, each
submitted 👎 form is posted once to the support channel without spending a
credit, and the form says it is shared. Feedback text is never logged.

Support requests route to the configured escalation channel. A request asked from a channel is open to those who could start a
turn there. When the channel is read only from inside (`readers: inside`),
the form warns that the note leaves it, and the request and any routed 👎
form say so, so whoever picks it up answers in the channel.

### People from another organisation

A shared channel's external participants (B2B direct connect) and guests
(Entra B2B guest accounts in ours) are treated as members, as everyone in a
Discord server is. Teams opens no 1:1 chat across organisations, so a 1:1
chat naming another tenant is refused. A shared channel another
organisation hosts keeps its files on that organisation's site.

### Commands and admins

In a 1:1 chat a command answers there. In a channel or group chat, `help`, `here`,
`memory`, `routines`, `billing` and `privacy` answer in the thread as a
private message only the sender sees, and their panels stay private when
clicked. Teams expires a private message after 24 hours, which closes its
panel. `setup` and `support` keep their flows in the sender's 1:1 chat,
opened if needed, with a private pointer in the thread; so does any command
Teams will not deliver privately, and any command in a channel read through
Graph. `new` there says each post is its own conversation. Typing `/` in a
channel lists the in-thread commands. A message is a command only when it is
the bare word (or `memory /<path>`), with or without a leading `/`; anything
longer goes to the agent. A message that is exactly a command word runs the
command, so a reply of just `here` shows the status card rather than reaching
the agent. None of them runs an agent turn.

A private message sent to Daimon in a thread runs no turn: a command answers
privately, and anything else gets one private line saying private questions
are not supported yet. The prompt starters in the compose box (personal
chat) and after an @mention (channels) fill in a ready-made request to send.

| Command | Does |
| --- | --- |
| `new` | Start a fresh conversation, or end a setup conversation. Teams only; Slack and Discord ask the agent. |
| `help` | The commands that work where it was sent. In a 1:1 chat it lists them all; in a channel it lists the ones answered there and names the 1:1 chat for `setup` (and `support`); in a group chat it also lists `new`. |
| `setup` | Agents, their details and who answers where; create an agent, add a skill to one, connect coding tools (admins), mint, list and revoke operator tokens (admins), or open a setup conversation. |
| `here` | Who answers here and what it can use: the status card for where it was typed (a channel post's thread, or the 1:1 chat): who answers, what it can read and whether publishing needs approval. |
| `routines` | List your routines (admins see all); create one for the agent this chat answers with (admins, for any agent); admins and creators pause, resume, read the last output or delete. |
| `memory` | Show what the agent answering where it was typed remembers; add a path to read one file. `memory mine` (and `memory mine /<path>`) shows your own personal memory with that agent, and `memory forget-mine` clears it after you confirm with the code it gives you. Answered in a 1:1 chat instead of a channel whose readers are limited, it says so, since the answer would leave the channel. In a channel shared with another organisation the agent uses that channel's own memory, which everyone there sees: `memory` shows it, and `memory mine` and `memory forget-mine` say personal memory is not used there. |
| `privacy` | See, export or delete what Daimon stores about you, or disconnect your Microsoft account. |
| `billing` | Your usage this month and the credit left, with when timed credit expires, and, typed in a channel with a budget, that budget; admins also see the month's spend, the top spenders and channel budgets, add credit, redeem promo codes and look up one person's spend with the people picker. |
| `support` | Ask a person: a form whose Send spends one of your support credits. Listed only when `DAIMON_SUPPORT__TEAMS_ESCALATION_CHANNEL_ID` names a Teams channel (`19:…`); Teams requests never go to Discord's or Slack's channel. The post links to where it was asked; requests go to the escalation channel. |

A 1:1 chat has no threads, so **Manage** in `setup` switches the chat into a
setup conversation for the chosen agent, with its own session. The chat's
usual session resumes when `new` or **End** closes it. Opening one runs no turn.

Daimon's admins are the owners of one team, and nobody else. The team is named
by its Entra group ID in `DAIMON_TEAMS__ADMIN_TEAM_ID` (the team's ⋯ → **Get link to team** → the
`groupId` parameter). The app must be installed in that team: its
`TeamMember.Read.Group` consent is what lets Daimon read the owners. Making
someone an owner makes them an admin, with no redeploy; the owner list is
cached for a minute, so a removed owner loses admin within about a minute,
everywhere: each owner read also takes the stored admin role, which routines,
tools and MCP clients act on, from anyone no longer an admin. A failed read
falls back to the last successful one for up to an hour, and demotes no one. If
the app is removed from the admin team, or no owner list has been read within
an hour, nobody is an admin in chats and cards until the next good read, while
stored admin roles stand until then; re-adding the app restores the owners.
When a Teams refusal sends someone to an admin, it adds "Admins are the owners
of the {team name} team.", using the name Daimon last recorded for the admin
team; until that team has had activity with the app in it, the line is left out.
Admins' turns run with the admin role, and the
panels unlock the admin actions for them. Every card click and dialog
re-checks the organisation, the clicker and their role. Boot does the same
demotion; support requests and budget notices for "the admins" reach the team's owners.
A refusal in a channel is a private
message, toast, dialog message or card edit only its sender or clicker sees.
Channel budgets (`set_channel_budget`) and channel environments
(`set_channel_environment`) work as on Discord and Slack. Who answers where
lists each channel's environment, and its **Channel settings** dialog changes
a selected channel's environment, permissions, and skills. Channel skills are
extra skills for whatever agent answers in that channel, added by name,
`agent/name` or skill id and removed by ticking them, as on Discord and Slack. Channel rules work as on Discord and Slack,
with `set_channel_rule` or `daimon channels rule set`. A thread (`;messageid=`)
counts as its channel, and a channel's own agents send nothing to 1:1 chats. The
CLI can't read channel names, so a copy it makes is named from the channel id.
Members see only the responders and agents explicitly allowed at the panel's
channel/thread. A panel opened in a 1:1 chat has no verified channel and lists
no agents for members; an admin retains the existing full panel roster.

### Group chats

A group chat is one session, keyed on the chat. The bot answers a message that
@mentions it, quotes one of its messages, or is a command; with plain-text
mentions on, a typed name counts too. It never joins in unprompted. A
targeted message to it runs a command privately, and anything else there
gets a one-line notice.

Everyone in a group chat sees each answer, whoever is in it. A group chat has no team, so its settings need a workspace admin.

A turn replays the chat like a channel thread (Channel history, below): the
first turn the newest 25 messages, a later one only newer messages, the
newest 4 images, under the resource-specific consent `ChatMessage.Read.Chat`
that a chat member grants when adding the app. Without it the agent gets the
text of the message alone. Those first 25 can predate the app's addition: the
bot does not record when it joined a chat, so it reads them all. Commands answer privately as in a channel; `new`
resets the sender's session there, and `setup` and `support` stay in the 1:1
chat. Files are not shared in group chats: every output, images included, is
skipped and deleted, and the agent is told so. Routines and
the agent's Teams tools do not reach group chats yet, and adding the app to
one posts no welcome.

### Channel history

Like Discord and Slack, a channel turn replays the conversation it sits in,
read through Microsoft Graph and marked untrusted for the agent. The first
turn in a thread gets the root post and its newest 50 replies, a later turn
only the replies newer than the last message it read, and a mention that starts a
thread the channel's 25 most recently active posts, each with its newest 10
replies. One page is read per turn, marked `truncated` when there is more.
Each message carries its sender and time, and the turn names its channel. System events, deleted
posts and the bot's own cards are left out; mentions read as `@name`. The
newest 4 images in the replay are passed to the agent, and up to 10 shared
files get a download link where channel files work (below). daimon's own
answers read back with their previews as images and the files they saved
as attachments with download links, so the agent can open what it delivered
earlier; the list of file links below an answer is left out of the
replayed text, so the agent does not copy it into its own reply. The MCP
Teams read tools resolve those saved-file links too. When Graph cannot
be read, the agent is told history is unavailable and why, rather than
guessing from nothing.

Graph access is the resource-specific consent `ChannelMessage.Read.Group` in
the manifest, plus `TeamMember.Read.Group` for the owner list the admin team reads (cached for a minute; without it the
team's owners are not admins). A team owner grants them when adding the app to a team, for that
team only; no tenant-wide permission or admin consent is needed. It also makes
Teams deliver every channel post to the bot, which ignores those without a
mention unless their thread is followed. An existing install needs the updated app package uploaded again
(bump `version`), then accepting the permission when the team updates the
app. Tenant admins can turn this consent off
(`Set-MgBetaTeamRscConfiguration -State DisabledForAllApps`); the bot then
answers without history. A refused, throttled or slow read (10 seconds) never
fails a turn: it runs without history and the adapter logs one warning with
the HTTP status and no content.

### Following threads

As on Discord, a thread can be followed: the bot reads replies nobody
addressed to it and joins in when a small classifier says it can help. Ask
the agent ("follow this thread", "stop following"); it calls
`set_thread_participation`, and `get_thread_participation` says what applies.
Anyone in a channel can follow its threads; a whole channel or the
organisation needs a listed admin. The deployment default is
`DAIMON_THREAD_PARTICIPATION__MODE`; `disabled` turns it off for Teams and
Discord alike.
In private and shared channels this needs the Graph notifications below.

A burst of replies is judged once, after the thread has been quiet for
`QUIET_SECONDS`, over the thread read through Graph, and capped per thread
per hour. The turn runs as the burst's newest author and passes the same
admission and billing gates as a mention, but posts only its answer: no
status card or Stop button, and every refusal, notice and error is only
logged. No turn posts a status message of its own under its answer, as on
Slack and Discord: what the person must hear rides on the answer, and only
cards (forms, file offers) are sent besides it. Root posts are never judged, the bot's own and other bots' messages
are ignored, and channels with `writers: none` are skipped. Without Graph history (the
consent above) a followed thread stays mention-only.

### Private and shared channels (optional)

Teams sends a bot only the private or shared channel posts that @mention it,
whatever the app's resource-specific consent says, and that consent cannot
subscribe to those channels' messages. With
`DAIMON_PRIVATE_CHANNEL_MESSAGES__ENABLED=true` Daimon subscribes to Microsoft
Graph change notifications for each private or shared channel it is in, so
replies in followed threads, Replies quoting the bot and typed `@<bot name>`
mentions (when plain-text mentions are on) work there as in a standard
channel. Graph's notification carries no content: Daimon reads the new
message once and routes it exactly as a Bot Framework message. @mentions still
arrive through Bot Framework and are ignored here. Expect a few seconds
of extra latency.

Setup:

1. Entra → App registrations → the bot's app → API permissions → Add a
   permission → Microsoft Graph → Application permissions →
   `ChannelMessage.Read.All` → Grant admin consent. The permission is
   tenant-wide, so Graph would serve any channel; Daimon subscribes only to
   channels it is added to or sees a message in.
2. Set `DAIMON_TEAMS__PUBLIC_URL` and
   `DAIMON_PRIVATE_CHANNEL_MESSAGES__ENABLED=true`, and let the reverse proxy
   pass `/api/graph/notifications` and `/api/graph/lifecycle`.
3. Restart the service. A channel is subscribed when the bot is added to it
   or first sees a message there; in a channel that already had the bot, one
   @mention is enough.

Subscriptions live about three days and are renewed half a day before they
expire; one Graph removed and could not recreate is retried after ten
minutes. Removing the bot or the app, or deleting the channel, deletes its
subscription (an app removal, all of the team's). No read tells whether the
bot is still in a channel, so if that removal event is lost the subscription
lives on until a post there is refused with 403
`BotNotInConversationRoster`; another 403 or a 404 keeps it. A typed
`@<bot name>` posts its card before any model call, so it stops there. An
unprompted reply posts nothing before its answer: until one answer is
refused, replies in followed threads still reach the metered classifier,
and that one turn is spent. Each
notification must carry the channel's secret, compared in constant time; one
that does not is ignored, and a request with no valid entry and a wrong
secret gets 401, and an entry whose subscription id is not a GUID is
ignored. A batch naming more than 100 new messages reads each channel's
newest page once instead of each message; older messages beyond that page
are dropped with a `catch_up_truncated` warning. Graph does not resend a batch it
saw accepted, so a Reply quoting the bot or a typed name read while the
service shuts down is held like a Bot Framework message and runs on the next
instance (see Restart behaviour); a followed-thread reply or a refusal read
then is dropped, as on Bot Framework. Each boot also reads every subscribed
channel once for messages after the last one handled, at most 10 minutes
back, and so does a notice that Graph missed notifications; a notified
message older than that is skipped too. Only the boot's read runs an @mention
of the bot: Bot Framework delivers those, except while the adapter was
restarting. Edits never start
a turn, and a message seen through both Bot Framework and Graph, notified
twice or read again after a restart runs once: both routes write the same
database receipt (see Restart behaviour). A shared
channel hosted by another organisation cannot be subscribed. With the
setting off the routes answer 404 and nothing is subscribed.

### Files

- **In.** Images pasted into a message are fetched with the bot token and
  passed to the agent. Files shared in a 1:1 chat reach the agent as short-lived
  download links. The manifest must set `supportsFiles: true`.
- **Out.** Files the agent writes to its outputs are offered in the 1:1 chat
  with Teams' file consent card, "Save <name> to your OneDrive?" (the agent
  guidance describes this path); accepting uploads the file to the user's
  OneDrive. Offers live in memory, so a restart drops them and the next turn
  that uses a tool offers the file again.
- **Channels.** Teams sends the bot only a channel message's text, so the bot
  reads each message it answers from Graph and passes its images to the agent.
  Files live in SharePoint: a standard channel's on the team's site, a private
  or shared channel's on a site of its own. No team-scoped permission reaches
  either, so files work once an admin grants the app `Files.ReadWrite.All`
  (below). Then a shared file inside the channel's own Files folder (never one
  from another site, library or folder) reaches the agent as a short-lived
  download link, and each file the agent writes is uploaded to the channel's
  Files tab (never overwriting) and linked
  below its answer, or in one message when the answer has no room (never
  after an unprompted answer); a failed upload is only logged.
  Elsewhere, or when Graph refuses, the agent is told the shared file's name
  and why it could not be opened, for its answer to explain, and an output is logged
  (`teams.channel_output.skipped` or `.upload_failed`, no name or content) and
  dropped from the delivery listing; the agent's own copy stays in its
  workspace. The turn context tells the agent which case applies
  (`files="available"` or `"unavailable"`), learned per channel from the last
  folder lookup or upload and rechecked every 10 minutes while unavailable.
  Where they are unavailable, `files_hint` tells the agent that an admin must
  grant `Files.ReadWrite.All`, rather than offer an artifact, report or
  notebook.
- **Office skills.** A Teams turn of the default agent adds Anthropic's
  built-in `docx`, `pptx` and `xlsx` skills to its session (Anthropic backend
  only; see `platforms` in [Defaults](defaults.md)). They count toward the
  20-skill session limit, so such a channel adds at most 6 skills of its
  own, or 5 where Microsoft accounts add the `microsoft-365` skill.
- **Updating a channel file.** A file in the channel's Files folder reaches
  the agent with its SharePoint link and version (its eTag) beside the
  download link. `replace_channel_file` saves an edited file over it as a new
  version instead of a renamed copy; SharePoint's version history keeps the
  earlier ones. It is refused for a folder, a file outside the folder of the
  channel the turn runs in, one of another type, or one changed since the
  agent read it (the version is compared without the eTag's quotes), and
  SharePoint refuses a file that is locked (open in a desktop app) or whose
  permissions, retention policy or sensitivity label block app writes; the
  agent then posts its version as a new file. A write is never retried: when
  its answer is lost or SharePoint fails with a 5xx, the agent is told the file
  may or may not have been saved. Slack and Discord have no equivalent.
- **Inline images.** A PNG, JPEG, GIF or WebP output also shows inline, as a
  base64 data URI downscaled to fit 1024x1024 and re-encoded under 512 KiB
  (PNG, else JPEG). Up to four, in delivery order, are added to the answer's
  last message as markdown images after its text, `![name](data:…)`: in a
  channel in the same edit as the file links (not after an unprompted
  answer), in a 1:1 chat above the consent cards. Teams refuses an edit that
  carries text and an image attachment (400 `BadSyntax`, "Activity resulted
  into multiple skype activities") and splits such a new post into two
  messages, so no activity carries both. The message is then sent as plain
  markdown, which Teams stores with each image as hosted content that Graph
  reads back; an Extended Markdown message keeps a link no app token can
  fetch. Plain markdown shows no math, so an answer whose last message has
  math, a closing notice card, or an edit Teams refuses (as it may when the
  message gets too large) gets them in an image-only message of its own after
  it; in a 1:1 chat, one message each below the consent cards, as do any
  beyond four. Every later edit of the answer, such as a notice above it,
  sends its images again; the 4,000-character limit counts its text alone. A channel without file
  access shows no previews. Animated GIFs get no preview, and a preview Teams
  refuses is only logged (`teams.output_preview.send_failed`).
- **Document previews.** Up to four Word, PowerPoint, Excel or PDF outputs
  saved to a channel's files show their first page, in delivery order, added
  to the answer in a second edit after the image previews, which never wait
  for them or give up a place to them. When the answer would then hold more
  than four previews, or the edit is refused, they come in a message of their
  own. Each is Graph's thumbnail of the uploaded file, asked for up to
  three times while Graph renders it (ten seconds at most for them all),
  fetched without a token and re-encoded like an image. One that does not come
  is logged (`teams.output_thumbnail.skipped`, no name) and left out.
- **Preview outputs.** An output named `<file>.preview.png`, such as
  `report.pdf.preview.png`, is the agent's own picture of that file. It shows
  inline like an image, under the file's name, and is never offered or
  uploaded; delivery deletes it. In a channel it stands in for Graph's
  thumbnail of that file. In a 1:1 chat it shows with the other previews,
  and `files_hint` tells the agent it may make one when it can render the
  first page cheaply (PyMuPDF from a PDF); no office renderer is installed by
  default.

The bot token is only sent to Bot Framework hosts, the Graph token only to
`graph.microsoft.com`, downloads and uploads only go to SharePoint hosts (a
thumbnail also to Microsoft's media service on `*.svc.ms`, where Graph serves
SharePoint thumbnails from), and every redirect hop is re-checked (Graph
reads follow none).

A shared `.md` or `.zip` can become a skill: `add_skill(attachment_url=…)`
takes its download link only over https from a SharePoint, OneDrive or Graph
host, sends no token, refuses a redirect off those hosts, and checks the
file's name before reading its capped body, with the usual preview and
confirmation card. **Add skill** on an agent's Details in `setup` takes a
pasted SKILL.md (up to 4,000 characters; a dialog has no file input), shows
what it holds, and adds it on a second Send as the agent's own skill, under
Discord's and Slack's rule: an admin for any agent, or anyone for an agent nobody else uses. The
panel is outside every channel, so a pinned agent needs an admin.

### Channel files (optional)

Grant the app the Microsoft Graph application permission
`Files.ReadWrite.All`. The manifest does not change and no restart is needed:
a channel's files turn on within 10 minutes of consent.

1. Entra portal → App registrations → the bot's app → API permissions → Add
   a permission → Microsoft Graph → Application permissions →
   `Files.ReadWrite.All` → Grant admin consent.
2. That covers every channel, standard, private and shared alike: Daimon
   finds each one's Files folder through Graph (`filesFolder`). A new
   channel's Files tab must be opened once first; until then the channel has
   no folder to find.

`Files.ReadWrite.All` reaches every file in the organisation, past
SharePoint's per-file permissions. Daimon therefore only reads a shared file
inside the channel's own Files folder, which the channel's members share, and
only writes to that folder; a link to anything else, or to a file more than
seven subfolders down, stays a name. Removing the
permission returns every channel to names only. Uploads go up in one request
(Graph takes up to 250 MB; Daimon's outputs are capped at 20 MiB), since
upload sessions need `Sites.ReadWrite.All`.

### Keys and sign-ins

When an agent asks for an API key, a `.env` file or an MCP token
(`request_agent_key`, `request_mcp_token`), it posts a card in the conversation. Only the
person who asked can open it; the secret goes into a Teams dialog and never
through the chat. Key values and `.env` contents take a multi-line field (paste
the file: a dialog has no file input), tokens a password field.
For GitHub repository access, the agent posts Connect GitHub.
`request_mcp_oauth` opens a private sign-in link the same way. Once the value
is saved, the card shows the outcome and the waiting work resumes. Key names,
replacing a key, whole-file imports, and binding a repo or importing skills
onto a shared agent follow Slack's rules. A GitHub token that cannot read the
repo is refused in the dialog, and the form stays open.

### Agent tools

MCP tools that need a chat platform work from Teams turns, posting through
the Bot Framework REST API and reading through Graph with the same app
registration:

- **Channel reads.** `list_channels`, `read_channel`, `read_thread`,
  `get_message`, `list_threads`, `parse_link` (Teams message links) and
  `search_messages`, over the consent above, so "summarise this channel"
  works. The caller must be on the channel's roster; a private or shared
  channel is read only from a turn inside it. Graph has no message search for
  an app, so `search_messages` scans recent posts, bounded, and says when it
  stopped short. A 1:1 chat cannot be read back. A message's files come with
  a download link, SharePoint link and version where `Files.ReadWrite.All` is
  granted and the file is in the channel's own Files folder, else by name: the permission ignores
  SharePoint's per-file permissions, so a file from another site, library or
  folder is never linked. The link lasts about an hour
  and anyone holding it can fetch the file, so only messages the read returns
  get one.
- **Posting.** `send_message` and `create_thread` (up to 6,000 characters,
  only into a conversation the requester belongs to). Files ride along: in a
  channel with `Files.ReadWrite.All` they are saved to its Files tab and linked; in
  a 1:1 chat they are offered with a file consent card. An agent can edit or
  delete what it posted (`edit_message`, `delete_message`); closing a thread
  (`delete_thread`, `archive_thread`) is not on Teams.
- **`replace_channel_file`** (Teams only) saves a staged upload over a file
  in the channel's Files folder as its new version, for a requester who may
  post there, from a turn in that channel (its `origin_context_id`), only
  while the file is at the version the agent read (`If-Match`). It returns a
  link line for the answer, and the link and new version for a further
  replace.
- **`send_direct_message`** opens a 1:1 chat with someone who shares a team
  with the caller and Daimon, named by Entra object ID.
- **GitHub setup.** `github_connect` sends the Connect GitHub card to the
  requester's 1:1 chat, never the channel, since its link is single-use and
  private; if that chat cannot be opened, the link is withdrawn and the agent
  says so. Work that waited on the repo resumes once it is connected.
  `set_working_repo` and `remove_repo` work as on Discord and Slack. Access
  requests to admins (`request_github_access`) are not on Teams yet.
- **`post_wizard`** posts its form as an Adaptive Card that only the asker can
  fill in (no step images); Submit starts their turn.
- Task handoff and fresh starts, timers (`create_timer`, `list_timers`,
  `cancel_timer`), routines that post to a channel or thread (below),
  `bind_public_repo` and the GitHub App install link. When a channel's agent
  changed under a conversation, its notice carries a Hand over button, as on
  Discord and Slack; a refused click is answered only to the clicker.

With tool safety on (`DAIMON_TOOL_SAFETY__ENABLED`), an attached tool's write
waits on an Approve/Deny card in the conversation that only the requester can
answer. It shows the action, its consequence and the requester's name, with a
**Details** expander for the inputs. Each call gets its own card; answered,
expired and stopped cards lose their buttons.

Nothing app-only lists the teams an app is in, so the adapter records each
team when the app is added or anyone writes there; an older install shows up
once someone writes in it. Adding the app posts a welcome: in a team's General
channel a card that says to @mention Daimon in a post, with an **Open chat**
button that opens the person's 1:1 chat with it; in the 1:1 chat a short note
that says to ask a question or send a data file. Removing the app from a team
forgets that team.

### Microsoft account (optional)

Each person can connect their own Microsoft 365, so the agent reads and
changes their files, mail, calendar and notes as them, with exactly their
permissions. Set `DAIMON_MICROSOFT_ACCOUNTS__CLIENT_ID`, `__CLIENT_SECRET` and
`__TENANT_ID` (and `__AUTHORITY` for a national cloud), plus
`DAIMON_CRYPTO__KEYS` and `DAIMON_MCP__PUBLIC_URL`. Set the
`DAIMON_MICROSOFT_ACCOUNTS__*` values identically on every service that runs
turns and on the MCP server:
turn services decide from them which tools ask and whether the skill is
added, and the MCP server checks the session against the same decision.
With none of them set the tools and the skill are absent and sessions are
unchanged; the privacy card still offers **Disconnect Microsoft** to anyone
whose account was saved earlier. With only some set the tools stay absent
and the MCP server logs one `microsoft_accounts.disabled` warning naming
what is missing; a tenant ID that is not a UUID stops startup with an error
naming the variable, and a blank `__AUTHORITY` means the default.

Register a single-tenant app in the same Entra tenant.

1. Add a **Web** redirect URI `<root>/oauth/microsoft/callback`, where `<root>`
   is `DAIMON_MCP__PUBLIC_URL` without the trailing `/mcp`, and create a
   client secret.
2. Under **API permissions**, add the Microsoft Graph **delegated**
   permissions `openid`, `profile`, `User.Read`, `offline_access`,
   `Files.ReadWrite.All`, `Sites.Read.All`, `Mail.Read`, `Mail.Send`,
   `Calendars.ReadWrite` and `Notes.ReadWrite.All`. Grant admin consent, or let each person consent
   when they sign in if the tenant allows it.

When a tool needs an account the person has not connected, the agent asks
whether they want to connect; only if they agree does it call
`connect_microsoft`, which sends a private card to their own 1:1 chat. No
new card is sent while the last one is unexpired. Its link works once, for
ten minutes, and only for them: the account they sign in
with must be the same Entra user in the same tenant, or nothing is saved.
Conditional Access applies to that sign-in and to every token refresh; when
a policy later demands a new sign-in (for example MFA or a compliant
device), the saved account is dropped and the agent asks them to connect
again.

Microsoft outages and throttling never drop a saved account: the tool says
so, with the `Retry-After` wait when Graph sends one (sign-in outages give
none), and nothing retries on its own. A client
secret Entra refuses (`invalid_client`, for example once it expires) is
reported as a setup problem for an administrator and logged as
`microsoft_accounts.app_rejected`.

Where these settings are configured, a Teams turn of the default agent adds
the `microsoft-365` skill, which tells the agent when and how to use these
tools (Anthropic backend only, like
the Office skills); Slack and Discord sessions never get it.

The tools, all Teams-only and limited to an allowlist of Graph v1.0 paths:

- **`microsoft_search`** and **`microsoft_get`** find and read files,
  SharePoint lists and Excel workbooks. In the person's 1:1 chat, file
  content comes back as a short-lived download link for the sandbox; no
  pre-authenticated link is returned anywhere else, and sharing permissions
  and thumbnails are refused.
- **`microsoft_write`** uploads or replaces files, edits workbooks and
  creates or updates OneNote pages. It has no delete or move call, but
  replacing a file or a workbook call (clearing or deleting a range or sheet)
  can still destroy data. A path over 500 characters, or a body over 8,000
  characters or 14,000 bytes (emoji and most non-Latin text take 3 to 4
  bytes a character), is refused. With tool safety on, each call waits for the person's Approve
  on a card that names the change (a workbook, a file, a OneNote page) and
  shows the method, the path, and the whole body under **Details** (the file
  handle for an upload).
- **`microsoft_send`** sends an email or a calendar invite, each time only
  after the person presses Approve on a card showing every recipient, the
  subject, time and time zone, location and the start of the message, with
  the whole message under **Details**, whether or not tool safety is on. A
  message over 8,000 characters, an address with an invisible character, or
  addresses, subject, location and times that total over 3,000 bytes, is
  refused. If the card can't be shown, nothing is sent.

Mail, calendar and OneNote are reachable only from the person's 1:1 chat
with Daimon, never from a channel or group chat. The chat is the one recorded
for the turn origin the agent passes, which must be a live turn of that
person with that agent. Results are wrapped as
untrusted content.

Refresh tokens are stored Fernet-encrypted (`DAIMON_CRYPTO__KEYS`), one row
per person; access tokens stay in memory. `privacy` → **Disconnect
Microsoft** deletes the row, and deleting your data removes it too.
Disconnecting does not revoke Microsoft's own sign-in sessions; people can
do that from their Microsoft account page.

### Routines, pings and tables

A routine's destination can be a channel or a thread
(`<channel>;messageid=<root>`). Its creator must still be on the channel's
roster; when they are not, or the post fails, the result goes to their 1:1
chat if the direct-message policy allows. With completion pings on
(`DAIMON_COMPLETION_PINGS`), the answer is posted as a new message (in a
channel, @mentioning the asker) so Teams notifies, and the card then reads
"Done. The answer is below.". The asker's message also gets 👀 once the turn
is accepted and ✅ in its place once it is answered, as on Discord. Teams renders markdown tables
natively, so `DAIMON_TABLE_RENDERING` has nothing to do here.

A routine result posted in a channel also sends its creator an activity feed
notice that opens it. Each routine keeps one feed entry (its `chainId` comes
from the routine's id), so a new run replaces the last. A tool approval or
question card in a channel thread notifies the person who must answer it. A
1:1 chat notifies on its own and gets none. Notices go through Graph's
`sendActivityNotification` on the channel's team under the
`TeamsActivity.Send.Group` consent, in the background within five seconds;
one that fails is logged (`teams.activity_notice.failed`, status and reason
only) and dropped, never touching the post. People turn them off under the
app in their Teams notification settings.

### Capacity

Each tenant runs three turns at once by default
(`DAIMON_TEAMS__MAX_CONCURRENT_TURNS_PER_TENANT`). A new thread over the limit
gets a retry-later reply without starting a turn, and due timers and handoffs
from the wake poller wait until a turn finishes. A post, edit or reaction Teams
throttles (HTTP 429) is retried once after the wait Teams asks for, up to 10
seconds (five for a reaction, its whole call). Reactions (👀, ✅, ⌛) are spaced
to Bot Framework's
[limit](https://learn.microsoft.com/en-us/microsoftteams/platform/agents-in-teams/agent-reactions)
of two a second per bot and sent in the background, so they never hold up a turn. An add that
would wait over five seconds, or fails, is dropped; a removal always waits its
turn, so ⌛ and 👀 never stay behind, and is skipped only when its add never
landed.

### Restart behaviour

On shutdown the adapter stops admitting, gives running turns and output
sweeps 60 seconds, including a sweep that a turn ending meanwhile starts,
then cancels them; give the container a longer stop grace (90 seconds, see
the self-hosting guide). A cancelled sweep leaves its files listed, and the
session's next sweep delivers them. The next boot edits every card a restart cut off to an
interrupted notice before admitting new turns. A channel card whose send never
returned an id is found by reading its thread through Graph; one in a 1:1 chat,
which cannot be read back, is left as is.

Every incoming message is admitted once, by Teams activity id, in a database
receipt shared by every adapter process, so a redelivery, a second process or
a restart never runs it twice. A message whose turn had not started when the
restart began (queued behind a running turn, or arriving during the 60
seconds) is kept: its chat gets one "I'm restarting for an update. I'll pick
this up as soon as I'm back." note, and the next process runs it once, in
order, through the usual admission checks. One more than 10 minutes old by
then gets "I restarted for an update and couldn't get to this in time. Please
send it again." instead. Either way its ⌛ comes off once the next process
picks it up; shutdown never waits on a reaction. A turn that had started is
never rerun: its card is
marked interrupted as above. Nothing is said about a message until its receipt
is written: while the database is unreachable the chat hears nothing, and a
message whose start could not be recorded is tried again every 30 seconds. A message's
text is stored only until its turn starts or it is answered, and the
content-free receipt is deleted after 24 hours.

Teams does not resend a message posted in the seconds between the old process
stopping and the new one serving. So once the new one serves, it reads back,
through Graph, the standard channels (replies included) and group chats where
a message was admitted in the last 24 hours, newest first, at most 50 of them,
one page each. A message from a person that is under 10 minutes old and was
posted before the service began serving runs through the same admission as any
other, in order: an @mention, a reply in a followed thread, a group chat
mention, or a typed "@<bot name>" while that switch is on. Later ones are
Bot Framework's own. One already admitted, whether Bot Framework delivered it
or an earlier boot read it, is skipped, so nothing runs twice; a message
within a second or so of the start of serving can be missed. This needs the
same per-team permissions as channel history (`ChannelMessage.Read.Group`,
`ChatMessage.Read.Chat`), not the setting for private channels. A conversation
Graph refuses is logged (`teams.restart_catch_up.read_failed`) and skipped; it
never holds up boot. Private and shared channels are read through their
subscription (above), so they are caught up only with
`DAIMON_PRIVATE_CHANNEL_MESSAGES__ENABLED` on. A 1:1 chat cannot be read back
through Graph, so a message sent there in the gap is lost and must be sent again.
The bot's name for a typed "@<bot name>" comes from a private-channel
subscription or the bot's own earlier message in what was read; with neither,
only a real mention is read back. Deleting your data in `privacy` clears you
and your words from your receipts but keeps each receipt until the 24 hours are
up, so a message you deleted is not run again by a restart's read.

Handoffs, work waiting on a private input and timers are durable wake-queue
rows, and a wake poller opens every chat with due work. They survive a
restart, a timer fires at its time, and one whose turn had already started
when the process died is not run twice. A timer whose chat now answers as a
different agent posts a notice instead of running. A deployment with
`DAIMON_TEAMS__ENABLED=false` runs no timers.

### Not supported

`/dm-me` conversations, files, routines and agent tools in group chats, files in channels whose site is
not granted or that another organisation hosts. Removing the app does not
archive the organisation's tenant: a deployment serves one organisation.

Agent Details shows a read-only GitHub section with one repo per line, the
working repo (or “No working repo”), and a prompt to ask the agent in chat to
change repos; the Teams setup card has no Connect GitHub button. A setup
conversation in the 1:1 chat opens with “This chat is about…”.
