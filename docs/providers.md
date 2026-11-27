# Model providers

Choose which model backend answers in your agents and threads: the default
Anthropic backend, OpenAI, Gemini or a self-hosted harness. Read this if you
self-host and do not want every turn on Anthropic, or want to try another model
in one conversation.

## Compare backends

| | Anthropic (default) | OpenAI | Gemini | Self-hosted harness |
| --- | --- | --- | --- | --- |
| Needs | Anthropic API key | OpenAI key and project | Gemini key | Your own harness service and Azure OpenAI |
| Models | The agent's own model | `gpt-6-luna`, `gpt-6-astra`, `gpt-6.2-astra`, `gpt-6.2-sol` | `gemini-3.8-flash`, `gemini-4-argon`, `gemini-4-carbon`, `gemini-4-hydrogen`, `gemini-4-neon` | The deployment you name (default `gpt-6.1-sol`) |
| Set as an agent's model or a thread's | Yes | Yes | Yes | An agent's, once the operator grants it; not a thread's |
| Shared threads | Yes | Yes, without Daimon's tools unless the agent itself picks OpenAI | Yes, without Daimon's tools | Yes |
| Tools that need a confirmation | Yes | Daimon's Approve card | Daimon's Approve card | Daimon's Approve card |
| The agent's own connectors | Yes | Through Daimon | Through Daimon | Through Daimon |
| Sealed channels and Slack DM context | Yes | Yes | Yes | Yes |
| Multi-agent delegation | Yes | Copies of itself | Copies of itself | Copies of itself |
| Memory across replies | Yes | Through Daimon, needs an Anthropic key | Through Daimon, needs an Anthropic key | Not supported |
| Web fetch | Yes | Through Daimon | Through Daimon | Through Daimon |
| Web search | Yes | Yes | Yes, through Google Search | Not supported |
| Output files | Yes | Yes | Yes | Yes, from harness 0.2.0 |
| Reasoning effort | Yes | Low, medium, high | None | Not supported |
| Spend limit for one reply | Yes | Daimon stops the reply | Daimon stops the reply | Not supported |
| Routines | Yes | With sandbox tools turned on | Yes | Not supported |
| Workspace export and import | Yes | Not supported | Not supported | Not supported |
| Skills | Full | Custom and agent skills, up to 28 MiB each | Custom and channel skills, up to 28 MiB each; small text skills share 2 MiB | Seeded defaults only |

A provider that cannot meet a channel's requirements refuses the reply.

Anthropic's built-in Word, PowerPoint, Excel and PDF skills run only on Anthropic.
On OpenAI and Gemini an agent that has them gets short open equivalents
(`defaults/skills/open-*`, built on python-docx, python-pptx, openpyxl and
pypdf) instead.

### What is not supported yet

- **Self-hosted:** memory, routines, effort and a
  spend limit, workspace export and import. An agent can pick it only with
  its own runtime file row (`daimon selfhosted init --agent`).
