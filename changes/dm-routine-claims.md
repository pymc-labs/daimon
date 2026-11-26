- **Signed servers can read for a routine that posts to its owner's DM.** With
  `"dm_routine_claims": true` beside `"routine_claims": true` on a
  signed-identity MCP server, a Managed Agents routine that posts to its owner's
  DM signs its routine, the owner, the owner's DM and room `dm`, only while that
  run is live and the routine enabled and unedited. Daimon opens the DM before
  the run, sending nothing. It is off by default
  ([signed identity](docs/mcp-servers.md)).
