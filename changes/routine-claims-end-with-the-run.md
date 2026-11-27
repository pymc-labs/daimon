- **A routine's signed calls end with its run.** With `"routine_claims": true`, a channel or
  thread routine's calls to a signed-identity MCP server carry its routine claims only while
  the scheduled run that made them is still running, as a DM routine's already do. They also
  name the routine's owner as `owner`, apart from `sub`, so a server can share what the run
  makes with that owner. Update all Daimon processes together
  ([MCP servers](docs/mcp-servers.md)).
