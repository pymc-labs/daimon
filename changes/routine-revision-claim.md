- **Signed servers can approve one routine at a time.** With `"routine_claims": true`,
  a routine's calls to a signed-identity MCP server also carry its name and `rev`, a
  fingerprint of its prompt, owner, agent name and destination, so the server can bind
  an admin's approval for that routine to it. A pause, a resume or a new schedule keeps
  it; a paused routine signs none
  ([MCP servers](docs/mcp-servers.md)).
