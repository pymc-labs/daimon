# MCP servers

Connect Notion, Linear and other MCP servers to an agent, and set up sign-in for
servers that need extra configuration. Read this if you administer agents or run
the deployment.

## Connect a server

Ask the agent to connect a server. It posts a card only you can open. Adding a
server to an agent that other people use needs a server admin, but anyone can
sign in to a server the agent already has. GitHub repos use
[GitHub](github.md), not a token.

- **Servers that take a bearer token (Linear).** You get a private token form.
  Daimon checks the token against the server before storing it, and everyone who
  talks to that agent shares it.
- **Servers that sign people in through a browser (Notion, Slack, Atlassian).**
  You get a sign-in link. Daimon registers itself as an OAuth client, or uses
  one you registered (see below). You approve in the browser, and the grant
  lands in your own vault. Each person connects their own account.

The sign-in routes are `{origin}/oauth/mcp/start` and
`{origin}/oauth/mcp/callback`. [`DAIMON_MCP__PUBLIC_URL`](configuration.md#daimon_mcp__public_url)
must be reachable from a browser, and
[`DAIMON_CRYPTO__KEYS`](configuration.md#daimon_crypto__keys) must be set. If a
connection fails, the agent still answers and names the server it could not use.
Ask it to disconnect the server or connect again.

## Register a client for servers without dynamic registration

Some authorization servers, including Microsoft Entra ID, do not let Daimon
register itself. The sign-in page then says the server does not offer sign-in
Daimon can drive. Register a client by hand and list it in
[`DAIMON_MCP_OAUTH__CLIENTS`](configuration.md#daimon_mcp_oauth__clients) with
the MCP servers it may serve. A sign-in uses the client only when the MCP server
URL is listed (compared exactly, ignoring host case, a default port and a
trailing slash) and the authorization server is the listed
`authorization_server_host`. Every other sign-in registers dynamically.

> **Warning:** List only MCP servers you trust. A listed server decides which
> scopes the sign-in asks for unless you set `scope`, and it receives the
> tokens. The list is what stops any other server from borrowing the client.

For Entra ID:

1. In Entra, add an app registration. Under **Authentication**, add a **Web**
   platform with the redirect URI `<root>/oauth/mcp/callback`, where `<root>` is
   `DAIMON_MCP__PUBLIC_URL` without the trailing `/mcp`.
2. Under **Certificates & secrets**, create a client secret and copy its value.
3. Under **API permissions**, add the delegated permissions the MCP server
   documents. Grant admin consent, or leave consent to each person if your
   tenant allows it.
4. Configure the client. Point both endpoints at your tenant, because a
   single-tenant app is refused at the `/organizations` endpoints (AADSTS50194).
   Turn `send_resource` off, because Entra rejects a `resource` parameter that
   does not match the scope's resource:

```bash
DAIMON_MCP_OAUTH__CLIENTS='[{"authorization_server_host": "login.microsoftonline.com",
  "mcp_servers": ["<mcp server url>"],
  "client_id": "<application id>", "client_secret": "<secret value>",
  "authorization_endpoint": "https://login.microsoftonline.com/<tenant id>/oauth2/v2.0/authorize",
  "token_endpoint": "https://login.microsoftonline.com/<tenant id>/oauth2/v2.0/token",
  "send_resource": false}]'
```

If the MCP server publishes no scopes, set `scope` to the scopes to request, for
Entra usually `<application id URI>/.default`. A configured client also asks for
`offline_access` when offered, so connections refresh on their own. The client
secret is copied into each stored connection, so after rotating it people
connect again.

A value that does not parse stops Daimon at startup and names the bad entry. If
the authorization server refuses the client, the sign-in page tells the person
to ask the operator. The log records `mcp_oauth.callback_failed` or
`mcp_oauth.authorization_declined`.

## Let a server verify who is calling

A remote MCP server can ask Daimon to sign each request with the calling agent,
the workspace and, for a person's turn, who asked and from which channel and
message. Daimon signs on its own side, so the key never reaches the agent's
sandbox.

1. Run `daimon mcp identity-keygen`. Store the printed
   `DAIMON_MCP_IDENTITY__SIGNING_KEY` line as a secret on the MCP service, and
   give the server the public key (JWK or PEM). `daimon mcp identity-public-key`
   prints it again.
2. List the server in
   [`DAIMON_MCP_IDENTITY__SERVERS`](configuration.md#daimon_mcp_identity__servers)
   with the issuer and audience it gave you:

   ```bash
   DAIMON_MCP_IDENTITY__SERVERS='[{"name": "tools", "url": "https://tools.example.com/mcp",
     "issuer": "<issuer id>", "audience": "https://tools.example.com",
     "workspaces": ["discord:<guild id>"]}]'
   ```

3. Add `<root>/mcp-identity/tools` to the agent's MCP servers in place of the
   server's own URL, with no token. Daimon forwards each request with an
   `Authorization: Bearer <EdDSA JWT>` valid for two minutes.

The token carries these claims:

| Claim | Value |
| --- | --- |
| `iss`, `aud`, `iat`, `exp` | the issuer and audience it gave you; two minutes apart |
| `jti` | a new id on every request, so a server may accept each token once |
| `agent`, `agent_name` | the calling agent's id and name |
| `ws` | `<platform>:<workspace id>` |
| `sub` | `<platform>:<user id>` of the person who asked |
| `channel`, `msg` | the channel, thread or DM the asking message is in, and that message |
| `room` | on Discord, with `channel`: the kind of place the answer goes to |
| `routine` | with `"routine_claims": true`: the routine whose run posts to `channel` |
| `wake` | with `"wake_claims": true`: why a call with no new message runs |
| `rev`, `routine_name` | with `routine`: a hash of its prompt, owner, agent name and destination (none while paused), and its name |
| `owner` | with a channel or thread `routine`: its owner, not an asker |

`room` is `dm` for a direct message. In a room it is `contained` or `narrow` as
[Rooms with outside people](outside-people.md#contained-and-narrow-rooms) defines them,
and `team` for any other room. Contained wins over narrow, and a room Daimon cannot judge
is `contained`. Daimon judges the room again on every request, whatever that feature is
set to.

When Daimon opens a thread for its answer, `room` judges that thread while `channel` stays
the message's channel; a new public thread reads as its channel. A routine that posts to a
thread Daimon has not sorted yet, such as an archived one, signs `narrow`.

On a server with `"routine_claims": true`, a routine run that posts
to a Discord room carries `routine`, that room as `channel`, its `room` and its `owner`
while that run is live. A DM run needs
[`dm_routine_claims`](unattended.md#a-routine-in-its-owners-dm) too. Update all Daimon
processes together. Agent chat and outsiders name no person.

<!-- knowledge: web -->
The web app's tool list names no person either.
<!-- /knowledge -->

On a server with `"wake_claims": true`, Daimon signs `wake` only for these turns:

| Turn | `wake` | Signed with it |
| --- | --- | --- |
| A person's turn that is still running | `turn` | `sub`, `channel`, `msg` and `room`, as without the setting |
| A queued turn that runs in its own thread | its reason: `timer`, `watch_fired`, `watch_report`, `job_finished`, `task_handoff`, `private_input_applied`, `github_access_ready` or `confirmed_call` | that thread as `channel`, its `room` judged as for a routine, no `sub` or `msg` |
| A helper, a watch report into a new thread, a timer in a DM or channel, a sender from another organisation | none | what it signs without the setting |

The settings are independent: `wake_claims` allows calls with no new message even with
`routine_claims` off:

| `routine_claims` | `wake_claims` | Calls with no new message the server can tell apart |
| --- | --- | --- |
| off | off | none |
| on | off | routine runs |
| off | on | running turns and queued turns in their thread |
| on | on | all three |

Update the server first, then all Daimon processes together, then turn `wake_claims` on;
turn it off before rolling the server back. A call with `wake` never writes.

Without `workspaces`, every workspace's agents reach the server. Request bodies over 4 MiB
are refused.

A channel turn of an agent with a relay server runs held: it posts only in its
channel and its threads, sends no DMs and has no admin tools. The agent keeps no
long-term memory, and its sessions are sealed to the channel.

Set `"channel_scoped": false` on a server that checks each call's asker and
place itself. An agent whose relay servers all set it gets its normal memory and
sessions, so what it learns can surface in its other channels. One server left
at the default `true` still limits the agent. Existing conversations keep their
limits until someone starts a new one with `/new`. Remove the key before rolling
back to a release without it, which refuses to start on the unknown key.

Only agents on Anthropic Managed Agents can use the relay. Self-hosted harness,
GPT and Gemini agents cannot.

### Servers that attribute writes to the requester

A server that trusts one key rather than a signature, and attributes each write
to the person who asked, is listed with `"auth": "bearer"` and
`"requester_meta": true`. Its key goes in
[`DAIMON_MCP_IDENTITY__BEARERS`](configuration.md#daimon_mcp_identity__bearers),
a JSON object by server name, kept apart from the server list so it can be a
secret. Because the key is shared, a bearer server must also list the
`workspaces` and the `agents` (by name) allowed to use it; any other agent
gets a 403. No signing key is needed when every server is bearer.

```bash
DAIMON_MCP_IDENTITY__SERVERS='[{"name": "tools", "url": "https://tools.example.com/mcp",
  "auth": "bearer", "requester_meta": true, "workspaces": ["discord:<guild id>"],
  "agents": ["<agent name>"]}]'
DAIMON_MCP_IDENTITY__BEARERS='{"tools": "<key>"}'
```

Daimon sends the key in place of the caller's own credential, which never
reaches the server. On every `tools/call` it first removes any
`params._meta.requester_platform_user_id`, `requester_conversation` and
`requester_kind` the caller supplied, then sets them from the live run. Those
keys are removed under `_meta` of every method, not only tool calls.

- `requester_platform_user_id`: the asker's user id (Discord only). For an
  unattended run, the person it runs for: a routine's owner, or whoever set up
  the watch, timer or wake.
- `requester_conversation`: `{"place_type": "channel" | "thread" | "dm",
  "channel_id": ..., "parent_channel_id": ...}`. `parent_channel_id` is a
  thread's channel and null otherwise. For a routine, its delivery place. A routine
  with no place (no destination, or a thread with no parent) is stamped with the
  asker and kind but no place, so a server that requires a place refuses every read
  from it (fails closed); writes are unaffected.
- `requester_kind`: `"person"` only while a person's own message or click is
  being answered. `"routine"` means any unattended run: a routine, watch,
  timer, continuation wake or DM routine. `"agent_chat"` is a turn a person
  started from an MCP client signed in to the hub.

`"agent_chat"` carries no `requester_conversation` or wake link. Daimon sends it
only to a server with `"agent_chat_requester": true`, off by default. Agent,
coding and CLI keys name nobody. So does the turn after a send that failed with
a server error, and an older conversation's first turn (logged as
`agent_chat.requester_withheld`). Turn the option on only once every process
runs this release: older ones send without clearing the record.

A DM that is not open yet is `{"place_type": "dm", "channel_id": null,
"parent_channel_id": null}`; a routine can only DM its owner.

A routine run is stamped only while its routine still exists, belongs to the
same organisation and agent, and still has the owner signed into its
credential when it started. A changed owner or agent means no stamp.
`requester_conversation` is left out for a legacy thread with no recorded
channel, or nowhere. A sender from another organisation, a replaced session and
two turns at once carry none of the three fields, so a server must not treat a
call with no `requester_kind` as a routine.

### Wake the conversation when a server's job ends

A `requester_meta` server that starts long work can list the tools that start it
in `"wake_tools"`. A call to one in a person's turn also carries
`params._meta.wake_url`, a private [callback watch](watches.md#callback-watches)
link; one the caller supplied is removed. Each `POST` of the outcome wakes the
conversation once, in a turn that reads and reports. Keep the link out of logs.

Each call gets its own link to one watch per conversation and server. The watch
follows the conversation to its newest session and lasts a day after the latest call.
It needs the `watches` feature and `DAIMON_WATCHES__CALLBACK_BASE_URL`. In a Discord
DM each post runs as a fresh private turn in that DM. A Slack DM, a helper or an owner
at the watch limit gets none.

A request body that is not JSON, repeats a key, cannot be re-encoded, or holds
a tool call without object `params` is refused with a 400. GET and DELETE
requests are forwarded without a body. Rotating
the key means changing the secret and restarting the MCP service.

## Add a skill a server serves

A server that implements the MCP Skills extension (`io.modelcontextprotocol/skills`,
protocol revision 2026-07-28) can serve skills. Ask the agent to add one by
server and skill name, or by its `skill://` URI. It goes through the same
preview and approval as any added skill, and becomes the agent's own.

Daimon checks every file against the size and SHA-256 the server lists, and
refuses a skill that fails or whose files are built per request. Adding it
again publishes a new version only when its content changed.

A server with a shared token is asked with it. A relay server is asked with a
signed token naming the person asking and the channel. A server that signs
each person in separately cannot serve skills to Daimon.

<!-- knowledge: web -->
### Follow a server's skills in the web app

The agent editor's **Add skill** list offers the skills of the web app's
[data server](web.md#connect-agents-to-a-data-server). An added skill joins the
library read-only and follows the server.

Daimon checks the server every 15 minutes, or on **Check now** in the library.
A new version moves every agent that follows the skill. An agent that pins it
shows **Update available** instead; following again moves it at once. A skill
the server stops offering keeps its last version. **Fork** makes an editable
copy, under a name you choose, that does not follow. Agents fixed by the
server's settings show **Managed by settings** and never move.

An update that adds or changes a script, or changes `allowed-tools`, waits
until an admin approves it. Other changes, such as `.md` and `.txt` files,
apply without approval.

An agent switched to OpenAI or Gemini follows updates too. OpenAI uploads each
version of a custom or channel skill, and a skill over its size limit is off
there. Gemini carries the default skills only.
Self-hosted channels use their deployment's own skills.
<!-- /knowledge -->

## Give an agent Google Workspace access

An agent can act as one Workspace user, with scopes you choose. Set
[`DAIMON_CREDENTIALS__GOOGLE_SA_JSON`](configuration.md#daimon_credentials__google_sa_json)
to a service-account key, and grant that account domain-wide delegation in
Google Workspace. Then bind the agent:

```bash
daimon agents bind-google <agent> <email> --scopes <scope>
```

Repeat `--scopes` for more scopes. The agent's `get_cli_token(service="gcloud")`
call then returns a token that impersonates only that user and those scopes.
An unbound agent gets an error naming the missing binding.