- **OpenAI and Gemini:** watch reports. OpenAI's hosted shell cannot be left
  out, so its routines need
  [sandbox tools](unattended.md#opt-a-routine-into-sandbox-tools) and its timers
  cannot run. A channel's own deployment where a channel or thread turns a
  built-in off. Workspace export and import.
  Switching off one of bash, read, edit, write, grep or glob removes all six,
  since the provider has one execution tool. A connector that
  OpenAI's size limit cannot hold, or whose tool names clash with Gemini's own,
  is off there, as is a web search that must ask first. Repository
  checkouts stay on Claude.
- **Without an Anthropic key:** memory.

## Route channels to a backend

With nothing configured, every channel runs on Anthropic using the agent's own
model. Selecting another backend uses its default model unless you pass
`--model`. Route in one of two ways:

```bash
docker compose run --rm init "daimon agents backend set <agent-name-or-id> openai"
```

This sends every channel that uses the agent to OpenAI. To route single
channels instead, use `daimon channels rule set` and scope the runtime file row
with `--platform`, `--channel-id` and `--account-id` on `daimon openai init`.
A self-hosted model is a channel pin, or an agent's model where the operator granted it.

<!-- knowledge: web -->
An admin can also pin a channel from its page in the web app.
<!-- /knowledge -->

A channel's own pin outranks the agent's provider there. An admin can clear it
from chat by asking Daimon to clear the channel's pin and confirming the card.
Existing threads keep their model.

A refused reply names the cause in chat: the channel's pin, a tenant allowlist
that does not enable the provider ([enable it](#enable-providers-for-a-tenant)), or an agent whose provider cannot serve the
channel yet. Backend selection is on by default
([`DAIMON_TURN__CHANNEL_BACKENDS`](configuration.md#daimon_turn__channel_backends)).
Setting it to `false` sends every reply back to Anthropic, as an emergency
stop.

## Switch with a model pick

Picking a model on another provider is the switch. `/model` on Discord, the
Model shortcut on Slack, the `model` command on Teams and the agent panels all list
every model your install can run with its price. A pick applies at once, like any
model change. What the new
provider can't carry is off there and named after the pick, until you switch
back. Earlier messages carry over, and so do saved files where the new provider
keeps a workspace. A refusal names its cause and changes nothing.

<!-- knowledge: web -->
The web app's model editor and chat header list the same models.
<!-- /knowledge -->

Asking an agent in chat to move itself or the conversation to another provider
is different: `request_agent_backend_switch` and `set_thread_model` preview first,
then post a card in the thread that names the provider. It runs only when the
person approves.

Picking a model for one conversation, in chat or with the MCP tool
`set_thread_model`, leaves the agent on its own model everywhere else. Anyone
who may change a conversation's model can pick another provider's, as the
`thread_providers` feature is on by default. Turn it off for an agent and only
its owner or an admin may. The pick stays when an admin edits the agent. A
conversation on OpenAI can also take its own effort, low, medium or high.

An admin can **Switch back to Claude** from a refused reply or a panel, when
the install has an Anthropic key. MCP tools and the CLI take
`--model provider:id`, for example `openai:gpt-6-luna`, with `--yes`.

### Convert an agent in either direction

```bash
daimon agents convert <name> [--to openai|gemini|anthropic] [--yes]
```

An admin runs it. Without `--to` it picks the first provider that has a key.
Moving to OpenAI or Gemini changes the agent's model and keeps the same agent:
its name, owner, channels and agent rules. Skills that exist only on Claude are
left off, and the command names each one. `--to anthropic` makes it a Claude
agent again with the same owner, threads, routines, memory, settings and grants.
Both are safe to re-run after a failure. They are refused while the agent has
queued wakes, pending approvals or running jobs.

## Run without an Anthropic key

Set only an OpenAI or Gemini key. `init` seeds the default agents on the first
keyed provider at its default model, and agents you create later live there
too. Each is pinned and replies with no Anthropic request. Thread titles and
participation checks use a small model from that provider. An agent still on Anthropic gets a card offering a copy on
another provider, or asking whoever runs Daimon to add the Anthropic key.
Memory settings show as needing setup.

## What Daimon adds on OpenAI and Gemini

- **Memory.** Daimon's own tools read and write the agent's shared, personal
  and channel memory, under the same rules as on Claude. The stores live on
  Anthropic.
- **Web fetch.** Daimon opens public https pages for the agent, checks every
  redirect and cuts long text. The place's Open web pages setting decides. Self-hosted
  agents get it too.
- **Connectors.** The agent's own connectors run through Daimon with a
  credential for that reply only, and the provider never sees a sign-in. Each tool keeps the
  agent's permission. Sign-ins and pasted tokens keep working after a switch,
  except one made before this release for an agent on Claude, which needs one
  last Reconnect. On OpenAI and Gemini, `github` reaches the agent's repositories
  with App tokens for that reply. Self-hosted agents get the others, and their
  harness asks before an `ask` tool.
  The provider receives what the connectors return; turn the
  `mcp_connector_relay` feature off to keep them out.
- **Keys.** The agent's saved keys go with it as a `.env` file at
  `/workspace/.daimon/.env` in the provider's sandbox, so the provider receives
  them. Copies of itself get none. Turn it off with the `provider_keys_file` feature.
- **Confirmations.** A tool that needs one posts an Approve and Deny card on
  Discord, Slack and Teams. Only the asker answers, and an approved
  call runs once. An unattended reply runs only what its rules allow. An agent's
  `ask` and channel tool rules use the same card, and a built-in tool a channel
  or thread switches off leaves the reply. Turn the card off with the
  `host_call_gate` feature; an `ask` rule then refuses the call.
- **Copies of itself.** `ask_helper` with `self` starts a fresh copy per ask.
  All copies of one message stop at $10 of spend together, and share no files.
- **Effort.** OpenAI takes low, medium or high, kept with the model or set for one
  conversation; Gemini takes none. Fast stays on Claude.
- **Spend limit.** The host prices a reply's usage while it runs, as the runtime
  file section below describes.

<!-- knowledge: web -->
On the web app, the Approve and Deny card appears in the chat.
<!-- /knowledge -->

## Run on OpenAI

Two limits apply beyond the table: no mid-session resource changes (initial
file mounts work), and fixed container defaults.

1. Create an OpenAI project and an API key scoped to it. Add the key to
   `.env` under a name outside the reserved `OPENAI_*`, `ANTHROPIC_*`,
   `GOOGLE_*` and `GEMINI_*` prefixes:

   ```bash
   DAIMON_CHANNEL_OPENAI_KEY=sk-...
   ```

   The runtime file records only this name, never the key.

2. Point the runtime at the file in `.env`:

   ```bash
   DAIMON_TURN__PROVIDER_RUNTIME_FILE=/app/runtime/openai.json
   ```

   The services already share `./runtime` as `/app/runtime`. `daimon openai
   init` reads the same variable, or takes `--manifest`.

3. Invite the Discord bot first: Daimon creates your server's tenant when
   the bot joins. Its UUID is the `id` field in the output of
   `docker compose run --rm init "daimon tenants list --json"`. Provision the
   agent, environment and default skills:

   ```bash
   docker compose run --rm init "daimon openai init --tenant-id <tenant-uuid> --project <openai-project-id> --model <openai-model-id>"
   ```

   It creates one OpenAI agent and one hosted environment, uploads every skill
   under `defaults/skills/` and writes a runtime file row. Repeating it prints
   the existing IDs. Pass `--skip-default-skills` for a bare agent and
   `--api-key-env` for a different variable name. Then run
   `docker compose up -d` so the services read the new values.

4. Route channels as shown above, then verify:

   ```bash
   docker compose run --rm init "daimon openai smoke --project <openai-project-id> --model <openai-model-id>"
   ```

   The smoke test provisions its own agent, runs one billed turn that must call
   one tool, and exits non-zero unless it completes.

## Enable providers for a tenant

Every key you set turns its provider on: a tenant whose admin never chose may
use Anthropic and every provider whose deployment key is set, such as
`DAIMON_CHANNEL_OPENAI_KEY` or `DAIMON_CHANNEL_GEMINI_KEY`, with or without an
Anthropic key. A key under another name through `--api-key-env` does not count.
Self-hosted backends are never on by default. An admin's own list always wins,
so an admin can narrow it to Anthropic or add what is missing.

An admin changes the list in chat by asking Daimon and confirming the card.
On Discord, an operator can do the same from the CLI. Each call checks that
the named user is a current server admin, and other platforms refuse it.

```bash
daimon tenants backend-policy snapshot discord <guild-id> --actor-user <admin-user-id> --output before.json
daimon tenants backend-policy apply discord <guild-id> --actor-user <admin-user-id> --snapshot before.json --backends anthropic,openai,gemini --yes
```

`--dry-run` previews a change, and `--backends default` returns to following
the keyed providers. To undo, run `revert` with `--snapshot` and
`--expected-revision <applied-revision>`.

## Edit the runtime file

The runtime file is a JSON list that you own. Each row authorizes provisioned
resources for one tenant, and holds variable names, never credentials. A row
for OpenAI:

```json
[
  {
    "profile": "openai.persistent_workspace",
    "tenant_id": "<tenant-uuid>",
    "platform": "*",
    "channel_id": "*",
    "account_id": "*",
    "project": "<openai-project-id>",
    "agent_id": "<openai-agent-id>",
    "environment_id": "<openai-environment-id>",
    "api_key_env": "DAIMON_CHANNEL_OPENAI_KEY",
    "skill_ids": []
  }
]
```

`platform`, `channel_id` and `account_id` accept `"*"`. A row with `"*"` on all
three covers every channel and account in its tenant, which is what
`daimon openai init` writes. `tenant_id` and `profile` never accept `"*"`, so a
row can never serve another tenant. A narrower row wins over a wildcard row.
Two equally specific rows, or rows naming different fields, are refused as
ambiguous. Unknown fields, a stale digest or a missing resource are refused too.

An optional `spend_limit_usd_cents` caps each reply, not the whole session. OpenAI
refuses a native session spend limit, so the host enforces it: it prices the reply's
usage while it runs (container time not counted), stops it once the limit is passed
and tells the agent next time. A reply sums its requests, so it counts at the base
rates past 272k input tokens; only one request known to pass that takes the model's
long-context rates. The host refuses `OPENAI_CUSTOM_HEADERS` for configured OpenAI
runtimes so ambient headers cannot replace the channel's key or project.

## Run on Gemini

Gemini has no provisioning command. The published image includes the
`daimon-mux[gemini]` extra.
With `DAIMON_CHANNEL_GEMINI_KEY` set, Gemini runs with no runtime file row.
Add a row to the runtime file, which can hold both providers, only to override
project, spend holds or storage:

```json
{
  "profile": "gemini.inline_reuse",
  "tenant_id": "<tenant-uuid>",
  "platform": "slack",
  "channel_id": "<channel-id>",
  "account_id": "<account-uuid>",
  "config_digest": "<admitted-config-digest>",
  "project": "<gemini-project>",
  "api_key_env": "DAIMON_CHANNEL_GEMINI_KEY",
  "agent_name": "<agent-name>",
  "system": "<system instruction>",
  "tools": [],
  "environment": {"name": "inline-workspace"},
  "records_path": "/var/lib/daimon/gemini-records.sqlite",
  "holds_path": "/var/lib/daimon/gemini-holds.sqlite",
  "hold_usd": "0.05",
  "cap_usd": "0.25"
}
```

- **Tools.** `tools` is required. `[]` sends an explicitly empty list, and a
  missing value is refused. Search, URL context, code execution and custom
  tools run only when listed.
- **Storage.** The two paths must be absolute, distinct and on persistent
  volumes, shared by every worker. For several hosts, set
  `"storage_backend": "postgres"` and omit both paths. This uses the
  application database and migration `0106_gemini_runtime_state`. Cloud Run
  refuses SQLite.
- **Spend.** Each attempt places a hold, an estimate against `cap_usd`.
  Holds release on a refusal, settle when usage is final, and otherwise expire
  after 24 hours. An optional `spend_limit_usd_cents` caps each reply as for
  OpenAI: the host stops it once its priced usage passes the limit, for a
  model with a known price. Rows built in code set 50.
- **Growth.** Records and holds have no size bound or compaction, so monitor
  database storage before raising load.

In a row, name the profile `gemini.inline_reuse` and the model
`gemini-3.8-flash`.

## Run a self-hosted harness

The `selfhosted.loop` backend sends turns to a `daimon-harness/0` service that
you operate. It is only ever a channel pin, never an agent's model. Setup, the
admission settings and the harness container are in
[self-hosted harness](self-hosted-harness.md).
