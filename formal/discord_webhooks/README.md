# Discord webhook posting under concurrent turns

`WebhookLoad.tla` checks posting capacity and route changes for concurrent
turns sharing parent channels. `Cadence.tla` checks the render tick and
lifecycle debounce over ten ticks with the event's parent-channel spread and
both Discord global rate-limit modes.
`CardRecovery.tla` checks the durable initial card, ambiguous post response,
terminal edit, and restart reconciliation.
Run all three through `TLA2TOOLS_JAR=... formal/check.sh` from the repository root.
These are finite abstractions of the code, not a proof of Discord or discord.py.

## Code paths and assumptions

| Model action | Production path |
| --- | --- |
| `CreateHook`, `Choose` | These capacity configs retain the earlier one-to-three-hook abstraction. The live Discord and MCP transports now create one hook per parent in a deduplicated background task, wait at most two seconds, and use the bot route while creation is pending. Existing hooks remain available by ID for edits. |
| `CreateHit429`, `ChooseAfterCreate` | A separate creation bucket can return a 66-second retry. `LoadCreateWaitUnsafe` waits for creation before the first post; `LoadCreateSafe` takes the bot fallback within the two-second budget. The staging identity-on stress run found that a 200-mention burst across 60 fresh parent channels blocked completions for over two minutes while inline `create_webhook` calls waited on 429 retries. |
| `PostHook`, `Hit429`, `NextWindow` | `DiscordPostTransport.send()` uses `wait=True`. discord.py normally waits on `retry_after`; a 429 still escaping the library is raised by the adapter. MCP `send_agent_message()` does the same. Hook execute allowance is modeled as five calls per two-second window. |
| `UnknownWebhook`, `PostBot` | Error 10015 evicts a cached hook and routes a send through the bot; missing permission, locked thread, unavailable parent, and disabled identity also use bot posting. The first answer chunk gets the agent name when identity is enabled and a webhook could not show the sender. MCP uses the same name-prefix rule. |
| `CommitIntent`, `RemoteAccept`, `PersistResponse` | `post_initial_turn_card()` commits an intent before `DiscordTurnLifecycle.post_initial()`; it records the returned message ID before the turn proceeds. |
| `FinishAnswer` | The normal prompted success path posts an initial card, edits the Done card, then edits that message with the answer. Progress edits, sealed responses, answer overflow, and notify-on-completion can add requests. |
| `SilentEnd` | An unprompted turn with no answer deletes its transient card. If delete fails, the lifecycle marks the discard failure and the caller retains the intent. |
| `Crash`, `BootLookup`, `RecoverEdit` | `bot.py` gates new turn admission behind orphan sweep, then reconciles the boot intent snapshot; `turn_card_recovery.py` scans history and edits every matching pending card before retirement. The orphan sweep also refuses replacement, so an unknown webhook cannot create a second bot card while leaving the original pending. |
| `RecoverReplacement` | The pre-fix recovery path could replace an uneditable webhook card with a new bot message and retire the intent while the old card still displayed a pending button. Recovery now disables replacement and retains the intent on missing/unknown webhook token. Normal non-recovery edits may still replace a message. |
| `PeriodicLookup`, `RecoveryFailure`, `AgeOut` | Startup and hourly passes serialize reconciliation. The hourly pass excludes local live-turn intent IDs and active card IDs; the age threshold exceeds the turn ceiling. An aged Discord intent becomes `unrecoverable` after a definite missing-webhook, missing-token, permission or deleted-thread failure, or after the configured number of failed passes. The pass count persists across restarts. A single transient API failure or cancellation keeps the intent active. With Manage Messages, recovery deletes only fetched messages that still carry this turn's pending button, including duplicates; a finished answer is preserved. A failed delete is logged and the terminal state records exhausted automatic recovery. |

`WebhookLoad` counts **three requests per prompted turn**: initial card,
terminal Done edit, answer edit. It models the final answer as one message and
one successful delivery. Its work-conserving scheduler advances a two-second
window only when no eligible request remains. `retryAt` represents the
library's `retry_after` wait. A turn can switch from hook to bot on 10015;
identity off routes directly to bot. Per-hook use and bot use reset each
window. The bot allowance is 50 requests per second, or 100 per model window;
the small TLC configs use lower allowances to expose the same boundary.

