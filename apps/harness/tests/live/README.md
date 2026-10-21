# Live Luna evidence

Paid OpenAI Responses API, `gpt-6-luna` only, Codex refreshed after round-2 fixes
on 2026-10-21, 08:50–08:51 UTC; Agents SDK comparison retained from
08:07–08:09 UTC on the same date. These are live HTTP
harness transcripts, not mocked responses. Each backend used service UID 10000
and tool UID 10001. The production provider remains Azure; the OpenAI path is
explicitly gated by `HARNESS_LIVE_PROBE=openai` and
`HARNESS_RUNTIME_MODE=probe`, with Azure/MI configuration refused.

The 14 `<backend>-<scenario>.json` files contain the final passing run's durable
C3 events, independent provider usage, and request-by-request checks. Each loop
has 13 requests: 12 measured and one cancelled with `usage_unavailable`. Measured input/output tokens: Codex 6,269 / 262; Agents SDK 6,247 / 269.
Cached input: 0 for both; reasoning: 22 / 28, respectively. Every
measured observation matches all four provider counts exactly. Cancellation
observations contain no fabricated token counts. These totals cover only the
final passing run, not earlier diagnostic calls.

| Scenario | Codex | Agents SDK |
| --- | --- | --- |
| Plain output | Completed; 5 deltas, one message ID | Completed; one post-response delta |
| Automatic MCP echo | Completed; 2 requests | Completed; 2 requests |
| Ask → allow | Required action, then actual MCP execution | Same |
| Ask → deny | Required action; MCP execution refused | Same |
| Bash isolation | UID 10001; `OPENAI_API_KEY` absent | Same |
| Sequential MCP calls | 3 measured requests | 3 measured requests |
| Cancel | Interrupted after streamed text; unknown usage | Interrupted during model I/O; unknown usage |

`diagnostic-attempts.json` retains inventory and usage from saved earlier
attempts. Those runs exposed bundled Codex code-mode metadata selecting native
`exec`, default plugin cloning and a descendant cleanup race; the adapter now
uses a minimal direct-functions catalog, disables discovery/code mode, and
kills residual controller process-group members. Round-1 fixes additionally
disable bundled system skills and instruction injection; the relay removes
controller skill/environment messages by provenance and private paths from
controller continuations. Round-2 fixes preserve authoritative C3 input and
history verbatim, including marker-prefixed user text. An early probe observer also
tried to parse compressed JSON; the observer now requests identity encoding.
Early SDK observer setup failures occurred before a per-scenario evidence file
was written; this directory is not an account-wide usage ledger.

Run `../live_luna_probe.py` explicitly using the harness README instructions.
It never writes API keys, bearer headers, or credentials. The build-host probe
scopes its startup file scan to the private scratch root; the real container
mount-scan and Azure/managed-identity readiness remain separate gates.
