# Shared connections

## Share a server with the workspace

An admin can share an MCP server so agents and outside clients use it without
their own sign-in, from chat or in the web app.
[`shared_connections`](features.md), on by default, turns them all off.

<!-- knowledge: web -->
In the web app, open Integrations, Shared connections, and add the server's
name, URL and token (stored encrypted). The agent page lists what its viewer holds.
<!-- /knowledge -->

Daimon lists each tool's description and schema; turn on the ones you want. A
tool whose text changes upstream stays off until approved again; one named like a
Daimon tool is left out. Each tool appears as
`<name>_<tool>` on Daimon's MCP server, for members, not web guests. A scoped
connection or tool is served only where data reads may answer and everyone the
answer reaches holds its scope.

A tool that changes data is marked by its name; you can change the mark. In
chat it waits for the asker's card unless [`shared_write_approval`](features.md)
is Skip. An outside MCP client, agent key or coding key may call it, told to ask
its user first; a chat call with no conversation is refused. Turn a tool off here
for the workspace, or with tool rules for one agent (agent keys follow; Ask
refuses them). Every call is audited.

Admins add a connection from chat too; each change waits for their card, naming the
tool, its server, parameters and the words callers get. A token goes through
their private form, never chat. In a room, addresses and errors go to them by
direct message, through `send_private_details`.

## Declare connections in a file

A `connections` entry (`id`, `url`, `token: "env:NAME"`) in the
[declared data config](data-sources.md#declared-config) declares a connection;
admins approve its tools.
