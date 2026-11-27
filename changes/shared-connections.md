- **A server admin can share one MCP server with the whole workspace.** Add the
  server's URL and token under Integrations, Shared connections; admins can
  also manage it from chat, where the token goes through their private form,
  never the chat. The `shared_connections` feature is on by default. Daimon
  shows each tool's description and input schema, and a tool stays off until an
  admin turns it on; one that changes upstream is held until approved again.
  Tools appear as `<name>_<tool>` on Daimon's MCP server and the hub, so
  agents, agent keys and outside MCP clients use them without their own
  sign-in. A tool marked as changing data asks for approval on a card
  (`shared_write_approval` can skip it per workspace or agent); an outside
  client may call it and is told to ask its user first. Admins turn tools off
  for the workspace, or for one agent with its tool rules, and every call is
  audited ([Shared connections](docs/shared-connections.md#share-a-server-with-the-workspace)).
