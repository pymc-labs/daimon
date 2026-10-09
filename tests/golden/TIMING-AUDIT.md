# Golden timing audit

Audited all 33 source tests, their selected helpers/fixtures and current production boundaries on unchanged integration `17ce8c2f46ed9eede9567afc8689b43df800272c`. The three short fixture timing assumptions are now synchronized to actual stream/card events. Original production behavior and assertions remain; no larger sleep, reduced assertion, sorted effect sequence or retry of a failed replay is used.

Application/SQL/observation clocks are frozen, periodic renderer ticks are blocked, forced terminal renders still run, and background DB writes are drained before snapshotting. Long host/checkpoint watchdogs are safety guards, rather than triggers for the expected behavior. The runner's 120s process guard and 30s event-wait guards fail missing progress instead of selecting a different golden transcript. sleep(0) in posted-control fixtures is a scheduling yield tied to observed trace state, with no wall-clock or iteration cap.

| Scenario | Timing control / audit result |
| --- | --- |
| `timer` | Explicit `fire_at` / `now` drive DB scheduling and dispatch decisions; no real timer is started. |
| `env_mount_failure` | Replacement/delete/add SDK calls are awaited sequentially; no delayed cancellation or polling. |
| `plain_turn` | Immediate scripted SSE events; periodic rendering is frozen until forced final render. |
| `tool_use` | Immediate scripted tool/idle events and unattended read confirmation; no polling deadline. |
| `approval_card` | Original 200 × 5ms polling cap now waits for the actual confirm callback; original prompt, blocking and no-premature-confirmation assertions remain. |
| `approval_card_discord` | Posting is awaited; source yields with sleep(0) until trace exists, then settles the real card. No elapsed-time deadline drives the click. |
| `approval_card_slack` | Posting is awaited; source yields with sleep(0) until trace exists, then settles the real card. No elapsed-time deadline drives the click. |
| `cancel_mid_stream` | Original 20ms timer now waits for the first SSE event to be consumed; unchanged source asserts partial content, interrupt POST and clean acknowledgment. |
| `reconnect` | RaiseConnection is a scripted stream step after a real message; replay and second stream are awaited, with no competing timed trigger. |
| `rate_limit` | Scripted 429 carries literal retry-after=30; callback date uses fixed now. No source timer races setup; no source-driven retry sleep is scheduled. |
| `mcp_degraded` | Failure, reply and terminal idle are sequential scripted SSE events; no delayed trigger. |
| `ceiling` | A controlled wait_for waits for first body read, advances FakeClock by the exact remaining deadline, cancels/drains the real pump and asserts ceiling/closure/cleanup. |
| `billing_replay` | RaiseReadTimeout is a scripted event step, followed by history replay and terminal stream; no elapsed-time read timeout races setup. |
| `cold_discord` | Real cold-session creation and adapter turn are awaited; epoch clocks freeze duration/render timers, and immediate SSE completes before the long host watchdog. |
| `cold_slack` | Real cold-session creation and adapter turn are awaited; epoch clocks freeze duration/render timers, and immediate SSE completes before the long host watchdog. |
| `dm_delivery` | Two listener deliveries and duplicate are awaited in sequence; real core/HTTP streams are immediate, with no test-side delayed trigger. |
| `plain_discord` | Adapter dispatch awaits complete scripted SSE; no source-side delayed event. Periodic renderer is frozen. |
| `plain_slack` | Adapter dispatch awaits complete scripted SSE; no source-side delayed event. Periodic renderer is frozen. |
| `blocked_balance` | Admission refuses before streaming; fixed application/SQL dates, no timer task. |
| `blocked_cap` | Admission refuses before streaming; fixed usage/SQL dates, no timer task. |
| `dead_session` | Scripted 404 / replacement / fresh stream are sequential; expired history has explicit 404, with no timeout impersonating an unscripted response. |
| `handoff_full` | Injected `_now` and `_no_sleep`; immediate checkpoint SSE and file transfer. Fixed 180s checkpoint watchdog exceeds the entire 120s scenario-process guard. |
| `handoff_transcript` | Injected `_now` and `_no_sleep`; scripted archived-session refusal and history response; no wall-time polling. |
| `handoff_history` | Injected `_now` and `_no_sleep`; scripted deleted-session/history 404; no checkpoint starts. |
| `wake_continuation` | Continuation is written/claimed/dispatched inside awaited adapter turns; latest-human lookup is explicitly stubbed, not time-polled. |
| `handoff_continuation` | Three awaited turns with fresh event IDs; continuation is drained before return, latest-human lookup is stubbed, no elapsed-time trigger. |
| `dm_turn` | Admission, two core turns, history and duplicate are sequential awaits over immediate router SSE; no timed callback. |
| `sealed_channel` | Host run-turn boundary is an awaited async fake; routine dates are explicit, and no scheduler loop is started. |
| `scheduler_run` | Host run-turn boundary and resolver are awaited fakes; no scheduler clock tick is needed. |
| `mcp_start_turn` | Tool awaits session/create/event acceptance HTTP responses directly; no source-side sleep/deadline. |
| `mcp_continue_turn` | Tool awaits history/session/event acceptance HTTP responses directly; no source-side sleep/deadline. |
| `mcp_cancel_turn` | Tool awaits interrupt event POST directly; no stream or cancel timer is started. |
| `cli_session_get` | Synchronous CLI invocation awaits the scripted retrieve response; no polling or turn timeout. |

The explicit regeneration target is excluded by default pytest testpaths and skips unless `DAIMON_ORACLE_REGEN_STRESS=1`. Only N3 may opt in on unchanged integration:

```bash
DAIMON_ORACLE_REGEN_STRESS=1 uv run pytest -n 2 -q tests/golden/test_regeneration_load.py
```

Each pass regenerates all 33 files with at most two concurrent source processes. Run five passes consecutively, require every original scenario to pass, and compare every file byte-for-byte with the preceding pass (including the initial checked-in transcript). Store separate logs and SHA256 manifests for all five passes in the READY report. No full repository suite is repeated as part of this targeted proof.
