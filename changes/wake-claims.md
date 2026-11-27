- **Signed servers can tell running and queued turns from fresh messages.**
  With `"wake_claims": true` on a signed-identity MCP server, each call of a
  person's running turn also signs `wake: turn`. A timer, watch report, finished
  long job, hand-off or resumed form that runs in its own thread signs its
  reason, that thread and its kind as for a routine, and no person. It is off by
  default and independent of `routine_claims`
  ([Reads from a signed server](docs/unattended.md#reads-from-a-signed-server)).
