# Request provenance in handoff and tool recordings

The handoff recordings include four session reads that verify authorization
before unattended messages are sent.

| Recording / zero-based effect index | Request | What the request verifies |
| --- | --- | --- |
| `handoff_full.json` / 3 | `GET /v1/sessions/<id:3>` | The unattended checkpoint uses the session's own agent, server URLs, and conversation rules channel. |
| `handoff_full.json` / 5 | `GET /v1/sessions/<id:3>` | Session authorization is checked again immediately before sending one unattended message. |
| `handoff_transcript.json` / 4 | `GET /v1/sessions/<id:3>` | The unattended checkpoint uses the session's own agent, server URLs, and conversation rules channel. |
| `handoff_transcript.json` / 6 | `GET /v1/sessions/<id:3>` | Session authorization is checked again immediately before sending one unattended message. |

The documented runner in [README.md](README.md) replays the affected scenarios
with `--turn-path legacy`: `mcp_start_turn`, `mcp_continue_turn`, `handoff_full`,
and `handoff_transcript`.

## Execution grants

`mcp_continue_turn.json` and `mcp_cancel_turn.json` model sessions opened
through agent chat, so recorded session responses carry
`daimon_agent_chat=true`.
The cancel recording checks ownership and the caller's access before waiting
for the driver to allow cancellation, then checks authorization again after
waiting. Once authorized, the interrupt is unconditional.

Both legacy and mux replay against the same recordings.

## Routine sandbox opt-in

The per-routine sandbox opt-in adds one keyword to the scheduler's
`headless_runner.run_turn` call: `sandbox`, the routine's recorded opt-in
(`routine_sandbox.RoutineSandbox`: the agent version and environment revision
the admin approved, and the callback that clears a drifted opt-in), or null.
`scheduler_run.json` and `sealed_channel.json` record `"sandbox": null` in
that call's arguments because neither routine is opted in. Both recordings
replay on the legacy and mux paths using the documented runner in
[README.md](README.md).
