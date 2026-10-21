# Live Luna evidence

Paid OpenAI Responses API, `gpt-6-luna` only, 2026-10-21. These are live HTTP
harness transcripts, not mocked responses. Each backend used service UID 10000
and tool UID 10001. The production provider remains Azure; the OpenAI path is
explicitly gated by `HARNESS_LIVE_PROBE=openai`.

The 14 `<backend>-<scenario>.json` files contain the final passing run's durable
C3 events, independent provider usage, and request-by-request checks. Each loop
has 13 requests: 12 measured and one cancelled with `usage_unavailable`. Every
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
kills residual controller process-group members. An early probe observer also
tried to parse compressed JSON; the observer now requests identity encoding.
Early SDK observer setup failures occurred before a per-scenario evidence file
was written; this directory is not an account-wide usage ledger.

Run `../live_luna_probe.py` explicitly using the harness README instructions.
It never writes API keys, bearer headers, or credentials. The build-host probe
scopes its startup file scan to the private scratch root; the real container
mount-scan and Azure/managed-identity readiness remain separate gates.
