# Architecture

Daimon is built on
[Anthropic Managed Agents](https://platform.claude.com/docs/en/managed-agents/quickstart).
Managed Agents (MA) owns the agent, its sandbox, its skills and the session
that runs a turn. Daimon owns everything around that: the chat surfaces, the
tenancy model, the config cascade, credentials, the credit ledger, and the
pipeline that turns a chat message into a session and streams the result back
into a thread.

This page is the map to read before the code. `CONTRIBUTING.md` has the dev
setup and the quality gates; [configuration.md](configuration.md) has every
setting; [self-hosting.md](self-hosting.md) has the deployment.

Discord, Slack, scheduler and MCP emit `runtime.health` every 30 seconds with
Anthropic response attempts, database pool use, event loop lag and active turns.
The Discord process also reports Discord 429 retries by route and longest retry wait.
When an opening Discord mention takes over three seconds to name or create its
thread, the bot replies in the parent channel with an opening notice, then edits
it with the thread link or retry guidance. The conversation stays in the thread.

## The shape

```mermaid
flowchart TB
    subgraph adapters["packages/adapters — one platform each"]
        direction LR
        Discord
        Slack
        Teams
        MCP
        Scheduler
        CLI
    end

    subgraph core["packages/core — daimon.core"]
        direction LR
        turn["turn/<br>admit · bind_session · run_prepared_turn · driver"]
        stores["stores/<br>Pydantic over the private ORM"]
        defaults["defaults/<br>seed + reconcile"]
    end

    adapters --> core
    core <--> ma["Anthropic Managed Agents<br>agents · environments · sessions · skills"]
    core --> pg[("Postgres<br>tenants · identity · thread↔session<br>config · credentials · ledger")]
    ma -. "tool calls back over HTTP" .-> MCP
```

The dotted edge is worth noticing early: the MCP adapter is both an inbound
adapter and the server the running agent calls its tools on. That is why a
turn started from Discord still reaches `packages/adapters/mcp/` — the
sandbox makes an authenticated HTTP call back to it mid-turn.

## Packages, and why the boundaries exist

| Package | Path | Owns |
| --- | --- | --- |
| `daimon.core` | `packages/core/daimon/core/` | Schema and migrations, stores, MA helpers, the turn pipeline. Imports no adapter. |
| `daimon.adapters.discord` | `packages/adapters/discord/` | Discord I/O, rendering, permissions, slash commands. |
| `daimon.adapters.slack` | `packages/adapters/slack/` | Slack I/O, Block Kit rendering, per-user OAuth. |
| `daimon.adapters.teams` | `packages/adapters/teams/` | Teams HTTP ingress, Adaptive Card rendering, text commands and panels, file consent delivery, channel files through SharePoint. |
| `daimon.adapters.mcp` | `packages/adapters/mcp/` | The MCP server the agent calls, plus the OAuth, webhook and hub HTTP routes. |
| `daimon.adapters.scheduler` | `packages/adapters/scheduler/` | The routine poll loop. |
| `daimon.adapters.cli` | `packages/adapters/cli/` | The `daimon` admin binary. |
| `daimon.testing` | `packages/testing/` | Shared fixtures. |
| `mux` | `packages/mux/` | A separate namespace, not part of `daimon`. See [its README](https://github.com/pymc-labs/daimon/blob/main/packages/mux/README.md). |
| `notebook_host`, `report_host` | `apps/*/src/` | Standalone services that talk to Daimon over HTTP only. |

None of that is convention. Eight `import-linter` contracts in the root
`pyproject.toml` are the authority, run as `uv run lint-imports` in pre-commit
and CI:

| Contract | Forbids |
| --- | --- |
| Core must not import adapters | `daimon.core` → `daimon.adapters` |
| Adapters must not import each other | any of cli, mcp, discord, scheduler, slack, teams → another |
| ORM module is private to stores and defaults | anything but `daimon.core.stores.**` / `daimon.core.defaults.**` → `daimon.core._models` |
| Core must not import testing | `daimon.core` → `daimon.testing` |
| CLI admin commands and run must not import each other | `daimon.adapters.cli.commands` ⟂ `daimon.adapters.cli.run` |
| Mux must not import daimon | `mux` → `daimon` |
| notebook-host must not import daimon | `notebook_host` → `daimon` |
| report-host must not import daimon | `report_host` → `daimon` |

The first two make the platform surfaces replaceable: a change to how Slack
renders a table cannot reach Discord, and core can be exercised without any
chat client. The ORM contract is the one contributors trip over most — the
schema lives in `packages/core/daimon/core/_models.py` behind a leading
underscore, and everything outside `daimon.core.stores` and
`daimon.core.defaults` sees Pydantic models from
`packages/core/daimon/core/stores/domain.py` instead of SQLAlchemy rows. `packages/core/tests/test_orm_import_contract.py` fails when the
contract's enumerated module list drifts from the directory, because
import-linter cannot express "every sibling except `_models`".

The last three are trust boundaries rather than tidiness: a service that
cannot import `daimon` cannot hold the Anthropic key or a database
credential, whatever a future contributor is tempted to do inside it.

## MCP browser pages

The MCP adapter renders its GitHub, Slack, personal link, MCP connection, and
billing pages through `web_shell.render_page`. The shell owns the Daimon mark,
local Inter fonts, semantic colours, responsive card, and cache and frame
headers. The pages are native HTML using shadcn New York styling recipes;
checkboxes, radios and forms remain native controls. Page modules escape any
dynamic content before passing their trusted markup to the shell. The
stylesheet and assets are served at `/web/` from the MCP package.
Meaningful page icons render as local inline Lucide SVGs. The GitHub mark is
from Feather, since Lucide excludes brand marks; both licences are bundled
beside the Inter font licence.

`scripts/generate_web_css.py` compiles `static/input.css` using the pinned
Tailwind v4 standalone Linux x64 CLI. Run the generator on Linux x64; it does
not ship macOS binaries. The script verifies the binary's SHA-256, scans only
the page modules listed in `input.css`, and writes the committed `static/web.css`.
Run `uv run python scripts/generate_web_css.py` after editing styles; CI runs
the same command with `--check`. The CLI is a build tool and is not part of the
runtime image. The notebook and report hosts have their own page shells.

## How a message becomes a turn

Discord, Slack and Teams run the same two-stage chokepoint in `daimon.core.turn`.
The staging is deliberate — neither stage returns a boolean, both raise typed
errors, so an adapter cannot forget a gate.

**Stage one, `admit()` — `packages/core/daimon/core/turn/admission.py`.** One
call does identity resolution, config resolution, and every pre-turn gate, and
returns a frozen `Admission` (account id, MA agent, MA environment, resolved
config). The order is load-bearing and documented as such in the module:

1. Resolve the platform user to an `accounts` row, via
   `get_or_create_platform_principal` in
   `packages/core/daimon/core/stores/identity.py`.
2. Channel writers — a turn whose reply would land in a channel with
   `writers: none`, a thread under one or (Discord) a channel in such a
   category raises `AdmissionDenied("writers_none")`, admins included. It runs
   before the invoker gate, whose refusal would otherwise be posted there. If a
   Discord thread's category can't be resolved while any category has a rule,
   the turn is refused.
3. Invoker policy — the tenant's access policy (below) may restrict who can
   start a turn. A refused user raises `AdmissionDenied("invoker_not_allowed")`
   before the cascade, so they learn nothing about the tenant's configuration
   and no MA call is made.
4. External participant — a caller from another organisation (a Teams shared
   channel's B2B direct connect participant or a guest,
   `admit(external=ExternalFinding(…))`) raises
   `AdmissionDenied("external_participant")` anywhere but a channel with
   `readers: own` or a thread in one, and in its setup thread too. Only a finding on
   positive evidence is stored on `accounts.is_external`; one without holds
   that turn alone (`turn_origins.is_external`, which the MCP verifier reads)
   and keeps the stored role. An external account's stored role is always
   `user`, and it administers no channel. `reauthorize` checks it again.
5. Resolve config through the cascade
   `thread → channel → tenant → deployment`, in
   `packages/core/daimon/core/stores/scoped_config_read.py`. The tiers are
   named by `ConfigTier` in `packages/core/daimon/core/scope.py`; the bottom
   one comes from `defaults/config.yaml`, see [defaults.md](defaults.md).
   A direct bot mention may choose a named agent with `<agent-name>:` as the
   first token after the mention (case-insensitive NFKC match). Discord also
   accepts a mention of a bot-managed agent role, even without a bot mention.
   An unknown name leaves ordinary routing in place without a notice. Agents
   with a home cannot be named outside it, even if the caller guesses the name.
   The named choice overrides the unbound default but still enters `admit()`
   through `authorize(RUN_AGENT)` for the agent's `runs_in` and home rules,
   plus the usual invoker and budget checks. A new named thread records a
   `handoff` binding so later replies keep its agent. A bound thread rejects a
   different named agent and offers the existing Hand over action on Discord
   and Slack. Teams shows the same notice without a button. Two visible agent
   choices get a request to use one name. A channel with `readers: own` accepts
   only its own agent and names that agent in the refusal. The five refusal
   cases share one copy source: Discord renders a notice card, Slack renders
   Block Kit and Teams renders an Adaptive Card. On processes running the wake
   poller, Discord reconciles
   roles after ready, every ten minutes, within a minute of a policy change,
   after its agent-create and channel-rule panel actions, and when a used role
   has an outdated name. It excludes
   agents with a home or a rule that runs nowhere. Missing Manage Roles logs
   once per guild while the text form remains available.
6. Raise `MissingTurnConfigError` if no agent or environment resolved — before
   any MA call, so a misconfigured tenant sees the config error rather than a
   billing one.
7. Resolve the agent and environment to live MA ids via
   `packages/core/daimon/core/ma_resolver.py`, which self-heals by re-running
   defaults reconciliation when a tag no longer resolves, and rejects an agent
   whose `archived_at` is set.
8. Balance gate — `tenant_balance.is_over_balance`.
9. Monthly cap gate — `billing.is_over_cap`.
10. Channel budget gate — `channel_budget.is_over_channel_budget`, against the
   parent channel, or for a DM the channel it was moved from with `/dm`
   (`dm_source_channel_id`); skipped in an older DM or where the channel has
   no budget. A channel's own agent counts toward its home channel wherever an
   exempt caller runs it (`AgentPermissions.budget_channel`). The channel is
   carried on `Admission.channel_id` so every debit
   for the turn is attributed to it. The window's first refusal also
   DMs the channel's admins through the adapter's `TurnDeps.budget_notifier`
   (`daimon.core.channel_budget_notice`).

The policy, writers, external participant, balance, cap and channel
budget gates each raise `AdmissionDenied` with a reason literal, which
`admission_refusal_text` words in each platform's nouns. See
[billing.md](billing.md).

A Slack `app_mention` runs a turn only when its text contains the bot's own
`<@U…>` mention token, follow-ups in a thread included. Slack's docs say the
event fires only on a direct mention, but installs have reported it arriving
for thread replies that never mentioned the bot, so `_handle_app_mention`
checks the text itself, as Discord checks `message.mentions`. The bot's user
id comes from `auth.test`, cached per workspace. The check runs after dedup and
the token read and before the Slack Connect rejection, so an external sender
who never addressed the bot gets no notice. A dropped event is logged as
`slack.event_dropped.no_explicit_mention`; a failed `auth.test` drops the event
without an error reply.

After admission, Slack resolves the answering agent's message name and avatar
once for the turn. The status card and each answer or continuation post carry
that identity in Slack's message header. The built-in Daimon agent uses the
app header. A missing face is queued for generation after the turn proceeds;
the current picture remains in use until it is stored. Slack is expected to keep the header when the status card is edited into an
answer; each new turn post is recorded under the turn's agent and card intent.

Discord starts a turn on a direct bot mention or a reply to a recorded bot or
application-owned webhook post in the same tenant and channel. The usual
admission path follows either trigger. Agent turn posts use a pool of up to
three application-owned webhooks per text or forum channel, with each thread
assigned by its ID; built-in Daimon posts use the bot. If a webhook is
unavailable, the first answer chunk carries a bold agent name. The bot and MCP
Discord tools resolve the webhook matching a message's webhook ID to edit or
delete that agent's recorded post.
Restart recovery keeps a card intent active when a pending card cannot be
edited. It does not post a replacement status card, because the original
button could remain. A definite missing-webhook, missing-token, permission or
deleted-thread failure can mark an aged intent unrecoverable. So can a configured
number of failed recovery passes, counted across restarts. An hourly pass
revisits aged intents without waiting for another restart. It does not overlap
startup reconciliation or inspect a turn still running in this process. The
unrecoverable age setting must exceed the turn ceiling. If the bot can
manage messages, it fetches each known matching card and deletes it only while
it still carries that turn's pending button. Answered cards without that button
stay intact. An isolated transient API failure and shutdown cancellation keep
the intent active for another recovery attempt. An unprompted turn likewise retains its
intent when its card delete fails, including an unknown-webhook error.

An unmentioned reply in a Discord or Teams thread costs one cascade read of
`thread_participation_scopes`. In a followed thread it joins a quiet-timer
batch; once the thread goes quiet the shared gates in
`packages/core/daimon/core/participation_gates.py` run the hourly cap, balance,
cap and channel budget checks, then the metered classifier. A `respond`
verdict runs an ordinary turn through `admit()` as the burst's newest author,
with every notice withheld. Teams also counts a quote of the bot's message as
a mention.

Discord checks its per-guild in-flight limit before the optional process-wide
turn limit (`DAIMON_DISCORD__MAX_CONCURRENT_TURNS`). Guild mentions, unprompted
replies and DMs count against it; an excess requested turn gets a retry notice.
Unprompted replies follow their existing silent-refusal policy. Continuation
wakes retain their existing admission path. The limit is unset by default and
applies only to this Discord process.

Discord, Slack and Teams also limit simultaneous chat turns per tenant before admission.
`daimon tenants turn-cap PLATFORM WORKSPACE_ID N` stores a tenant override;
`default` clears it and restores the adapter's deployment setting (3 by default).
The check covers Discord mentions, thread participation and wizard submits,
Slack mentions, and Teams messages, wakes and thread participation. It does not limit MCP or routine turns.

A channel with `writers: none` hears nothing from the agent, not even a
refusal or an error. Each turn entry decides FIRST, before tenant liveness, provisioning or
any other read that can fail, whether the agent may post there:
`protection_state` (`packages/core/daimon/core/turn/protection.py`) returns
`unprotected`, `protected` or `unknown`, and never raises -- a policy that
doesn't parse, a database or pool failure, or a failed category lookup all
give `unknown`. The channel and its parent are checked first; a Discord
thread's uncached parent is fetched for its category only when a category
has a rule and the channel isn't already closed, and cached so
admission doesn't fetch it again. Anything but `unprotected` drops the turn
with only a log line, and the entries' error boundaries post only when the
state is `unprotected`. The entries are Discord `on_message`, organic thread
participation, wizard submit and continuation turns, and Slack
`_handle_app_mention` (with a second check in `_orchestrate` after it claims
the thread, and on its ephemeral shed notice), and Teams `_handle` and
thread participation (plus the refusals `handle_message` posts). The continuation dispatchers (Teams uses
core `continuity/dispatch.py`), which can post skip or responder-changed copy
outside any turn (from the wake poller or a credential submission), ask the
same decision right before each post and settle the row skipped without
posting when it isn't `unprotected`.

**Tenant access policy — `packages/core/daimon/core/access_policy.py`.** One
`TenantAccessPolicy` per tenant, stored as JSON in `tenant_access_policies`
(`packages/core/daimon/core/stores/access_policy.py`). A tenant with no row
gets the open default, so nothing changes until a policy is written; a present
row that is not a valid policy object (JSON `null` included) raises
`AccessPolicyUnreadable` and callers refuse rather than fall open. Unknown
fields are rejected too, so rolling back past a release that added a field
locks the tenant out until the row is rewritten.

Who counts as an admin differs by path. `admit()` trusts only the live role the
adapter passes; no role means non-admin. The MCP turn tools (`ask`,
`start_turn`, `continue_turn`, on the hub and per agent, plus billed media)
and routine fires have no live platform role, so they use the account's
stored role (and role ids, for channel admins), refreshed on every chat turn. The operator path
(`platform_user_id` unset: CLI and internal tokens) is not a platform member
and skips the policy, as it skips billing. An account marked external is
never an admin or a channel admin on any path: admission stores no group ids
for it, the MCP verifier reads the mark from the row on every request, and
the identity middleware refuses it the setup, channel setup, credential and
direct-message tools (`middleware/external_participants.py`). Ids are the
platform's own (Discord snowflakes, Slack ids, Teams Entra object and
conversation ids):

| Field | Empty means | Enforced by |
| --- | --- | --- |
| `invoker_user_ids` | anyone may start a turn; admins always may | `admit()`, the MCP turn tools (`_admit` in `tools/_ctx.py`), routine fires |
| `channel_rules`, `category_rules` | every channel open | `authorize()`, asked by `admit()` (every path: mention, follow-up, wizard submit, continuation) and `reauthorize`, every Discord, Slack and Teams write tool (`require_channel_writable` in `packages/adapters/mcp/daimon/adapters/mcp/tools/_channel_policy.py`), the channel read tools via `ChannelReadPolicy`, the session transcript tools via `tools/_session_access.py`, the routine save and fire checks, every channel default bind, and visibility in the agent, skill, routine, routing and handoff tools, the hub, the setup panel and `/memory` |
| `agent_rules` | every agent runs wherever the cascade sends it | `admit()` after the agent is retrieved (every turn path, DM included), the MCP and hub turn tools, `hand_off_task` via `decide_handoff`, `fork_agent`, and routines at save (`_check_agent_pin`) and at every fire (scheduler) |
| `dm_memory_read_only` (default `false`) | DM turns get writable memory | `admit(is_dm=True)` sets `Admission.memory_read_only` |

[Permissions](permissions.md) defines the rules and every limit they set. A
row an older build wrote (protected, sealed and isolated channel lists and
channel pins) reads as the same rules; writes store rules.

`writers: none` covers threads under the channel and, on Discord, channels in
such a category; it applies to admins too, and runs after the caller's own
permission check so it never reveals a channel the caller cannot see. A
channel whose readers are `inside` or `own` is readable only from a turn
inside it (the channel or a thread under it): the read must pass the
`origin_context_id` from that turn's controls, and a missing, expired or
foreign origin counts as outside. The origin must also belong to the agent
the token executes as (`agent_id`, or `chat_agent_id` for ordinary chat); a
token bound to neither can't claim one. A single thread can take
`readers: inside` on its own: a Discord thread by its id, a Slack thread as
`channel_id:thread_ts`. Such a thread, its messages and (on Discord) its name
are withheld from outside turns: in `read_thread` and `get_message`, in
channel history (a Slack thread's root and broadcast replies; Discord's
thread-created notice, whose text is the name), in the channel context a Slack
top-level mention starts with, in `list_threads` and in search. Once any
readers are limited, search reports only the hits it shows as its total,
scoped or not, on both platforms, and Discord hints at more results only when
a full page of visible hits came back, so neither the count nor the hint can
reveal hidden matches. A turn inside a limited channel or thread gets
read-only memory. An origin is any active one of the same account and
responder, not only the current turn's: a member who copies an origin id out
of such a turn can read that channel from elsewhere until it expires --
someone who could read it anyway. Outside reads are refused after the
platform's own caller check, and search drops hidden hits.

A session transcript holds everything its turns saw, so the transcript tools
apply the same limit. `admit()` records the turn's channel and thread on the
`Admission`, with every id limiting its readers (its channel and a thread with
its own rule), and `create_session` stamps them on the session
(`daimon_channel`, `daimon_thread`, `daimon_sealed=<ids>`;
`daimon.core.session_seal`). The recorded ids only grow: a limited turn that
reuses a session adds its id (`bind_session`, which waits rather than run the
turn if Managed Agents refuses the update mid-turn), and a session that
replaces another -- by transcript, checkpoint, bundle, handoff or dead-session
recovery -- inherits its predecessor's ids, or is limited to its own thread
when the predecessor can't be read. Each read or follow-up requires the
calling turn's origin to be inside every recorded id, whatever the current
policy, and judges the channel and thread against the current policy as a
channel read would: the main MCP server's session tools take the calling
turn's `origin_context_id`, claimable only by a chat turn's own credential;
agent-chat keys run outside every channel, so they never list, read or
continue a limited conversation, and the hub does the same for members. A
workspace admin may list and read any limited conversation from the hub, and
a channel admin those of the channels they administer whose every recorded id
lies there too (the channel, a thread in it, or its `channel:ts`), but
continue none (see [Trust model](#trust-model)). Limiting a channel's readers
later covers its existing sessions, and opening it never releases a session
that ran limited -- only that thread, when the thread had its own rule. A
session from before the stamp that a thread ran on (`thread_sessions`) has no
known parent channel: while any readers are limited it is shown only to a turn
in that same thread. Nor can a limited turn open or drive another session to
carry its content out: agent chat's `start_turn`, `ask` and `continue_turn`
are off the surface a chat turn's token (`chat_agent_id`) sees, and refuse
that credential outright if they are ever reached with it
(`_require_outside_chat_turn`), so every session they create comes from a
headless caller outside every channel. The one exception is an agent key
minted with "Use from your coding tools" in a channel with limited readers, or
in a channel its agent's rule names (a thread counts as its parent; Teams
panels live in the 1:1 chat, so its dialog asks which channel): its
`mcp_tokens` row records that channel (`coding_token_channel`), and its calls
run as a turn there -- under the channel's rules, environment and budget, with
its sessions stamped to the channel (`token_channel_id`). `authorize` sees it
as the place of the key's turns (`mcp_place`) and as the read origin, nowhere
else, and re-decides both at the moment of action; the rule is read right
before a session is created, and a conversation opened before its channel's
readers were limited can be read but not continued. Keys minted anywhere else
are unchanged. A server admin mints anywhere; a channel admin of every channel
an agent's rule names mints for it only from inside one of those channels, and
that token is always bound there (`authorize(MINT_CODING_TOKEN)`, through
`authorize_coding_token`).

Rules are set by server admins and operator tokens (`channels:write`), never
a channel admin: in chat with `set_channel_rule` and `set_agent_rule`, on the
channel's **Permissions** screen (from Who answers where in the setup panel;
Teams: the Channel settings dialog), or with the CLI. Each write is one transaction under the
policy lock (`packages/core/daimon/core/channel_rules.py`):

```bash
daimon channels rule set discord GUILD_ID CHANNEL_ID --readers own --writers own \
    [--copy-from AGENT]            # keep it to its own agents
daimon channels rule set discord GUILD_ID CHANNEL_ID --readers inside
daimon channels rule set discord GUILD_ID CHANNEL_ID --readers any [--release-agents]
daimon channels rule set discord GUILD_ID CATEGORY_ID --category --writers none
daimon agents rule set discord GUILD_ID AGENT --runs-in CHANNEL_ID [--runs-in ...]
daimon agents rule set discord GUILD_ID AGENT --anywhere | --nowhere
daimon tenants access-policy rules discord GUILD_ID [--json]
daimon tenants access-policy get discord GUILD_ID [--json]
daimon tenants access-policy set discord GUILD_ID --invoker USER_ID [--dm-memory-read-only]
daimon tenants access-policy set discord GUILD_ID --clear [--drop-agent-rules]   # back to open
daimon tenants access-policy set teams ENTRA_TENANT_ID \
    --add-member-guest OBJECT_ID [--remove-member-guest OBJECT_ID]   # guests as members
```

An agent with a rule runs only in its listed channels and the threads under
them. The rule is keyed by agent name and checked against both the cascade's
name and the agent's own metadata name, so a thread handed to the agent by id
is covered; setting a rule replaces one on the agent's other names. A turn
anywhere else is refused with `runs_elsewhere`, a member's DM included.
Admins are exempt only where the reply reaches no one else -- their own DM
and hub turns -- and even there the agent's channel sends reach only its
channels (see [Trust model](#trust-model)). `hand_off_task` refuses to bring
it into another channel before anything is written. Its routine must post
straight into one of its channels (a channel destination, not a thread or
none), because the scheduler cannot resolve a Discord thread's parent at fire
time; it is refused at save and skipped at fire otherwise.

`fork_agent` is admin-only and refuses a source with a rule: a copy would be
the agent's prompt, skills and connectors under a name with no rule. A fork
also starts with no credentials (no GitHub access, repo binding or proof, and
no agent-wide MCP token), so copying an agent never hands out another
project's access; MCP servers that only work with a stored token are left off
the copy. The source's own uploaded skills are copied as new skills of the
fork's.

An MCP or hub turn (`start_turn`, `ask`, `continue_turn`, on a new session or
a resumed one) runs in no channel, so it is outside every agent rule, as a DM
is: the policy is read on every call, and an agent with a rule is refused
before any session is created or message sent. Only the operator's internal
tokens, which carry no platform user, bypass it.

Cross-agent separation is complete only for agents with a rule: an agent
without one still answers wherever the cascade sends it, so give every client
project agent a rule. Beyond the rule, whatever an agent can reach (its repo,
keys, connectors and memory) is also guarded where a member could otherwise
borrow it:

- `hand_off_task` and the Switch to agent button let a member hand a thread only
  to an agent of that channel: the one it answers with, one whose rule names
  it, or one of the channel's own agents. Any other destination needs a
  server admin, or a channel admin of the parent channel when the thread's
  readers aren't limited and they could make the agent its default: the same
  rule, which also refuses a move that would take it out of another channel
  admin's channels while it is theirs (below, and
  [Same-thread handoff](#same-thread-handoff)).
- `create_routine` and `update_routine` let a member schedule only the agent
  they are talking to, or the agent the destination channel answers with.
  Routines are listed and read only by their creator and admins.
- `fork_agent` is admin-only and forks start credential-less (above).

A rule naming a channel kept to its own agents must name it alone, and makes
the agent one of them only if it could be the channel's own (custom, and
answering nowhere else); a channel's own agent keeps its rule until that
channel's readers change. An agent without a rule runs anywhere, so dropping
one is explicit (`--anywhere`, `runs_in: null`). `access-policy set --clear`
needs `--drop-agent-rules` while agent rules exist. Rule names must match an
agent of the tenant. Every supplied id is validated before writing: Discord
ids are 15–21 decimal digits; Slack user ids start with `U` or `W`, channel
ids with `C` or `G`, followed by uppercase letters or digits (a Slack thread
rule is keyed `channel_id:thread_ts` and only takes `readers: inside`). A
Teams rule names a whole `19:…@thread.tacv2` channel; a thread id names its
channel. Invalid input names the field and value and writes nothing.

A private DM conversation (`dm:` scope) is outside every agent rule wherever
it is checked: admission, `hand_off_task`, and continuations owed to a DM,
which are admitted as DM turns. Adding a key, connector token, skill-repo
token or repo binding to an agent with a rule (`request_agent_key`,
`request_mcp_token`, `request_mcp_oauth`, `request_skill_repo_token`,
`request_repo_binding`) or pointing it at a public repo (`bind_public_repo`)
needs an admin, a channel admin of every channel its rule names, or a request
made inside one of those channels. The form's submit (Discord, Slack and
Teams) re-checks the rule against the agent as it is now, resolved by its
stable id and checked by every name a rule can be keyed by
(`core/agent_pins.py`), so a rule added later or a rename still holds; a
target that can't be resolved while agent rules exist is refused. The direct
configuration tools (`update_agent`, `attach_mcp_server`, `detach_mcp_server`,
`remove_agent_key`, `remove_skill`) check it with no turn origin (an
`origin_context_id` only finds the agent), so on an agent with a rule they are
an admin's or a channel admin's of every channel it names; members inside its
channels use the request tools. An agent key's self-edit tools
(`set_repo_binding`/`clear_repo_binding`/`self_write_file`/`self_delete_file`)
are refused on an agent with a rule: an agent key's stored roles are never
trusted, so neither exemption applies to it. One guard
(`tools/_pin_guard.py`) serves all of them. A sign-in (`request_mcp_oauth`)
is re-checked when its callback arrives, before any grant or attach. Routines
are checked against every name of the agent they run (at save and at every
fire, after the scheduler self-heals to a replacement agent), and so is
`hand_off_task`'s destination.

**Channel admins.** A tenant can name, per channel, groups and members who run
that channel on top of the server admins (`channel_admins`,
`packages/core/daimon/core/channel_admins.py`). A group is a Discord role, a
Slack user group, or a Teams team, whose owners it admits; a Teams member is
named by Entra object id. Discord sends the member's roles with each event.
Slack and Teams look up only the groups some grant names
(`usergroups.users.list`; Graph's team owner list under the
`TeamMember.Read.Group` consent), each cached for a minute, and a failed lookup
grants nothing. `admit()` stores the member's matched group ids on the account
(`accounts.platform_role_ids`) beside the role, so MCP tools test a grant
without asking the platform for Discord roles. After admission, Discord and
Slack turn controls also name a channel admin grant for the current parent
channel; the model can then call setup tools, which independently check the
target agent's reach. This does not change the account's server-admin role.
Every Slack member can edit
user groups by default, so a group grant admits whoever can join it and the
workspace should limit group management to admins; outside a turn (the MCP
verifier, hub reads, an OAuth callback, channel admin DMs) a stored Slack
group or Teams team counts only while a live lookup still admits the person
(`confirm_stored_group_ids`), and a private form's submit, which runs under
the policy lock, ignores them. A stored Discord role is checked there against
the member's current roles (`GET /guilds/{id}/members/{user}`, cached a
minute), since listing a role's members needs a privileged intent; where no
lookup runs it stands. A channel admin may do what a server admin may for
an agent of theirs that is local to their channels -- not the tenant default or anyone's
personal default, answering or running somewhere and only in channels they
run (channel-scope rows, thread bindings, and other people's live sessions
and routines, each by its channel), and no unattended run of it owed to a
server admin or another channel's admin
(`packages/core/daimon/core/agent_reach.py`) -- and may set or clear those
channels' default agent (never a budget or the tenant balance, which stay
with server admins: `SET_CHANNEL_BUDGET`). An agent is theirs when a channel
admin made it from one of their channels (`agent_creation_channels`, written by
`create_agent` from a verified turn origin and by each setup panel's New agent
form), when a server admin's rule runs it inside their channels only, or when a
server admin set it as one of their channels' default
(`channel_config.agent_name_set_by_admin`; migration 0044 backfilled it from
each setter's role at upgrade time, not when they set it); any other agent needs a server
admin on every surface (`channel_admin_holds`). Handing a thread to an agent makes it answer
there as a default would, so binding and handoff are one rule
(`authorize(BIND_CHANNEL_DEFAULT)` and `authorize(HAND_OFF)` with `load_binding_reach`):
neither may take an agent out of another channel admin's channels while it is
theirs, read over all their user and group grants together. It guards these two
moves only; a member's routine, for one, still adds a channel to an agent's reach. A `/dm` conversation counts as the channel it was
started from. A session counts in the channel recorded when it was created
(`thread_sessions.channel_id`) and in any its spend was attributed to, and a
routine in the one its spend counts against; one with none recorded could run
anywhere, and the refusal says so. An agent answering nowhere is local to
nobody, so locality only narrows what key and MCP server replacements and
removals and skill repo connects count as shared, never past it. In `daimon.core.authz` a channel admin
is `Subject.administered_channel_ids`, filled from the stored grants and never
`is_admin`: configuring an agent with a rule from anywhere is theirs once
they administer every channel each of its rules names (a rule naming no
channel stays with server admins), and so is minting it a coding-tools token bound to one of
those channels (never an unbound one). A channel admin binds only a shared agent
(managed or tenant-wide), or one a channel admin made from one of their
channels or a server admin's rule runs inside them; a server admin's default does
not make an agent theirs to move, and another channel's own agent never is. No chat tool, panel or CLI
write (`daimon config set`, `daimon config propagate`) binds an agent with a
rule as the default of a channel outside it, for server admins too
(`authorize(BIND_CHANNEL_DEFAULT)`). Managed agents
and the tenant default stay with server
admins, and a tenant with no grant behaves as before. Stored role ids refresh on
the member's next chat turn; until then MCP calls, a coding-tools token
included, keep the old grant. Unattended runs are routines and queued wakes
(timers, handoffs, applied private input); each fires with its requester's
rights, so a channel admin's edits reach them as they reach anyone chatting
with the agent. A member's run carries that member's own read visibility, as
their chat does, and a server admin who chats with an agent a channel admin
edited runs its instructions with their own rights, as with any agent someone
else wrote. The check reads requesters' rights at edit time, as stored at
their last chat turn: a requester promoted later runs earlier edits with the
new rights. Server admins edit grants
with the `*_channel_admins` MCP tools, from Who answers where in the setup
panel, or with the CLI (`discord`, `slack` or `teams`; a Slack or Teams thread
id names its channel):

```bash
daimon channels admins get discord GUILD_ID [CHANNEL_ID] [--json]
daimon channels admins set discord GUILD_ID CHANNEL_ID --role ROLE_ID --user USER_ID
daimon channels admins clear discord GUILD_ID CHANNEL_ID
```

**Channels kept to their own agents — `packages/core/daimon/core/channel_rules.py`.**
A channel C with `readers: own` keeps its own agents to itself: those whose
agent rule names C alone (`agent_permissions(...).home`, by every name the
agent carries). `set_channel_rule` does it in one write under the policy lock:
it sets C's rule and gives C's default agent a rule naming C. The default must
not be built in, have a rule naming elsewhere or answer anywhere else
(`agent_reach`, checked at write time only); otherwise the call is refused
with the reason, unless asked for a copy. Then `copy_from`, or whoever answers
in C, is copied by `agent_fork.copy_agent` (`authorize(FORK)`: an admin's
call, never an agent with a rule; no credentials, no agent-scoped skills,
which the reply names) under a name from the channel, made C's default and
given its rule; a copy the locked re-check refuses is archived. Moving readers
off `own` keeps the agents' rules unless `release_agents` drops them, and warns
that they keep what they remembered in C.

Enforcement is `authorize()`'s: it fills `AgentRef.permissions` and
`Place.permissions` from the policy ([permissions](permissions.md)), so every
check and re-check is fresh. In C only C's own agents run, post, read, get
routines or become the default (`own_agents_only` on `RUN_AGENT`, `POST`,
`READ_CHANNEL`, `SAVE_ROUTINE` and `BIND_CHANNEL_DEFAULT`); a setup thread
under C (`Place.setup_thread`) still answers as the built-in agent. C's own
agents post nowhere outside C, not even the requester's DM, and send no direct
messages (`DIRECT_MESSAGE`) or create agents (`CREATE_AGENT`, also refused for
any call whose verified turn origin is in C, such as its setup thread), whose
prompts would answer outside C. Publishing (`PUBLISH`: `publish_report`, the
notebook and attachment upload URLs and `set_display_identity`, each seen
outside C) from C, or by any agent with a rule, waits for the requester's
Approve ([publishing](permissions.md#publishing)); a chat turn naming no
verified origin while some readers are `own` is refused, as for
`CREATE_AGENT`. Admission, `reauthorize` and the scheduler's fire check (the
resolved agent, by every name, at the routine's destination) decide through
`RUN_AGENT`; thread participation skips a refused turn before its classifier
runs. Memory stays writable for C's own agents in C and is read-only for any
other agent there, as with `readers: inside`; a session opened with a
coding-tool token bound to C follows the same rule. A session whose recorded
ids lie in C is read and continued only by C's own agents (`READ_SESSION`,
`CONTINUE_SESSION`). A verified turn origin in C holds the call to C whatever
agent runs it (`origin`): its posts, cards and routines stay in C and it sends
no direct messages; only its setup thread may still configure C's own agent.
Held to C, by its own agent or such an origin, a call also reads only C and
the sessions that ran there (`own_agents_only` on `READ_CHANNEL`,
`READ_SESSION`, `CONTINUE_SESSION`; `home_hold`); a session with no channel
stamp counts as outside C. A chat token names no turn, so while one of its
turns runs in C a call is held there whatever origin it names (`_held_origin`;
turns running in two such channels refuse it). Held, `list_channels` and the
search tools name nothing else, so nothing read elsewhere, a prompt planted in
C included, is reposted into C. A thread routine saved without its parent
channel is treated as inside any such channel until delivery places it
(`Place.parent_unresolved`). The rule constrains agents: a caller with no
executing agent (an operator token, the CLI) may still post into C, which is
input, not a leak; the readers rule keeps reads inside.

What callers see follows from where they stand. An MCP call is inside C when
its verified turn origin is in C, when it carries a channel-bound coding-tool
token for C, or when its chat turn's agent is one of C's; an agent key is
never inside by its agent alone. The roster, agent and key tools take the
turn's `origin_context_id` for this. A chat turn whose agent can't be found
is refused while some readers are `own`, since it may be one of C's. From
outside, C's agents are missing from `list_agents` and every by-name lookup,
from handoff destinations, `explain_agent_resolution` and the hub, and so are
their agent-scoped skills, their routines, routines posting into C and timers
set in C; inside C only C's agents show. For members the setup panel's
roster, details and Who answers where are filtered the same way at the
panel's location, and `/memory` hides an agent wherever it may not run;
server admins see everything. Server admins are exempt in their own DM and
hub, and a channel admin of C there too, but C's agents still never post
outside C. `get_tenant_summary` lists each channel with `own_agents_only`.
Limits: tools on other MCP servers don't see the policy; an agent created
inside C isn't C's own until its rule names C; `/dm` from C is refused; a call
is held to C only where its tool takes a verified origin (not the send, DM,
self-edit or routine edit tools), and a call that names none is judged from
outside.

When C closes, `archive_channel_copy` (server admins, or the `agents:archive`
operator scope; `core/channel_copies.py`) archives the copy `set_channel_rule`
made for it, stamped `daimon_isolation_copy`, with its rule and default in C.
It archives no other agent and no default, and refuses a copy whose rule or a
default names anywhere but the channel named as closing. C keeps its rule, so
nothing answers there after.

**Channel environments.** The environment a turn runs in resolves over the
same tiers as the agent but on its own (`_pick_environment` in
`packages/core/daimon/core/scope.py`), so a channel can keep its agent and run
it with the packages one team needs; routines follow the channel they post to.
Server admins set any channel's environment, or the tenant default by omitting
the channel, with `set_channel_environment` and `clear_channel_environment`; a
channel admin sets the channels they run, and a thread id resolves to its
parent. Who answers where in the setup panels lists each channel's
environment and gives server admins and this channel's admins a select for it
(on Teams, in the Channel settings dialog, for a channel picked there)
(`packages/core/daimon/core/channel_environments.py`); rows on the other
side of a channel kept to its own agents are dropped, and inside one the
workspace and deployment environments too. The name must match an existing
environment in the tenant, looked up once so the network rule and the write
judge the same one; conversations pick it up from their next message, keeping
their files and their recorded readers, and `explain_agent_resolution` reports each tier's
environment. `authorize(SET_CHANNEL_ENVIRONMENT)` decides every pick: in a
channel with limited readers, or one holding such a thread (a Slack
`channel:ts`, or a Discord thread the pick names or a session ran in under
it), an environment with unrestricted
networking (any network beyond package managers and MCP servers: anything but
a cloud environment on limited networking with no allowed hosts) needs a
server admin, and so does clearing a pick onto a default that has one. Even a
server admin's such pick waits for a confirmation (`EnvironmentPick.needs_confirm`):
`set_channel_environment` and `clear_channel_environment` take
`confirm_open_network`, which the model passes only once the caller confirms,
and the panels write nothing and point to chat. Changes beyond one channel ask
the same when they move a channel with limited readers onto such a network: a
workspace default that such channels without a pick of their own follow, an
`update_environment` that opens the network of an environment one runs in, and
an `archive_environment` whose cleared picks fall through onto one; both tools
take `confirm_open_network` too. A limited Discord thread no session has run in
yet counts as following the workspace default. A pick
made before the rule never met it, so limiting a channel's readers when its
own pick is open warns that a server admin should confirm it; who made
a pick isn't recorded. An operator token's
`channels:write` covers a channel's environment, never the tenant default. An
environment name only channels kept to their own agents across the reader's
line pick could name a client, so `list_environments`, `get_environment`, the
environment-changing tools and the panel pickers treat it as missing, and
`get_tenant_summary` blanks it, with other such channels' agent names
(`hidden_environment_names`); operator tokens, and server admins on the
panels, see every name. A
channel with no environment of its own falls through, so nothing changes until
one is set. Chat over MCP has no channel, so it uses the tenant or deployment
default. The channel tools read a Slack or Teams thread id as its channel and
a Discord thread through a lookup (`tools/_channel_target.py`); budgets and
environments also check the caller can see the channel.

**Channel skills — `packages/core/daimon/core/channel_skills.py`.** A channel
can add skills to whatever agent answers there, for its turns only, so a
shared agent carries one team's skill without every channel getting it.
Server admins and operator tokens (`channels:write`) add and remove them,
never a channel's own admins (`authorize(SET_CHANNEL_SKILLS)`), with the
`*_channel_skill(s)` MCP tools, Who answers where in the Discord and Slack
setup panels (the Channel settings dialog on Teams), or the CLI:

```bash
daimon channels skills list slack TEAM_ID [CHANNEL_ID]
daimon channels skills add slack TEAM_ID CHANNEL_ID SKILL
daimon channels skills remove slack TEAM_ID CHANNEL_ID SKILL
```

A channel may add a library skill of its tenant, or one uploaded to the agent
answering there now, unless that agent is another channel's own.
The row (`channel_skills`) pins the latest version at add time; adding it
again picks up a newer one. Admission reads the rows once per turn
(`turn_channel_skills`) and drops another agent's upload, a skill the agent
holds, a clashing mount name and anything past the session cap; the session
is created with the agent's skills plus these, and the drift check hashes the
same list (`session_snapshot.session_skills`), so adding or removing one
replaces the conversation's session on its next message. Routines, headless
runs and MCP chat don't use them.

**Skill uploads.** One skill can be added to one agent by hand
(`packages/core/daimon/core/skills/ingest.py` checks it, `skills/add.py` adds
it): a pasted SKILL.md, a `.md` or `.zip` attached on the caller's own platform,
or a GitHub folder read through the skill-repo fetch. `daimon skills add --agent
NAME PATH|URL` adds a local folder, SKILL.md or `.zip`, or a public GitHub folder,
from the CLI: it previews, asks unless `--yes`, and decides as a server admin
(never a built-in agent, the agent rule asked again just before the upload and
the attach). Archives refuse links,
absolute or `..` paths, encryption and the repo sync's size caps; the
frontmatter needs a lowercase name and a bounded description. The skill is
uploaded under the agent-scoped title, never the shared library. A name a
shared or built-in skill already holds, or one that would load under the same
folder as an attached skill, is refused. A fork (`copy_agent`, also behind
`set_channel_rule`'s copy) uploads the source's own skills again under the fork's
title and upload row, so the two never share a skill id; another agent's
skill, or one that fails to copy, is left off and named, and the fork still
succeeds. One attached by id or kept by an older fork may still be
shared, so a skill another agent also has attached is never versioned; the
upload must take a new name. Its `user_skills` row has `source = "upload"`, an origin (a GitHub
origin is `owner/repo/path@branch`, never the URL as typed) and the adding
account, so no repo sync replaces, deletes or re-attaches it, and removing the
skill forgets it. `add_skill` previews first and adds only when called again
with the preview's hash, which is bound to the target agent, and only after the
person presses Approve on the confirmation card (below). The server checks the
card can exist: the confirm needs a verified origin whose live session runs the
origin's responder with `add_skill` on `always_ask` (`has_confirmation_gate`),
as MA reports the session or, only when it reports the agent's own tools
(leaving the per-session overrides out), as the bind recorded sending them. Without that, as with tool safety off, an
`agent_chat` session or one whose tools have not caught up yet, a chat
confirm adds nothing; the preview and the refusal say which case it was and
point to Add skill in
the setup panels' Details (Discord takes a paste or a file, Slack a paste),
where the person's own submit is the approval. The `skill_add` and
`skill_remove` operations follow the shared-agent rule
(`authorize(CHANGE_SHARED_AGENT)`, spec family): built-in agents never, server
admins on any other, channel admins on agents of theirs local to their channels, anyone
on agents nobody else uses. Sharing is read as widely as a key change
(`WIDE_SHARING_OPERATIONS`): a default, a bound thread, someone's personal
default, or another member's routine or live session. Prompt and setup edits
(`agent_spec_edit`) and repo binds read sharing the same way, so an agent that
only an admin's routine or a bound thread runs is not a member's to change. An
agent with a rule takes a
chat add only from a verified origin in its channels (`require_pin_write_access`
with the card's origin), and a panel add only from its channels' panels
(`pin_refusal` with the panel's channel and thread) at the button, the submit
and the Add; `remove_skill`, like the other direct configuration tools, passes
none. Rules and sharing are checked again on the fresh agent just before the
upload and the attach, and refusals never name another agent. The target resolves from the turn's channel, so a
setup thread under a channel kept to its own agents reaches only them, and
nothing outside reaches them. Their uploads are hidden outside it like its other agent-scoped
skills, keyed by the upload row's agent.

**Stage two, `bind_session()` — `packages/core/daimon/core/turn/prepare.py`.**
Finds the live `thread_sessions` row for this thread or creates a fresh MA
session, assembles every `create_session` argument (credential env mount, MCP
vault, repo resource, memory store), writes the mapping row, and binds the
usage recorder. It returns a frozen `PreparedTurn` whose recorder field is
underscore-prefixed: adapters never construct billing wiring, and the only way
to reach the recorder is to hand the `PreparedTurn` back to stage three.

Whether an existing session may be reused, refreshed in place, or must be
replaced is decided in `packages/core/daimon/core/session_preparation.py`
against the fingerprints stored on the mapping row; the outcome rides back on
`PreparedTurn.continuity` so the adapter can say what happened.

GitHub App mode uses the agent's live grants and the current asker's linked
GitHub permissions when assembling a turn. External askers get no App token.
Each effective installation and permission profile gets an ID-scoped token,
recorded before minting and checked again after delivery. The session mounts
one repository resource per effective repo and its own vault for `GH_TOKEN`
and Copilot. A changed repository set replaces the session; unchanged mounts
receive refreshed tokens through resource updates. Legacy sessions continue to
use their existing binding and credential path.

**Stage three, `run_prepared_turn()` —
`packages/core/daimon/core/turn/run.py`.** Calls the driver, and on a
dead-session 404 recovers exactly once: mark the mapping dead, create a
replacement, read the dead session's event log back into the reseeded message,
rebind the recorder, re-run. A second dead signature is returned as-is.

Both stages two and three are wrapped by one shared per-turn ceiling from
`packages/core/daimon/core/turn/ceiling.py` — `TURN_CEILING_S`, 45 minutes. It
is a backstop against an MA session that never leaves `running`, not a latency
target; legitimate turns that fit a model or build a notebook run for many
minutes. `admit()` sits deliberately outside it.

### The driver

`packages/core/daimon/core/turn/driver.py` opens the SSE stream, posts the
user message, and runs a consume loop and a render loop concurrently until the
session goes idle or errors. Adapters plug in through the `TurnLifecycle`
protocol in `packages/core/daimon/core/turn/lifecycle.py`, which documents a
per-hook cost contract:
`on_render` is the answer-text delivery path and may talk to the network,
because it runs on its own task and cannot stall the pump; `on_sse_event` is
awaited inline in the consume loop and must stay a cheap local tap.

After a tool-using Discord or Slack turn, the adapter starts a detached,
per-MA-session-chained sweep of downloadable session files through
`daimon.core.output_delivery`. It posts each file into the conversation thread
before deleting its MA listing entry. Failed posts stay listed for a later
sweep. Discord uses the guild's upload limit, skips oversize files with an
in-thread notice, and checks the channel's writers before posting.

Reconnection is two loops for two failure modes. The outer loop handles
eventless cycles — the server closes cleanly roughly every ten minutes by
design — and asks MA whether the session is still running before reconnecting,
so silence can never be mistaken for a truncated success. The inner
`AsyncRetrying` block is a bounded two-attempt budget for a genuinely dropped
connection. The outer loop has no attempt cap; the per-turn ceiling is its
only backstop.

Every call must declare a billing posture, from
`packages/core/daimon/core/turn/posture.py`: `Billed` meters each
`span.model_request_end` event through the bound recorder, `BillingExempt`
meters nothing and logs why.

It also declares a tool-confirmation posture: what the driver does when MA
pauses the session on a `requires_action` idle. `RequireApproval` ends the
turn, `AutoApprove` allows every blocked call, and `PolicyApproval` asks a
decider per call and sends each answer on the same stream. The decider comes
from `packages/core/daimon/core/turn/approvals.py`, over the pure read/write
model in `packages/core/daimon/core/tool_safety.py`. With
`DAIMON_TOOL_SAFETY__ENABLED` on, `create_session` sends every attached
third-party toolset as `always_ask` (a per-session override, so it holds
however the agent was written); reads then run, a write in a routine is
refused unless the operator allowed it there, and a write in chat waits for
the requester to press Approve on a confirmation card. The card is a platform
hook: `run_prepared_turn(confirm_write=...)` takes a `ConfirmationHook`
(`packages/core/daimon/core/confirmation.py`), Discord, Slack and Teams each draw the
shared card from `packages/core/daimon/core/posted_controls/confirmation.py`,
with action-specific copy and a Details control with plain labelled inputs.
A pause shows one card per blocked call, and the driver sends one
`user.tool_confirmation` event per call after its answer. An adapter that
passes no hook gets `no_confirmation_surface`, which
refuses the write. Plugins can build their own `ConfirmationPrompt` and call
the same hook. Daimon's own `daimon-mcp` tools are not gated here (they keep
their `operation_policy` checks), except `add_skill`: its confirming call, the
one naming a preview's `content_hash`, is sent `always_ask`, so it waits on
the same card in chat and is always refused in a routine, whatever
`unattended_writes` allows. Each bind compares a reused session's tools with
the gated ones a new session would get and updates them in place, so a
session started before tool safety was turned on is gated from its next turn,
and a tools change never writes the agent's own `always_allow` back. The
exemption holds only for the deployment's verified
endpoint: with the policy on, `create_session` re-points a `daimon-mcp` entry
naming any other URL at `DAIMON_MCP__PUBLIC_URL`, and without a public URL the
reserved name is gated like any other server. A pending card is owned by the
turn: stopping the turn, a replayed pause, or the turn ceiling cancels the
decision, refuses the call and retires the card, so an Approve that arrives
after Stop never runs anything. That cleanup is best effort within a few
seconds per step (`CLEANUP_BUDGET_S`): a chat platform or MA that stops
answering cannot hold a turn past its ceiling or a Stop.

### How a turn ended

`packages/core/daimon/core/turn/termination.py` defines `TerminationReason`,
one closed enum for every way a turn can end: it completed, the user stopped
it, the stream or MA failed in one of several named ways, the ceiling fired, or
admission or binding refused it before a driver ran. Each driver finalizer, and
both ceiling handlers, set `TurnState.termination` before the terminal hook
fires, so a lifecycle and the caller's `RunOutcome.termination` always agree.
Refusals raise before any state exists; `termination_reason(err)` maps the
exception the adapter caught to its member, and never raises: anything it does
not recognise is `unknown`. Each `AdmissionDenied` reason with a member of its
own (balance, cap, channel budget, writers none, agent rule, own agents only) maps to it, and
`denial_termination_reason` gives the same member to a gate that decides with
`authorize` instead of raising; the rest are `admission_denied`. Two members have no exception behind them and are
set outside the mapper: `admission_concurrency_shed` by callers when
`should_admit_turn` refuses, and `recovery_failed` by `run_prepared_turn` on
the terminal hook when replacing a lost session raises (the exception it
re-raises maps to `unknown`). A session MA reports terminated without any terminal
event for this turn is `session_terminated`, never `completed`.
`TurnError.kind` is unchanged, and
every `TurnKind` value is also a `TerminationReason` value with the same
string.

`packages/core/daimon/core/turn/notices.py` turns a reason into a
`TerminationNotice`: a short headline, the cause, the tool work still running
and how much had finished, what survived, the next step, and a request id. The
copy lives in core; Discord and Slack draw it in `on_terminal_failure` as the
body of the red card, with the headline as the footer reason, and log the
request id with the underlying error so it is the handle for the detail;
Teams draws it as plain text on the ❌ card. No
lifecycle hook carries it -- the reason rides on the state every lifecycle
already receives -- so the CLI, headless routines and any new adapter keep
their existing failure path, and `TerminationNotice.plain_text()` is the
fallback wording for a surface without markup.
Anthropic's monthly spend-cap response stops SDK retries at the HTTP transport.
If Anthropic reports that cap or a user-set spend limit, Discord, Slack and Teams
show a model usage limit notice and log `anthropic.spend_limit_reached` with
the tenant and limit type.

### Outside text is data

Anything Daimon quotes into a turn from someone other than the person asking
goes through one envelope, `packages/core/daimon/core/untrusted.py`: an
element marked `trust="untrusted"`, opened by a fixed line saying the content
is data, not instructions, with every value escaped so the content cannot
close the element early. The Discord, Slack and Teams context builders wrap
replayed thread history, deltas and channel backfill in it; `fetch_youtube_transcript`
returns its transcript in it; the quoted transcript on a workspace
replacement (`render_previous_session`) uses it too. The channel read and
search tools return JSON rows, so their results carry the same marker as
`trust` and `trust_note` fields instead. The paragraph in the agent guidance
block (`packages/core/daimon/core/agent_guidance.py`) tells every agent what
the marker means. Only the `<user_query>` is the request.

Third-party MCP tool results travel from Managed Agents straight to the model
without passing through Daimon, so they carry no marker; the guidance
paragraph covers them by name ("whatever a tool returns").

## Trust model

[Permissions](permissions.md) has the rules as one model; this section has
who is exempt and why.

Admins are trusted; rules protect members and channels. An agent rule and a
channel's readers rule exist to keep one client's context away from other
people -- members of other channels, and anyone reading where an agent posts
-- not to restrict a workspace admin. So an admin is exempt only where the
output reaches no one but them:

| Surface | Members | Admins |
| --- | --- | --- |
| Channel, thread, handoff, routine that posts to a channel | rules apply | rules apply |
| DM (`admit(is_dm=True)`, Teams personal chats included) | agent with a rule refused | agent rule exempt |
| Hub `ask` / `start_turn` / `continue_turn` | agent with a rule refused | agent rule exempt |
| Hub `list_my_sessions` / `get_session` / `list_events` on a conversation with limited readers | refused | allowed, anyone's; a channel admin's in the channels they administer |
| Hub `continue_turn` / `ask(handle)` on such a channel conversation | refused | refused: continue it in its channel |
| Credential and configuration tools on an agent with a rule | from inside its channels only | allowed (a chat turn's admin, or channel admin of every channel it names) |
| `fork_agent` of an agent with a rule | refused | refused |
| "Use from your coding tools" | refused | allowed (a channel admin of every channel its rule names: bound to one of them) |
| Agent chat and any agent-scoped key or bearer token with no platform user | rules apply | rules apply |
| An agent key minted in a channel with limited readers, or one its agent's rule names | runs inside that channel only | runs inside that channel only |
| A channel's own agent, or any agent from a turn inside a `readers: own` channel | runs, posts, reads and lists only in its channel; sends no DMs; publishes once the requester approves | the own agent also runs in their own DM and hub (so does a channel admin of that channel), but still posts and reads channels nowhere outside it; the hub's conversation reads above still apply |

Channel and agent rules, the invoker allowlist and fork are decided by one
pure function, `daimon.core.authz.authorize` (who is acting, what they want to
do, where the result lands, which agent, which channel); the turn pipeline,
the MCP gates, the channel tools, the routine and handoff tools and the fork
paths gather their facts and ask it, including the hub's admin and channel
admin read of a limited conversation and the refusal to continue one. The
scheduler's writers and invoker checks and the shared-agent replace/remove
table are still decided where they are.

**Decided again at the moment of action.** Admission is a decision about a
turn that has not run yet, so it is asked again, on the policy as it is
then, where it matters:

- `bind_session` and the dead-session recovery call
  `daimon.core.turn.admission.reauthorize` before a session is found,
  reused, replaced or created. A rule or invoker change since admission
  refuses the turn there; a readers limit added since joins the turn's
  recorded ids, so the session is stamped with it and memory mounts
  read-only.
- Every channel send, card and direct message reads the policy when it is
  made, as do the routine fire, the handoff and the form submit checks.
- The MCP OAuth callback asks the agent rule's write check again after the
  code exchange and before the grant is written to the vault.
- A published report's reader variant carries its source agent's names
  (`daimon_reader_source`), so a rule on the source holds for the reader, and
  publishing the reader of an agent with a rule needs an admin or an
  `origin_context_id` from inside its channels. A channel's own agent's
  reader is never published (`require_reader_source_publishable`), admins
  included.
- An agent-scoped key is never exempt as an admin inside `authorize`,
  whoever minted it, and never holds its minter's channel admin grants
  (`build_subject`), so a channel-bound key reaches no channel but its own.

A demoted admin keeps their stored role until their next platform turn
refreshes it; that is accepted.

On every turn, wherever an agent with a rule runs (including an exempt admin
turn and a member's turn inside its channel), its sends (messages, replies,
threads and posts, files and cards on Discord, Slack and Teams) reach only:
its rule's channels and threads under them; the requester's own 1:1 DM with
Daimon (a Slack IM whose user is the requester, or a Teams personal chat the
requester is in); and direct messages to the requester. Its context never
lands in another channel or another person's DM. A channel's own agent is
stricter: it posts only into its channel and the threads under it, never the
requester's DM, and sends no direct messages.

`readers: own` constrains agents, not callers. Inside such a channel C only
C's own agents run, post and read; a caller with no executing agent, such as
an operator token or the CLI, may still post into C. That is input, not a
leak: the readers rule keeps C's messages readable only from inside it. A
session that ran in a DM (a Slack IM, a Teams personal chat, or a `/dm`
conversation) is private: admins never read it from the hub.

A channel admin has a server admin's rights limited to the channels they
administer, so only those rows' exemptions apply to them, and only there:
configuring agents with rules, coding-tool tokens, hub reads of their
channels' limited conversations, and `readers: own`, where a channel admin of
C is exempt in their own DM and hub for C's agents only. That grant is the
person's, never an agent key's. Other hub turns, DMs and forks treat them as
members.

Continuing a limited channel conversation from the hub stays refused for
admins because a follow-up would join the channel's own conversation, which
the channel goes on reusing. Branching a private copy for the admin is a
possible follow-up.

Who counts as an admin:

- In admission (DMs), the live role the adapter passes for this turn.
- For credential and configuration tools, a chat turn's credential, whose
  `is_admin` reads the account's stored role -- recorded from the platform by
  that turn's admission, and as current as the person's last platform turn
  when the same vault token is reused by their hub or routine sessions. The
  operator's own internal token is trusted too; an agent-scoped key never.
- In the hub, the account's stored role, and for a channel admin the stored
  grants matched against the role ids of their last turn. The hub has no
  live platform role, so a demotion or promotion takes effect on the
  person's next Discord, Slack or Teams turn in that workspace, which
  records the platform's current role.

An operator token (below) is an admin only while its account's stored role is,
checked on every request. Like the hub, it sees a demotion only once the
person's next platform turn records it, so revoke the token to stop it at once.
It always carries a platform user, so it is billed and rule-checked like that
admin's own chat turn, never trusted as the operator.

Agent-scoped keys, chat-turn credentials in the hub and tokens with no platform
user are never admins on these surfaces, whatever role their account holds or
who minted them. An agent rule binds a bearer with no platform user too: such
callers skip billing, not admission.

The agent rule decisions (turn admission, MCP and hub turns, routine save and
fire, handoff, configuration writes and form submits), sends and direct
messages, channel and session reads, fork, channel admins' configuration
rights and channel default binds are decided by `authorize`; each caller keeps
only its own I/O and refusal copy. The live writers and invoker checks in the
scheduler and routine delivery and the OAuth no-request rule still use the
same `permissions` predicates directly.

## Tenancy and isolation

One Discord guild, Slack workspace or Teams (Entra) organisation is one tenant. The tenant UUID is
derived, not allocated: `derive_tenant_uuid(platform, workspace_id)` in
`packages/core/daimon/core/ma_identity.py` is a UUID5 under a frozen
namespace, so the same workspace maps to the same tenant across database
resets and processes.

Isolation is enforced in two places at once.

**In Postgres.** Tenant-scoped tables carry `tenant_id` with a cascading FK to
`tenants.id`, and stores take `tenant_id` as a parameter rather than reading
it from anywhere ambient. Erasure is not left to cascades:
`packages/core/daimon/core/purge.py` deletes every row referencing a principal
in FK-safe order in one transaction, and
`packages/core/daimon/core/privacy.py` is its read-only mirror for the preview
panel. A schema-reflecting drift-guard test fails when a new person-scoped
table joins one path and not the other.

**In Managed Agents.** One deployment runs on one Anthropic key, so tenant
separation inside the MA workspace is carried by metadata stamps defined in
`packages/core/daimon/core/defaults/metadata.py` — `daimon_tenant`,
`daimon_account`, `daimon_name`, `daimon_managed`, `daimon_spec_hash`. The
resolver checks the `daimon_tenant` stamp on every cached-id retrieve, so a
stale id belonging to another tenant is rejected rather than used. Credentials
never enter a prompt: they are mounted into the session as an encrypted env
file (`packages/core/daimon/core/credential_env.py`) or brokered per call
(`packages/core/daimon/core/broker/`), and the MCP server the sandbox calls
back into authenticates a JWT whose `agent_id` claim is the derived agent
UUID from `packages/core/daimon/core/ma_identity.py`.

Because that env file is `source`d in the sandbox, `credential_env.py`
serializes every value so bash cannot expand or execute it, and
`packages/core/daimon/core/env_file.py` decides which key *names* may be
stored. Names that control an interpreter, archiver, loader, locale, package
manager, git, an HTTP client or a CA bundle, or that redirect an SDK's own
endpoint (`TAR_OPTIONS`, `BASH_ENV`, `LD_PRELOAD`, `GIT_SSH_COMMAND`,
`*_BASE_URL`, …) are hard-denied for everyone (the four git commit-identity
names `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME` and
`GIT_COMMITTER_EMAIL` excepted) and dropped from the mount even
if stored earlier; a non-admin member may additionally add only a secret
name (ending in `_KEY`, `_KEY_ID`, `_TOKEN`, `_SECRET`, `_PASSWORD`,
`_PASSPHRASE` or `_PAT` — never an identity, region or `*_URL`/`*_HOST`
name, which only an admin may add). This keeps one tenant member from handing another client's
agent code execution or a redirected connector through a key value or name.

## Sessions and Managed Agents

A turn does not create a session per message. `thread_sessions` maps
`(tenant, platform, thread)` to one MA session id, and its `status` column
carries the lifecycle: `live` is the caller's current session, `dead` is one MA
no longer has, `superseded` points at the successor that carries its work
through `replaced_by_id`, and `retired` is an explicit fresh start. That
lineage is why a thread can survive a session replacement with its context
intact.

What MA holds is the agent, the environment, the skills and the session
transcript. What Postgres holds is metadata: identity, the mapping above, the
config cascade, credentials and billing. An MA session freezes its agent spec
at creation time, so the agent read at admission is not necessarily what
executes — the mapping row stores a `SessionSnapshot` of the configuration the
session is actually running (`packages/core/daimon/core/session_snapshot.py`),
and that snapshot, not the current spec, is what later turns compare and bill
against.

### Same-thread handoff

A thread can move to another agent without a new thread. Two triggers:

- Ask the agent in the thread ("hand this to research-bot"). It calls
  `hand_off_task` with the destination's id. This works on Discord, Slack and
  Teams.
- When a channel's agent changes, a thread whose session belongs to the old
  agent can't run a turn, and its next mention gets a notice instead. On
  Discord, Slack and Teams the notice has a Switch to agent button that moves the
  thread to the channel's agent for whoever clicks it.

Asking the agent is the default because it is how every other thread change
works (keys, repo, fresh start). The button exists because no agent can answer
in that stuck thread, so there is nobody to ask.

Both go through `daimon.core.thread_handoff.hand_over_thread`, which decides
`authorize(HAND_OFF)` and writes the thread binding in one transaction. It
takes the tenant policy lock (`lock_access_policy`), then the requester's
account row FOR KEY SHARE, then the binding row FOR UPDATE, and reads the
policy after the first lock. A policy edit committed before the switch refuses
it; an edit that arrives later waits for the commit. The module docstring
gives the order against every other lock holder. A Hand over click by an admin,
or one `authorize` refuses, records a `panel:handoff` audit row, as
`hand_off_task`'s refusals are audited.

The switch writes only the binding. On the caller's next message the bind
replaces the old session with one for the new agent: the old session's
transcript and files are carried across by the workspace transfer, the
successor is stamped with the old session's recorded ids, memory is read-only
when the thread's readers are limited (or a DM with `dm_memory_read_only`), and
the old row is
marked `superseded`. The work is carried only when the new agent could read the
old session itself from this thread (`authorize(READ_SESSION)`); otherwise the
new session starts with nothing. A target its rules refuse here is refused by
admission before any of this runs.

## Entry points that are not a chat message

- **Agent setup picture controls** show the current agent picture in Details.
  Admins can open a Slack upload form or a Discord file modal from Change;
  Discord's attachment option remains a fallback. The upload path checks
  platform file URLs, size, and image content before replacing the public
  picture. Details keeps visibility and cache guidance off the main row.

- **`/here`** in Discord and Slack, and `here` in Teams (answered in the 1:1
  chat), reads routing and access policy, then shows a private card built from
  `daimon.core.here_card`: who answers here,
  reading scope, and whether publishing needs approval. Discord uses an embed;
  Slack uses Block Kit with a state colour and notes that threads can differ;
  Teams uses an Adaptive Card.
  The card omits credential names, rule provenance, memory and session details.
  `where_am_i` returns the same short text plus the complete structured facts.
  The model does not compose either summary. `/agent-setup` and
  `explain_agent_resolution` provide routing detail. The facts use
  `daimon.core.permissions` and `authorize`. The MCP channel-turn view uses
  member visibility even for an admin, because the model's answer may be
  posted to the channel. Slack slash commands have no thread identifier, so
  Slack `/here` reports channel routing only.
  Discord read and scoped search tools explain when the bot lacks View Channel
  or Read Message History permission, including on an otherwise empty read.

- **GitHub connection invitations** are issued with `daimon github connect-link`
  for a tenant admin, `/github connect` on Discord and Slack, or the
  `github_connect` MCP tool from a conversation. They can target one agent;
  self-serve links are refused for legacy-mode agents with a saved GitHub key,
  working or skill repo credential, or a channel pin. An operator-issued agent
  link may stage an update for an operator to finish with
  `daimon github finish-update`. When the separate `DAIMON_GITHUB_APP__*`
  credentials and
  encryption keys are configured, MCP serves `/oauth/github/connect/{token}`,
  `/oauth/github/callback`, `/oauth/github/setup` and GET/POST
  `/oauth/github/confirm`. The browser flow checks the confirming person's
  GitHub admin access before authorizing tenant repositories. These routes
  are absent when GitHub connection is unconfigured.
  Discord `/github home` and Slack `/github`, plus `/agent-setup` on both,
  expose connected repos, agent grants, personal links, waiting requests, and
  disconnects.
  The connection page offers a searchable repo picker and confirms each repo
  against the signed-in GitHub account. The same browser can safely repeat a
  successful submission; a signed invitation receipt also handles concurrent
  submissions. New installation repos queue private admin notices after the
  UTC day closes. Confirmed installation removal cancels affected requests and
  notifies known admins.

- **Scheduled routines** go through
  `packages/core/daimon/core/headless_runner.py`, which creates a session with
  the same `create_session` the chat path uses and delegates the drain to the
  same driver under the same ceiling — but it calls neither `admit()` nor
  `bind_session()`. The scheduler runs the balance, cap and channel budget
  gates itself before each fire. A routine with a destination is told where its result
  goes; if the agent does not post there, the row's outbox goes `pending` and
  the chat adapter for the tenant's platform posts the result tail through
  its delivery poller (`daimon.core.routine_delivery`), after the access
  policy's writers and invoker checks. See [routines.md](routines.md).
- **MCP agent-chat tools**, in
  `packages/adapters/mcp/daimon/adapters/mcp/tools/agent_chat.py`, let a caller
  drive a session directly. They do not use the chokepoint either; they re-run
  the same balance and cap gates through `_admit` in
  `packages/adapters/mcp/daimon/adapters/mcp/tools/_ctx.py` and create
  sessions via `daimon.core.sessions.create_session`. A run of a channel's
  own agent, a hub run by an admin or that channel's admin included, is
  gated by and charged to that channel's budget. The billed media tool
  runs the same gates, then the budget of the channel named by the calling
  turn's `origin_context_id`, and charges its spend to that channel.
- **`daimon run`**, in
  `packages/adapters/cli/daimon/adapters/cli/run/command.py`, is a single-turn
  subprocess entry point that calls `run_turn` directly with `BillingExempt`.
- **Wakes** run a turn in an existing thread later, with nobody mentioning the
  bot: a handoff's first turn for the new agent, work unblocked by a private
  form, a one-shot timer (`daimon.core.continuity.timers`, the `create_timer`
  tool), and anything else queued through `daimon.core.continuity.wakes`. A wake is
  a `task_continuations` row. The Discord, Slack and Teams adapters each run
  a wake poller (`run_wake_poller`) that opens threads with due rows and hands them
  to the adapter's continuation dispatch. That dispatch takes the thread's
  turn guard and goes through `admit()`, `bind_session()` and
  `run_prepared_turn()` like a mention, so the balance, cap and channel
  budget gates apply.
  A claim holds a lease, and `started_at` is committed just before the turn
  starts. If a claim's lease expires before `started_at` is set, the wake is
  retried. If it expires after, the wake is settled `interrupted` and never
  run again (`formal/continuation/WakeLease.tla`). Waiting on a busy thread
  refunds the claim, so it never counts against the crash budget
  (`WAKE_MAX_ATTEMPTS`). A thread the adapter cannot open (no token,
  archived workspace, platform error) is pushed back five minutes, so it
  cannot hold up other threads. An adapter that starts no poller leaves its
  wakes pending. The scheduler process has no platform client, so it never
  runs wakes.

  **Rollout order.** Migration `0028_feat003_wake_queue` first, then every
  Discord and Slack adapter process, and only then anything that enqueues
  wakes (timers). The pre-queue claim and list calls in the store skip rows
  whose `available_at` is in the future. But an adapter binary built before
  this change dispatches without leases or fences, and never polls, so its
  wakes would only run at a turn tail. Downgrading the migration settles
  every scheduled wake that has not run as `skipped/downgraded`, so none of
  them runs early.

  Timers add a reason (`timer`) that older code rejects when it reads a row.
  A FEAT-003-only adapter or MCP process fails to load a batch containing a
  timer row. So roll timers out in this order: migration `0029_feat084_timers`,
  then every Discord, Slack and MCP process on a timer-aware build. Only then
  may `create_timer` be called, and it is only exposed by that MCP build.
  Downgrading `0029_feat084_timers` deletes every timer row, fired or not.
  A wake (timer, handoff, applied private input) only runs as the agent it
  was queued for; applied private input may also resume the agent of the
  requester's live session in the thread. If the thread answers to
  another agent when the wake fires, the adapter refuses it after
  `admit()` and before anything is bound or billed. It settles the row
  `skipped/skip_target_changed` and posts a notice in the thread.

If you add another, reuse `admit()` rather than re-deriving the gate order.

## Standalone apps

`apps/notebook-host/` serves published marimo notebooks, spawning one
marimo subprocess per notebook behind a reverse proxy. Each subprocess has
its own access token, and scratch notebooks are read-only unless the
publisher asks for the editor and the operator allows it
(`notebook.allow_editable` on the bot and `allow_editable` on the host). With
`DAIMON_NOTEBOOK__ORIGIN_BASE` set, each notebook is served from its own origin
(`<label>.<origin_base>`); the proxy routes by Host and refuses cross-origin
requests and WebSockets. Without it, notebooks share one origin, so a public
host admits uploads only from the tenants the operator lists in
`DAIMON_NOTEBOOK__TENANTS`, matched against the tenant named in the bot's
signed upload token.
`apps/report-host/` serves one published PDF report with a chat sidebar.
Both are FastAPI processes that hold no Anthropic key and no database
credential; they reach Daimon over HTTP with capability tokens, and the
`must not import daimon` contracts keep it that way.

## Where to look next

- [routines.md](routines.md) — the scheduler and headless turns.
- [billing.md](billing.md) — metering, the gates, the ledger.
- [defaults.md](defaults.md) — what is seeded and how reconciliation works.
- [mcp-tools.md](mcp-tools.md) — every tool the agent can call.
- [configuration.md](configuration.md) — every setting.

The operator CLI also exposes `daimon tenants funding-mode PLATFORM EXTERNAL_ID
MODE`. This stores a per-tenant `prepaid` or `operator_funded` policy. Shared
balance admission emits a warning instead of a refusal for operator-funded
tenants; usage recording and configured caps continue through the same path.

`daimon promo create|list|revoke|redemptions` manages deployment-wide promo
codes. Admins redeem them from `/billing` on Discord and Slack (a Redeem code
button, shown only while a code is redeemable, and a modal), the Teams
`billing` card (a Redeem code action opening a code field, shown the same way) or with the MCP
tool `redeem_promo_code`; each surface calls
`daimon.core.promo_credit.redeem_promo_code`. Scheduler housekeeping settles
timed credit windows through `daimon.core.promo_settlement`. See
[billing.md](billing.md#promo-codes).

Each adapter remembers the names it is handed on the way in: the author of a
Discord mention and the user of any Discord interaction (`DaimonBot.on_interaction`),
the name a Slack event, slash command or click carries (right after the ack in
`SlackApp.on_request`), and the sender and channel of every Teams message and
card click. `daimon.core.platform_names` queues them in a bounded in-process
map, latest name per person or channel, and one background task per process
writes them to `platform_user_names` and `platform_channel_names` in batches of
50, one session each. A name this process already wrote is skipped, the
recording adds no wait to a turn, and a failed batch is only logged; a person's
name is only stored while they have a principal in that tenant.
The billing panel names people and Teams channels from them when the platform
does not answer live. See [billing.md](billing.md#what-you-can-see).

### Invocation context fragments

Core adds a `turn_context` block before the user message, chosen by the trusted
caller origin (`chat`, `routine`, `relay`, or `handoff`). Default chat adds nothing.
Scheduler runs use routine framing; Discord and Slack handoff continuations use
handoff framing. Callers drafting a relay pass `origin="relay"` to the core runner.
The block is included again on dead-session recovery.

Agent YAML accepts `context_fragments`, keyed by origin, with `text` and an optional
`mode` (`replace`, the default, or `extend`). For example:

```yaml
context_fragments:
  routine:
    mode: extend
    text: "Include the source timestamps in the result."
  relay:
    text: "Write a concise client-ready answer in Spanish."
```

The spec converter stores this configuration in the agent's system field so it
survives upload, forks and defaults fingerprints. Replacing that system field
without the configuration removes the overrides. An empty replacement disables a
fragment. These blocks affect prompting only and grant no extra permissions.

Operator recovery tools: `daimon backup platform-export` exports the dedicated
MA workspace through core; `scripts/backup/postgres.sh` backs up/restores Postgres.
See [self-hosting](self-hosting.md#backup-and-disaster-recovery) for the recovery
contract and limits.

### Google tokens in ordinary chat

Chat sessions attach a per-account, per-agent vault whose signed JWT carries
`chat_agent_id`, derived from the tenant and Managed Agent ID. MCP resolves this
as the executing agent identity for `get_cli_token(service="gcloud")`, while
preserving the ordinary chat tool surface and live account-role checks. The
separate `agent_id` claim still selects the restricted external agent-chat surface.
Neither claim is supplied through tool arguments. Chat identity is stored separately
from `AuthIdentity.agent_id` and is consumed only by the Google broker path. GitHub
chat calls still resolve the account principal-default PAT; other identity gates
and the two-tool search interface remain unchanged.

The operator must configure `credentials.google_sa_json`, authorize domain-wide
delegation, and bind the agent with `daimon agents bind-google <agent> <email>
--scopes <scope>...`. The broker impersonates only that agent's bound Workspace
user and scopes; an unbound agent receives a clear operator-binding error.
Core does not ship curated Workspace tools. Agents may use the token themselves
or a deployment-provided Google MCP server.

Existing static-bearer vault credentials are upgraded in place on the next
session creation. Credential metadata records the identity version so subsequent
creates leave the token stable; unrelated and OAuth credentials are preserved.

### Completion signals

The core driver calls an optional `on_acknowledgment` lifecycle hook with
`accepted` after the initial event send and `done` after successful answer
delivery. Missing hooks are no-ops; reaction failures are bounded and do not
fail the turn. Opted-in Discord and Slack tenants react with eyes, then a check
mark on success. Unprompted Discord turns stay silent; failures and cancellation
do not get a completion marker. Continuations without a trigger message skip
reactions.

A cancelled prompted Discord or Slack turn keeps any partial answer and appends
"Stopped. Send a message to start again."; a turn cancelled after only tool calls shows that notice in its
status card. A cancelled turn sends no completion ping and no feedback controls.
An unprompted Discord turn cancelled before it has an answer stays silent.

Set `DAIMON_COMPLETION_PINGS` to a JSON object keyed by tenant UUID, for example
`{"00000000-0000-0000-0000-000000000001": true}`, to deliver that tenant's final
answer as a fresh thread reply mentioning only the requester. Missing or false
entries keep the existing in-place answer and reactions (none on Discord; Slack keeps its admission eyes). Slack admission adds eyes once; the lifecycle only replaces it on opted-in completion. Recovery lifecycles retain this policy;
continuity notices and feedback target the new answer. Teams posts the answer fresh (with
an @mention in a channel), then sets its card to "Done."; bots cannot
react there.

### Routine dispatch

The scheduler owns a persistent, bounded routine dispatcher across ticks.
Routine turns run independently of the tick; the same routine cannot overlap
itself. Per-routine missed-run policy and the latest skipped range are exposed
by the routine MCP tools. See [routines.md](routines.md) for catch-up and shutdown.

### Agent-initiated direct messages

The shared channel tool `send_direct_message(recipient_id, content)` dispatches
to Discord, Slack or Teams under the authenticated tenant. Both sender and recipient
are checked for current platform membership before a DM is opened (on Teams,
a team Daimon is in that both belong to). Discord bot recipients, Slack
inactive, external, or bot users and Teams anonymous members are rejected. Other
platforms return unsupported. Channel tools continue to reject DM channel IDs.

Default recipient policy is tenant members. `DAIMON_DIRECT_MESSAGE_POLICIES` is
a JSON map keyed by tenant UUID, for example:

```json
{"00000000-0000-0000-0000-000000000001": {"mode": "allowlist", "recipient_ids": ["U123"]}}
```

Tenant UUID keys are normalized at settings load, including uppercase and
unhyphenated UUIDs. Invalid keys fail settings validation.

`mode` accepts `members`, `allowlist`, or `disabled`. All modes that allow sending
still require live tenant membership. The tool sends at most 19000 characters
as plain text in bounded chunks and returns every platform message ID. Partial
failures state the number already sent; callers should not retry the whole text
blindly. Attachments and cross-tenant delivery are outside this tool's scope.
### Memory write policy

Session memory mounts are read-only for channels with limited readers (including their threads),
for DMs when the tenant access policy sets `dm_memory_read_only`, and for routines.
Other chat turns retain writable memory. Admission carries the trusted decision;
the mount mode is recorded in the session snapshot and checked before reuse.
Tightening access replaces an idle writable session, including a legacy session
whose mount can no longer be inspected. An active session refuses the restricted
turn instead of deferring enforcement. Replacement
when tightening memory access skips the old session's checkpoint, since that would execute
with its previous permissions; platform history supplies the new turn's context.
Uncommitted workspace files are not transferred on this restricted replacement.

Memory content is managed directly by the MA memory store. This safeguard does not
add per-memory author/origin records or rollback tooling.
### Platform table rendering

Enable per tenant with `DAIMON_TABLE_RENDERING`, a JSON map of tenant UUIDs to
booleans. Teams renders Markdown tables natively, so the setting does nothing
there. Missing/false preserves current plain-text delivery. UUID keys are
validated at startup. Core `tables.render_tables` parses pipe-delimited Markdown tables outside fenced
code and accepts an optional async platform hook. Without a hook the input is
returned unchanged. Both adapter helpers also default to disabled. Hook failures
and oversized tables retain their raw text. Rejected Discord attachment edits
and Slack table blocks are logged and retried as the original Markdown; Slack
keeps already-delivered chunks in place and retries only the rejected table.
The shared bound is 20 columns, 100 rows including the header, and 10000 cell
characters; at most ten tables render per answer.

Discord renders final-answer tables off-thread as PNG attachments using bundled
Inter fonts (including wizard submissions and their recovery turns), navy headers, light alternating rows, and horizontal rules. Wide
cells wrap without truncation, all-numeric body columns align right even without
an explicit `---:` marker, and a pixel budget
prevents excessive allocations. Tables containing glyphs absent from the selected
font, including CJK text, remain unchanged Markdown so no values are lost. Table markers preserve their position in the
surrounding answer text. Slack final replies use native wrapped table blocks,
with each table in a separate message so table budgets stay bounded and prose
order is preserved. Feedback stays on the final delivery and continuity notices
can be inserted before a leading table. See the [Slack table block reference](https://docs.slack.dev/reference/block-kit/blocks/table-block/).

Streaming status previews and MCP `send_message` remain plain text. Tables inside
code fences remain literal examples. Other adapters need no renderer changes.


### Durable turn outcomes

`turn_outcomes` stores one content-free terminal record per logical turn. A UUID
follows admission, session binding and execution; a dead-session recovery remains
one turn. Discord, Slack, their continuation/wizard paths, headless routines and
CLI runs use the same recorder. Admission and concurrency refusals are recorded
even when no model runs. The row contains tenant/account and agent identifiers,
platform/channel/thread identifiers, the shared `TerminationReason`, UTC start/end
and monotonic duration, recovery status, exception class, package release and
observed usage-event keys. It contains no messages, prompts, answers, tool inputs,
rendered error strings or credentials. Missing attribution stays null (for example,
a CLI run against an existing MA session); no tenant is inferred from user text.

The terminal path schedules a bounded background write and never awaits database
I/O. Inserts are idempotent on the turn UUID. At most 256 writes are pending; each
has a one-second timeout and owns its database connection. Failures and queue
saturation log identifiers and exception class only. Runtime shutdown drains
pending writes before disposing its engine. These are best-effort diagnostics:
process crashes, queue saturation and database outages can lose an outcome. They
are not a transactional audit log, and never change admission, billing or replies.
Library-only headless calls without a session factory remain unrecorded; all
production headless entrypoints provide one. Usage references are the natural
`(managed_session_id, event_id)` keys, including calls observed during recovery;
billing-exempt calls may have no corresponding `usage_events` row.


MCP agent-chat and hub `ask` calls also record one outcome, attributed to tenant,
account, agent and session with platform `mcp` and origin `chat`. A returned idle
reply is `completed`, an observed terminated session is `session_terminated`, and
the bounded polling deadline is `ceiling`. Shared admission gates record balance,
cap and access-policy refusals. `start_turn` and `continue_turn` record the
accepted dispatch with reason `unknown`: they return before model execution ends,
so these records are dispatch observations, not claims of terminal completion.
No follow-up terminal update or model-span usage capture is implemented for these
SDK polling paths. Their usage fields remain null rather than implying zero
model calls or cost. Channel/thread identifiers are unavailable on these calls.
Identity resolution failures before tenant attribution and adapter readiness /
draining gates before the turn boundary are outside this coverage. Library-only
headless calls without a session factory remain unrecorded.

The terminal outcome row also carries optional per-turn usage measurements:
model-span token/cache totals, model IDs and estimated provider cost. The driver
observes billed and exempt spans without changing metering; natural
`(session_id, event_id)` keys deduplicate replay and retain recovery-attempt usage.
The operator command `daimon usage turns` queries tenant-scoped rows and channel /
origin summaries without upstream requests. See [billing](billing.md#per-turn-usage-telemetry)
for unknown-cost and historical-row semantics.

### Security audit trail

Authenticated requests through the main JWT MCP application (`tools/call` and
`tools/list`) produce one tenant-scoped
`security_audit_events` row. The identity middleware records the tenant, account,
platform user and agent identifiers, tool name, timestamp, outcome and a fixed
reason code. Agent identity includes the signed external `agent_id` or ordinary
chat `chat_agent_id` claim. A registered token adds its kind and jti, and a
scoped tool adds the operator scope it checked. The shared operation policy annotates that same request with the
operation name and its decision; a policy denial remains a denial even if a tool
catches it. A tool that refuses a call on an access decision (`authorize`: a channel
or agent rule, the invoker allowlist, a routine or default binding, an
environment pick) records the action as the operation and `authz:<reason>` as
the reason, so those refusals are denials rather than tool errors; a list that
filters on the same decisions records nothing. Arguments, messages, credentials, response bodies and exception text
are never copied into the row. Tool names outside the supported identifier syntax
are recorded as `<invalid>`.

The audit transaction is independent of the tool transaction. Request completion
queues an immutable metadata snapshot without awaiting the database. Timestamps
capture request completion, preserving chronology when inserts finish out of order.
The middleware
tracks at most 128 writes, with two database writes in flight and a two-second
bound including queue wait. Shutdown drains tracked writes. Writes are best effort:
timeouts, cancellation, overflow and database failures emit `security_audit.write_failed`
with an error type, never the error body. An unavailable audit store does not delay
the tool response or change authorization behavior. Tool failures use outcome
`error`; authorization and policy rejections use `denied`. When the verifier refuses a
registered token (revoked, expired, a demoted admin, a kind that does not match its
row), it writes a `denied` row under the token's own tenant with tool name
`auth/verify` and the refusal as the reason. Other calls rejected before the JWT
verifier establishes a tenant cannot be safely attributed and are outside this trail.
`daimon mcp mint-operator-token`, `revoke-token` and `set-token-scopes` each write a
row (tool name `cli/<command>`) in the same transaction as their change.
Admin-tier setup and billing panel writes on Discord, Slack and Teams (channel
rules, channel admins, a channel's environment and skills, coding-tool and operator token
mint and revoke, promo redeem) each write their own row through
`core/panel_audit.py`, allowed or refused, with tool name `panel:<op>`; the clicker
is named only when they have an account, so privacy erasure can reach the row.
The channel tidy tools (`edit_message`, `delete_message`, `archive_thread`,
`delete_thread`) also write their own row per message mutation, committed before the
platform call: `target_channel_id`, `target_message_id`, an HMAC-SHA256 of the
text replaced (`content_hmac`, keyed from the first `DAIMON_CRYPTO__KEYS` key,
else the MCP JWT secret, under its own label) and the turn (`turn_ref`), never
the text. Those `allowed` rows are what the tidy limits count; `denied` rows
count toward a separate hourly cap. MA identity and origin I/O finishes before
locking. Each write takes the tidy per-agent advisory lock, the tenant policy
lock (`FOR NO KEY UPDATE`), then the target post row (`FOR UPDATE`). The
separate audit transaction skips the advisory lock already held by its caller.
The policy is loaded and checked under the tenant lock, which stays
held through the platform call (15-second timeout) and ledger completion.
Policy writers, form consume, support (user lock first), handoff and preparation
never acquire the tidy advisory lock or a post row before the tenant lock, so
there is no reverse lock edge. Pool headroom is reserved before checkout.
A refusal adds a `denied` row; a failed write adds an `error` row. An uncertain
result retires the target and clears its hash, refusing retries; a definite
platform refusal leaves the ledger intact. Discord `delete_thread` deletes
only individually recorded own messages and keeps the thread, including human
replies arriving mid-call. Slack also checks, audits and counts each deletion,
and stops with partial progress on refusal. An agent may tidy only posts in
`agent_posted_messages` under its own agent id, which `send_message` and
`create_thread` write at send time (`source='tool'`). Discord and Slack also
record the status card, answer chunks and in-thread notices of mention and
continuation turns (`source='turn'`, with the turn's card intent and the
requesting user). Discord records the thread it opens from a mention
(`source='auto_thread'`, with the user who mentioned it). Turn error notices,
setup-wizard turns, Slack DM answers (which have no card intent), and session-output files are not recorded. Each post belongs
to the turn agent's derived id. A turn post may be tidied
only once its card intent is retired or marked unrecoverable, and only for the user who started that
turn, the user who opened the auto-thread it is in with the same agent, or a server
admin; a turn row must name its turn (a CHECK enforces it) and erasure clears
the requester id;
archiving or clearing an auto-opened thread takes its opener or a server
admin. `archive_thread` on the thread the caller's own turn runs in does not archive
it at once (Discord refuses edits in an archived thread): once every check,
including the locked policy re-check, has passed, it sets
`turn_origins.archive_requested_at` on that turn's origin. The Discord adapter
reads it when the run ends and archives the thread after the turn's last edit
and reaction, and after any session-output files are posted. If a newer turn
holds the thread by then (in flight, queued or a deferred continuation) the
thread stays open; otherwise the adapter holds the thread in `_processing` for
the archive call and hands any mention that queued meanwhile back to
`on_message`. A turn that raises leaves the thread open. `daimon audit prune` also removes those
records past the retention, and account erasure clears `content_hmac`.
The policy remains synchronous and does no I/O outside an MCP audit scope; the
separate hub OAuth applications (`/discord/mcp` and `/slack/mcp`, which use
`HubIdentityMiddleware`) are not audited in this version. This trail is not a complete record of hub tool activity.

Operators can read records using `daimon audit list TENANT_UUID --since
2026-09-28T00:00:00Z --json`. Use `--account ACCOUNT_UUID` to include that person's
audit metadata in an operator-assisted privacy export. Results are chronological;
`--limit` (1–1000) and `--offset` paginate retained records. Repeat per tenant for
a person active in multiple workspaces. This operator command uses local database
access; it is not an MCP tool and is not exposed to tenant users.

Retention defaults to 90 days (`security_audit_retention_days`). Operators must
schedule `daimon audit prune TENANT_UUID` for each tenant, for example daily. The
command removes only that tenant's events older than the configured age; `0`
explicitly opts into indefinite retention. Privacy erasure still applies. Include
the schedule and backup expiry in the deployment's published retention policy.

The shared account privacy purge clears account and platform-user identifiers in
all of that account's audit tenants, even when no principal remains. Tenant deletion
removes its audit rows in the same transaction. PostgreSQL AFTER DELETE triggers
on accounts and tenants enforce these same erasure rules even when an older
application binary issues the deletion during a rolling upgrade. The triggers
restore the previous transaction-local maintenance flag after scoped erasure;
rollback restores both data and flag on failure. Install the migration before
starting audit-producing services; privacy workers need no coordinated upgrade.
Queued writes lock live identity
rows: after account deletion they omit personal identifiers, and after tenant
deletion they are discarded, so delayed writes cannot restore erased data.

Ordinary UPDATE, DELETE and TRUNCATE remain blocked by a statement trigger.
Dedicated store maintenance functions enable a transaction-local GUC only within
a savepoint, perform scoped erasure or expiry, then reset the GUC. Rollback also
resets it; TRUNCATE is never allowed. This guards accidental application mutation,
not a malicious database administrator or SQL caller able to set arbitrary GUCs.
Existing privacy panels do not yet deliver a full audit export: operators include
the CLI JSON export in the requested bundle.

### Operator tokens

An operator token lets an external integration call a few MCP tools over
`/mcp` for one server admin. `daimon mcp mint-operator-token` mints one, with
one or more scopes, a TTL of 30 days by default and at most 90 and, with
`promo:create`, an optional `--max-issued-usd` ceiling in whole cents. Server
admins also mint, list and revoke them from Who answers where on the Discord,
Slack and Teams setup panels (`core/panel_operator_tokens.py`): tenant scopes
only, never `promo:create`, 30 days, shown once, and they list and revoke
only tokens within those scopes; the live admin check is
stored as the account's role, as a turn stores it, so the verifier admits it. `daimon mcp list-tokens` and
`revoke-token` manage every registered token, and `set-token-scopes --jti ...
--scope ...` narrows an operator token to the scopes given: it only removes
scopes, so adding one takes a new token. `mint-token` CLI tokens are
registered and expire too, while older jti-less ones keep working.

| Scope | Tools |
| --- | --- |
| `tenant:read` | `get_tenant_summary`, `list_channel_budgets`, `get_channel_budget`, `list_channel_admins`, `list_channel_skills`, `list_environments` |
| `channels:write` | `set_channel_budget`, `clear_channel_budget`, `set_agent_default` and `clear_agent_default` (channel defaults only), `set_channel_admins`, `clear_channel_admins`, `set_channel_rule`, `set_agent_rule`, `set_channel_environment` and `clear_channel_environment` (channels only), `add_channel_skill`, `remove_channel_skill` |
| `agents:archive` | `archive_channel_copy` |
| `promo:redeem` | `redeem_promo_code` |
| `promo:create` | `create_promo_code`, `list_promo_codes`, `revoke_promo_code` (deployment-wide) |

`get_tenant_summary` lists every channel with a default, a budget, admins or
a rule, and `channels[].own_agents_only` says whether its readers are `own`.
Its read is `daimon.core.tenant_summary`, which `daimon channels list
PLATFORM WORKSPACE_ID [--json]` prints too, with the same JSON plus each
channel's `readers` and `writers`. The MCP tool omits those two keys: no other
MCP or chat path shows every channel's rule. `timed_credit` lists the live
timed promo credit. `set_channel_rule` with an operator token acts as that
admin: the copy it may make is an admin's fork, and its rule writes are the
same as the panel's.

Each request, `DaimonJWTVerifier` reads the token's `mcp_tokens` row: it must
be unrevoked and unexpired, its account still a server admin by stored role
with a platform user, and its scopes non-empty. The stored role changes only
on the account's next platform turn, so a demoted admin's token keeps working
until then or until it expires; `revoke-token` is the immediate stop, taking
effect on the token's next request. Scopes come from the row, so narrowing
them applies to the next request. An operator token cannot open a billing
checkout (`/billing/checkout` answers 403). The identity middleware disables
every tool for the token, enables those tagged `scope:<name>` for its scopes,
skips the search collapse and limits it to `operator_calls_per_minute` calls.
Each tool re-checks its scope (`tools/_scopes.py`). `promo:create` tools carry
no `admin` tag and a server baseline hides them from everyone else. A later
`channels:write` tool joins by taking the tag and the `require_scope` call;
`_scopes.py` describes the steps.

### Opt-in DM conversations

DM conversations are disabled unless the tenant's separate `direct_message_policies`
row enables them. Admins use `/dm enable` or `/dm disable`; the invoker allowlist
still applies through `admit(is_dm=True)` on every move and every private turn.
Discord uses a live guild-member lookup and Slack checks the current workspace
membership and role. No stored admin role grants access.

`/dm` in a server/channel moves recent text context into a new private scope. This
selects the workspace explicitly; unselected DMs remain ignored. The scope uses a
normal thread-agent binding, config cascade and per-account session mapping. Each
new selection resets the scope, so a physical Discord DM shared across servers
never contributes the previous server's private history. The live membership check
is bound to that scope to prevent a concurrent selection from changing its authority.
The context is escaped with the existing handoff builder and includes a source link.
`/dm` refuses in a channel with limited readers or a Discord thread that has
or sits under one, before any history is read: the DM sits outside the rule
and can reach open channels. Admission reports this as `source_sealed`, and
`start_dm` refuses it too. Slack's slash command carries no thread, so a Slack
thread with its own rule (`channel:thread_ts`) is not refused; instead its root
and broadcast replies are dropped from the copied channel history, as the read
tools do. Discord system notices (thread created, pins) are never copied. The DM
row records its source channel and thread and, on Slack, the `channel:thread_ts`
of every copied message. Every private turn re-checks them against the current
rules. Once any of them has limited readers the DM is quarantined: the turn is refused,
the conversation row (copied context and private history) is deleted, and the
scope's provider sessions are retired and archived, so a fresh `/dm` is needed.
Rows from before provenance was recorded cannot prove their source open and
are quarantined as soon as the tenant limits any readers.

Turns use the shared billing/session/recovery pipeline. Row claims prevent overlapping
or duplicate DM deliveries. The route retains bounded source context and private
history; privacy preview and deletion include it. Memory restrictions inherited
from the source remain attached to its DM conversation, and current DM policy is
checked on each admission. Session preparation enforces the selected memory mount access.
Slack private turns register their physical DM destination under a random execution
ID carried in a signed JWT in an isolated MA vault. The read guard resolves only
that exact row for the authenticated tenant/account and removes it on completion
or cancellation. Concurrent routine, headless, channel, MCP agent-chat and hub
credentials have no execution grant and cannot inherit account activity.
Each Slack DM turn starts a fresh MA session and vault; bounded source/private
history preserves conversation context, but ephemeral workspace files and personal
OAuth credentials stored only in the shared vault are not transferred. Old execution
tokens cease granting DM reads when their row is removed; expired rows fail closed.
Both platforms stamp private MA sessions with `daimon_private_dm`. Generic session,
agent-chat and hub lists, transcript reads and mutation/cost tools default-deny
these sessions even to the same account or an admin. Only a verified credential
whose execution ID matches the stamp can access one. Discord retains session
reuse but carries no transcript-browsing grant, so its private sessions are hidden
from all MCP session tools. Ordinary unmarked sessions keep their ownership rules.
Discord session reuse and ordinary shared vaults are unchanged.
The provenance thread ID remains the private scope ID, matching session mapping.

The first version delivers text replies after completion. Attachments, streaming
cards, cancellation controls, and moving Slack thread replies are deferred; Slack's
slash command carries recent channel messages. Run `/dm` again to reset or select
another channel. Disabling DMs prevents new turns, without cancelling a running turn.