`discord.py` 2.7.1 sends token-URL webhook execute, edit and delete requests
without a bot `Authorization` header. [Discord's rate-limit documentation](https://docs.discord.com/developers/topics/rate-limits)
says unauthenticated requests use an **IP-based** global limit, while bot-token
requests use the bot's global limit; both are stated as 50 requests/s. Thus
webhook posts do not spend the bot-token global bucket, but a webhook pool does
not escape a global ceiling. The hooks are per **parent channel**, not
per thread. The stated five requests per webhook per two-second window is a
planning assumption: Discord says route limits can change and response
headers, including `retry_after`, are authoritative. The model conservatively
shares a hook quota across execute, edit and delete, though actual buckets can
depend on route and method.

The model assumes one process owns the per-parent creation lock, Discord
honors the stated quota, `retry_after` is finite, and each HTTP acceptance
returns a response except for the separately modeled ambiguous initial card.
It does **not** assume that a failed 429 automatically succeeds through bot
posting: the adapter propagates a final 429. The creation configs bound the wait
before bot fallback, but do not bound network latency, other traffic on the same egress IP or bot token,
answer overflow, or a permanent platform outage. Cross-process hook
creation is not serialized; concurrent processes can create more than the
intended one in one parent, though the Discord channel cap is 15. The
Discord's process-wide `max_concurrent_turns` defaults to `None` (no cap);
200 is a proposed deployment cap, not a live default. The per-tenant default
is three, with a separate event-guild override proposed at 200. The
`WebhookLoad` interleaving model checks two or three turns across one or two
parents and one to three hooks. `Cadence` deterministically checks 15 and 200
active-turn request counts across 40 or 65 parents. It models Discord's
10-second edit debounce and Slack's 5-second debounce on top of the two-second
driver tick. Balanced edit phases are an explicit steady-state assumption,
not a property guaranteed by the code. It does not explore 200 independent
turn interleavings or prove a latency bound.

## Checked configurations

| Config | Bound and result |
| --- | --- |
| `LoadSafe` | Two turns, one parent, one hook, three requests each, two hook requests per window: all delivered within three windows; no loss, duplicate answer, busy retry, or pool overflow. |
| `LoadCreateSafe`, `LoadCreateWaitUnsafe` | A separate create bucket returns a 66-second 429 retry. Bot fallback posts within the two-second bound; waiting for a hook violates `BoundedPosting` at window 33. |
| `LoadDiscordQuota` | Two turns with the stated five hook requests per two-second window and 100 bot requests per window; six requests on one hook finish within two windows. |
| `LoadBotFallback` | Identity off, same two turns, bot allowance two per window: all delivered within three windows. This is capacity routing, not a promise that Discord accepts every bot request. |
| `LoadTwoChannels` | Two turns in two parents; each has one hook and finishes within two windows. |
| `LoadPoolThreeHooks` | Historical three-hook capacity comparison: three turns select three hooks in one parent, below the channel cap of 15. Current code creates one hook. |
| `LoadUnsafe429` | Dropping a request at the first 429 violates `NoAnswerLost`. `test_exhausted_webhook_429_is_not_silently_reposted_by_bot` pins the real adapter's final-429 propagation; the library's finite retry is an assumption, not an end-to-end guarantee. |
| `LoadUnsafeDuplicateAnswer` | Replaying a recorded final answer violates `NoDuplicateAnswer`. Production posts once and edits a known message; lifecycle and turn-card tests pin that path. |
| `LoadUnsafeFallback` | Dropping the bot route with identity off violates `FallbackRoute`. Discord and MCP transport tests pin the bot route when identity is disabled or webhooks are unavailable. |
| `LoadUnsafeBackoff` | Ignoring `retry_after` permits repeated 429s in one window and violates `NoBusyRetry`. The safe model waits for the next window. |
| `LoadUnsafeBound` | Claiming completion within two windows when six calls share a hook that accepts two per window violates `BoundedPosting`. The corrected three-window bound is checked by `LoadSafe`. |
| `CadenceExpected` | Fifteen ongoing Discord turns obey the ten-second debounce over two-second render ticks. |
| `CadenceWebhookGlobalSafe`, `CadenceBotGlobalSafe` | At 200 ongoing Discord turns with balanced edit phases, 40 edits plus six other requests per two-second tick stay below either modeled 100-request global allowance. The six other requests represent roughly three initial, terminal, or answer operations per second. |
| `CadenceSlackDebounce` | Slack's five-second lifecycle debounce permits at most one edit per turn every three two-second driver ticks. This checks cadence only; it does not assign a Discord global limit to Slack. |
| `CadenceWebhookRoutes`, `CadenceSixtyFiveRoutes` | At 200 cold starts, 40 parents with three balanced hooks or 65 parents with one hook can carry initial cards; first progress edits wait five ticks. These check route capacity only. |
| `CadenceWebhookNoDebounceUnsafe`, `CadenceBotNoDebounceUnsafe` | Counterfactuals with the lifecycle debounce removed permit 200 edits every tick and violate the modeled global allowance. The current code cannot produce this steady rate. |
| `CadenceColdBurstGlobalUnsafe` | A synchronized 200-card initial-post burst can exceed one modeled global window even with debounce. This is a real burst risk. |
| `CadenceWebhookSkewUnsafe` | The no-debounce counterfactual combines five initial posts and five edits on one hook in one window. `test_same_parent_threads_can_select_one_webhook` pins the hash-collision route, while the simultaneous edit assumption is deliberately unsafe. |
| `CardSafe` | A committed intent, one remote card, terminal edit or token-available recovery: no duplicate card/answer and no pending card after a finished turn, retirement, or recovery. |
| `CardUnsafeDuplicate` | Retrying an accepted initial post after losing its response creates two cards and violates `NoDuplicateCard`. The durable intent and history lookup avoid blind repost; Discord recovery tests cover the ambiguous response and duplicate discovery. |
| `CardUnsafeFinish` | Marking a turn finished before clearing its card violates `NoPendingAfterTurnEnds`. The lifecycle finishes the card before revealing the answer. |
| `CardUnsafeRecovery` | Retiring recovery before editing a pending match violates `NoPendingAfterRecovery`. Recovery tests keep the intent when card edits fail. |
| `CardUnsafeRetire` | Replacing an uneditable card and retiring its intent violates `NoPendingAfterRetirement`. The fix makes recovery edits refuse replacement; transport and recovery regression tests cover missing token and 10015. |
| `CardDeleteFailed` | An unprompted card delete fails; the turn ends silently but the intent remains active for later recovery. |
| `CardUnsafeDeleteRetire` | Retiring that intent anyway violates `NoPendingAfterRetirement`. The lifecycle records discard failure and the caller skips retirement; a regression test checks the flag. |
| `CardMissingToken` | With no token, fresh recovery stays incomplete and the intent remains active. The aged case is covered by `CardStaleUnrecoverable`. |
| `CardStaleUnrecoverable`, `CardStaleDelete` | After a definite recovery failure, an aged intent becomes terminal. Without Manage Messages the old pending card may remain; with permission and successful deletion it is removed. Both states release the tidy gate. |
| `CardAnsweredSafe`, `CardUnsafeAnsweredDelete` | A recorded message ID may point at an answered card before the intent retires. Checking the turn's pending button preserves the answer; blind deletion violates `NoDeletedAnswer`. The Discord age-out tests cover answered cards and multiple matches. |
| `CardPeriodicSafe`, `CardPeriodicLiveUnsafe` | Periodic recovery while the process is up excludes a still-running turn. Removing that guard retires a live turn and violates `NoLiveTurnRetired`. The Discord periodic-sweep tests cover local intent filtering and persistent failed-pass counts. |
| `CardUnsafeStale` | Keeping an aged unresolved intent active violates `NoStaleActive`; this is the old permanent `turn_in_progress` path. |

`CardSafe` assumes a known card can be edited and a requested delete succeeds. It does not prove that every
crash is recoverable: Discord history lookup is bounded, a message can be
unfetchable, a webhook token can disappear, and an edit can fail. The
`CardMissingToken` retains that residual state until both the configured age
and a definite recovery failure or the configured number of failed passes are
present. The finite model abstracts that threshold as one `RecoveryFailure`
action; the database-backed tests check the counter across process restarts.
“No card left pending after recovery” is therefore a conditional safety rule:
when recovery retires an intent, every matching card it found has been
resolved. Pending cards after a failed recovery are visible in the retained
intent for another attempt. An aged failure moves to `unrecoverable`; the
pending card may remain if Discord denies the final bot delete. There is no
unconditional eventual-clearance claim.

## Load answer and fallback

The event rehearsal used one guild with 65 private team channels and 195
pre-created threads; the final soak used 40 channels and 120 threads, three
threads per parent. The expected rate is 50–60 turns/min, with roughly 15
turns in flight inferred from the lower-rate soak. A 200-turn cold burst was
observed with 195 sampled turns in flight. The final soak used each thread
eight to ten times. A process-wide cap of 200 and an event-guild cap of 200
were proposed for the event; `max_concurrent_turns` currently defaults to
`None`, and the per-tenant default is three. These figures come from the
private R3 staging rehearsal report and are summarized here without
identifiers or operational details. R3 recorded zero Discord 429s in its
later cold and soak stages, but all synthetic prompts came from one QA bot
account and the rehearsal predates webhook identity mode. It does not
validate webhook mode.

The two-second driver tick only checks for changed state. Discord's lifecycle
allows a working-card edit after ten seconds, and Slack's after five seconds;
the next driver tick performs it. With a continuous two-second tick, that is
at least five ticks between Discord edits and three between Slack edits.
Terminal edits bypass this debounce. At roughly 15 Discord turns in flight,
the maximum sustained progress rate is about 1.5 edits/s. At 200 it is about
20 edits/s. The 50–60 turns/min expectation adds about 2.5–3 initial,
terminal, and answer requests/s at the three-request baseline. Keeping 200
turns in flight at that arrival rate assumes turns last roughly 3.5 minutes
or longer. If 200 turns each last about 15–18 seconds, the same baseline adds
roughly 33–40 non-edit requests/s; with 20 edits/s, demand approaches or
exceeds 50/s. With staggered edits and the lower arrival rate, both
webhook-IP and bot-token modes remain below their separate 50/s global
allowances in the model. Slack's 200-turn progress ceiling is about
33 edits/s after tick rounding; this Discord global allowance does not apply
to Slack.

The 200-card cold burst alone needs at least two global two-second windows in
either mode. Spread over 40 parents, five initial posts per parent fill one
hook's assumed route bucket; over 65 parents, each needs at most four. The
first progress edit cannot land in that same window because of debounce.
Thread IDs can still collide modulo three, and an uneven burst can create
local backlog. A synchronized wave of 200 edits after the debounce can also
exceed a single global window even though the sustained average is below
50/s. This is a capacity calculation, not a delivery guarantee; hook
creation, `retry_after`, other egress traffic, and answer overflow add load.

**Answer:** webhook mode has better per-route distribution and keeps its
requests out of the bot-token global bucket. With lifecycle debounce, the
modeled 200-turn sustained progress rate plus baseline turn traffic fits the
50/s global ceiling in both webhook-IP and bot-token modes. Cold starts,
synchronized edits, terminal traffic and one-second clustering can still
trigger 429s. Bot mode has no per-webhook collision risk. Monitor actual
`X-RateLimit-Scope`, bucket headers, 429 counts and card-edit rate in staging;
the R3 zero-429 result does not settle webhook capacity. Automatic fallback
uses a bot post with the agent name on the first answer chunk when webhook
setup or a non-429 send fails. A final 429 propagates instead of switching
routes, and bot posting can itself be rate limited.
`DAIMON_AGENT_IDENTITY__ENABLED=false` sends all guilds through
the bot without agent identity. There is currently no per-guild identity
switch; automatic fallback is per destination/turn, and removing Manage
Webhooks permission may not disable already cached hooks.
