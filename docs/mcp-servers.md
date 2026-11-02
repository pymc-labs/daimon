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

The token carries `iss`, `aud`, `iat`, `exp`, `agent`, `agent_name`, `ws`
(`<platform>:<workspace id>`) and, when a person asked, `sub`
(`<platform>:<user id>`), `channel` and `msg`. Routines, agent chat and senders
from another organisation name no person. Without `workspaces`, every
workspace's agents reach the server. Request bodies over 4 MiB are refused.
Only agents on Anthropic Managed Agents can use the relay. Self-hosted harness,
GPT and Gemini agents cannot.

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
