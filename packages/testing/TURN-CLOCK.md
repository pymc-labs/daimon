# Catalog turn clock

The opt-in `daimon.testing.turn_clock.ClockedLifecycle` forwards the real
lifecycle without completing a turn or deciding its outcome. It samples the
supplied monotonic clock after the actual terminal delegate settles. Later
invocation return, output capture and surface-context cleanup have separate
timestamps.

`completion_time` requires the independently acknowledged session/root and a
fresh native journal. Missing, foreign, delegated, preview, gap and duplicated
terminals cannot supply completion authority. The caller must validate its
persisted send claim and binding before providing that journal. Serialized
references and events are checked by value; object identity grants no authority.

`await_root_completion` applies a source completion budget to the actual root
callback plus that native proof. It cancels and drains on a late or absent root,
refused proof or caller cancellation. Verification and later invocation return
remain subject to an independent offline watchdog. A successful callback or
plain invocation return alone cannot stop the root deadline.

Share one `daimon.testing.host_clock.HostClock` across source-step, lifecycle,
headless-return and platform-post boundaries. It wraps an injectable clock and
assigns one increasing observation sequence even at equal times. Closed
`ClockSample` records retain that clock domain and order through JSON roundtrips;
their labels are diagnostics, never native root or transport acceptance proof.
The lifecycle exposes the actual terminal's sample for observation joins.

These helpers run no provider calls and change no application default. The
actual SDK/core/mux regression controls use offline native tapes and independently
set completion arrivals. They test timely versus late completion and return
cleanup beyond a source budget; they do not certify a frozen catalog cell.

Existing source gaps, original assertions and oracle windows remain intact.
Native-prefix execution and complete scenario verdicts are reported separately.

The ordered QA dispatcher now binds these helpers for Anthropic, OpenAI and
Gemini. `await_clocked_step` keeps the original cancellation budget for generic
hooks. Only the private signal from a settled real lifecycle and independently
verified native send/binding/fresh journal releases that root deadline. A later
return stays watchdog bounded. Returned evidence must name the same session and
root as the verified completion; callbacks or clock labels cannot supply them.

Inject a `HostClock` with `use_host_clock(clock)` around a catalog invocation.
The context follows asynchronous children and restores the caller afterward.
Actual adapter harnesses and headless-return capture use that same clock. Step
receipts serialize opening, verified completion and closing samples, preserving
one sequence at equal times without renumbering message-local identities.
Pending steps have no measured samples. Terminal evidence records actual root
completion; visible headless text records its later return boundary.

This binding preserves original catalog bytes, assertions, native payloads,
request receipts, unsupported-operation barriers and typed PENDING gaps. N9's
ordered-step unit already removed the 14 timeout barriers per provider column;
clock binding does not count those again. Controlled timing microcases remain
unscored, and full-cell verdicts are reported separately from timed prefixes.
