- **A watch's report reads through a signed server wherever it is posted.** With
  `"wake_claims": true`, a report in the new thread a watch opens signs `wake` with that
  thread as its room, as a report in the watch's own thread does. With
  `"dm_routine_claims": true` as well, a report to the owner's DM signs `wake`, the owner
  and their DM, as a DM routine does; Daimon opens that DM before the report, sending
  nothing. A report still writes nothing ([MCP servers](docs/mcp-servers.md)).
