# Discord webhook posting under concurrent turns

`WebhookLoad.tla` checks posting capacity and route changes for concurrent
turns sharing parent channels. `Cadence.tla` checks two render windows with
the event's parent-channel spread and both global rate-limit modes.
`CardRecovery.tla` checks the durable initial card, ambiguous post response,
terminal edit, and restart reconciliation.
Run both through `TLA2TOOLS_JAR=... formal/check.sh` from the repository root.
These are finite abstractions of the code, not a proof of Discord or discord.py.

## Code paths and assumptions

| Model action | Production path |
| --- | --- |
| `CreateHook`, `Choose` | `DiscordPostTransport._webhook()` uses a per-parent, per-process asyncio lock, lists application-owned hooks, and creates up to three; `select_discord_webhook_id()` hashes thread ID modulo pool length. Direct parent posts use the first hook. MCP `_post_transport.own_webhook()` has its own per-parent process lock and shares the listed application hooks. |
| `PostHook`, `Hit429`, `NextWindow` | `DiscordPostTransport.send()` uses `wait=True`. discord.py normally waits on `retry_after`; a 429 still escaping the library is raised by the adapter. MCP `send_agent_message()` does the same. Hook execute allowance is modeled as five calls per two-second window. |
| `UnknownWebhook`, `PostBot` | Error 10015 evicts a cached hook and routes a send through the bot; missing permission, locked thread, unavailable parent, and disabled identity also use bot posting. The first answer chunk gets the agent name when identity is enabled and a webhook could not show the sender. MCP uses the same name-prefix rule. |
| `CommitIntent`, `RemoteAccept`, `PersistResponse` | `post_initial_turn_card()` commits an intent before `DiscordTurnLifecycle.post_initial()`; it records the returned message ID before the turn proceeds. |
| `FinishAnswer` | The normal prompted success path posts an initial card, edits the Done card, then edits that message with the answer. Progress edits, sealed responses, answer overflow, and notify-on-completion can add requests. |
| `SilentEnd` | An unprompted turn with no answer deletes its transient card. If delete fails, the lifecycle marks the discard failure and the caller retains the intent. |
| `Crash`, `BootLookup`, `RecoverEdit` | `bot.py` gates new turn admission behind orphan sweep, then reconciles the boot intent snapshot; `turn_card_recovery.py` scans history and edits every matching pending card before retirement. |
| `RecoverReplacement` | The pre-fix recovery path could replace an uneditable webhook card with a new bot message and retire the intent while the old card still displayed a pending button. Recovery now disables replacement and retains the intent on missing/unknown webhook token. Normal non-recovery edits may still replace a message. |

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
not escape a global ceiling. The three hooks are per **parent channel**, not
per thread. The stated five requests per webhook per two-second window is a
planning assumption: Discord says route limits can change and response
headers, including `retry_after`, are authoritative. The model conservatively
shares a hook quota across execute, edit and delete, though actual buckets can
depend on route and method.

The model assumes one process owns the per-parent creation lock, Discord
honors the stated quota, `retry_after` is finite, and each HTTP acceptance
returns a response except for the separately modeled ambiguous initial card.
It does **not** assume that a failed 429 automatically succeeds through bot
posting: the adapter propagates a final 429. It also does not bound webhook
creation, network latency, other traffic on the same egress IP or bot token,
answer overflow, or a permanent platform outage. Cross-process hook
creation is not serialized; concurrent processes can create more than the
intended three in one parent, though the Discord channel cap is 15. The
production worker cap of 200 concurrent turns is planned, not live. The
`WebhookLoad` interleaving model checks two or three turns across one or two
parents and one to three hooks. `Cadence` deterministically checks 15 and 200
active-turn request counts across 40 or 65 parents. It does not explore 200
independent turn interleavings or prove a latency bound.

## Checked configurations

| Config | Bound and result |
| --- | --- |
| `LoadSafe` | Two turns, one parent, one hook, three requests each, two hook requests per window: all delivered within three windows; no loss, duplicate answer, busy retry, or pool overflow. |
| `LoadDiscordQuota` | Two turns with the stated five hook requests per two-second window and 100 bot requests per window; six requests on one hook finish within two windows. |
| `LoadBotFallback` | Identity off, same two turns, bot allowance two per window: all delivered within three windows. This is capacity routing, not a promise that Discord accepts every bot request. |
| `LoadTwoChannels` | Two turns in two parents; each has one hook and finishes within two windows. |
| `LoadPoolThreeHooks` | Three turns select three hooks in one parent; creation stays at three, below the channel cap of 15. |
| `LoadUnsafe429` | Dropping a request at the first 429 violates `NoAnswerLost`. `test_exhausted_webhook_429_is_not_silently_reposted_by_bot` pins the real adapter's final-429 propagation; the library's finite retry is an assumption, not an end-to-end guarantee. |
| `LoadUnsafeDuplicateAnswer` | Replaying a recorded final answer violates `NoDuplicateAnswer`. Production posts once and edits a known message; lifecycle and turn-card tests pin that path. |
| `LoadUnsafeFallback` | Dropping the bot route with identity off violates `FallbackRoute`. Discord and MCP transport tests pin the bot route when identity is disabled or webhooks are unavailable. |
| `LoadUnsafeBackoff` | Ignoring `retry_after` permits repeated 429s in one window and violates `NoBusyRetry`. The safe model waits for the next window. |
| `LoadUnsafeBound` | Claiming completion within two windows when six calls share a hook that accepts two per window violates `BoundedPosting`. The corrected three-window bound is checked by `LoadSafe`. |
| `CadenceExpected` | 15 active turns across 40 parents, one state-change edit per turn per two-second window: route and IP global backlogs stay zero. |
| `CadenceWebhookRoutes`, `CadenceSixtyFiveRoutes` | At 200 active turns, 40 parents with three evenly selected hooks can absorb an initial post plus edit per turn at the assumed hook quota; 65 parents can absorb one edit per turn even if each parent has one hook. These check route capacity only. |
| `CadenceWebhookIPUnsafe`, `CadenceBotGlobalUnsafe` | At 200 active turns, one edit per turn per window is 200 requests against either global allowance of 100 per window. Both modes leave 100 requests queued after the first window. Transport tests pin that final webhook and bot 429s propagate; no fallback can promise delivery after global exhaustion. |
| `CadenceWebhookSkewUnsafe` | Five turns per parent, all mapped to one hook, plus simultaneous initial post and edit require ten calls in one window against five. `test_same_parent_threads_can_select_one_webhook` pins the hash-collision route. |
| `CardSafe` | A committed intent, one remote card, terminal edit or token-available recovery: no duplicate card/answer and no pending card after a finished turn, retirement, or recovery. |
| `CardUnsafeDuplicate` | Retrying an accepted initial post after losing its response creates two cards and violates `NoDuplicateCard`. The durable intent and history lookup avoid blind repost; Discord recovery tests cover the ambiguous response and duplicate discovery. |
| `CardUnsafeFinish` | Marking a turn finished before clearing its card violates `NoPendingAfterTurnEnds`. The lifecycle finishes the card before revealing the answer. |
| `CardUnsafeRecovery` | Retiring recovery before editing a pending match violates `NoPendingAfterRecovery`. Recovery tests keep the intent when card edits fail. |
| `CardUnsafeRetire` | Replacing an uneditable card and retiring its intent violates `NoPendingAfterRetirement`. The fix makes recovery edits refuse replacement; transport and recovery regression tests cover missing token and 10015. |
| `CardDeleteFailed` | An unprompted card delete fails; the turn ends silently but the intent remains active for later recovery. |
| `CardUnsafeDeleteRetire` | Retiring that intent anyway violates `NoPendingAfterRetirement`. The lifecycle records discard failure and the caller skips retirement; a regression test checks the flag. |
| `CardMissingToken` | With no token, recovery stays incomplete and the intent remains active. No false retirement is checked. The old pending card may remain until access is restored or an operator resolves it. |

`CardSafe` assumes a known card can be edited and a requested delete succeeds. It does not prove that every
crash is recoverable: Discord history lookup is bounded, a message can be
unfetchable, a webhook token can disappear, and an edit can fail. The
`CardMissingToken` configuration explicitly retains that residual state.
“No card left pending after recovery” is therefore a conditional safety rule:
when recovery retires an intent, every matching card it found has been
resolved. Pending cards after a failed recovery are visible in the retained
intent for another attempt. There is no unconditional eventual-clearance
claim.

## Load answer and fallback

The event rehearsal used one guild with 65 private team channels and 195
pre-created threads; the final soak used 40 channels and 120 threads, three
threads per parent. The expected rate is 50–60 turns/min, with roughly 15
turns in flight inferred from the lower-rate soak. A 200-turn cold burst was
observed with 195 sampled turns in flight. The final soak used each thread
eight to ten times. The planned worker and event-guild caps are 200; current
defaults are an unset global cap and three per tenant. These figures come
from the private R3 staging rehearsal report and are summarized here without
identifiers or operational details. R3 recorded zero Discord 429s in its
later cold and soak stages, but all synthetic prompts came from one QA bot
account and the rehearsal predates webhook identity mode. It does not
validate webhook mode.

At roughly 15 in flight, the maximum render cadence adds about 7.5 edits/s;
50–60 turns/min add about 2.5–3 initial/terminal/answer requests/s at the
three-request baseline. Both posting modes fit a free 50/s global allowance
under this estimate. At 200 in flight, the render ceiling is 100 edits/s,
before new cards and terminal updates: **both webhook IP and bot-token global
budgets are overloaded if every card changes on every tick**. The model
retains this counterexample for each mode. Edits occur only when card state
changes, so 100/s is a stress ceiling, not an observed steady rate.

The 200-card cold burst alone requires at least two global two-second windows
in either mode. Spread evenly over 40 parents, five initial posts per parent
exactly fill one hook's assumed route bucket; over 65 parents, each needs at
most four. If an edit lands in the same window, 40 parents with three evenly
used hooks still fit the route quota, but one-hook collisions create local
backlog. Thread IDs can collide modulo three, so three threads do not
guarantee three active hooks. The IP global bucket remains the bottleneck even
when route capacity is sufficient. This is a capacity lower bound, not a
delivery guarantee; hook creation, `retry_after`, other egress traffic, and
answer overflow can add windows.

**Answer:** webhook mode has better per-route distribution and keeps its
requests out of the bot-token global bucket, but it is not intrinsically safe
at the 200-turn maximum cadence because its unauthenticated requests share an
IP-based global ceiling. Bot mode has no per-webhook collision risk, but its
50/s bot-token ceiling also overloads at that cadence. Monitor actual
`X-RateLimit-Scope`, bucket headers, 429 counts and card-edit rate in staging;
the R3 zero-429 result does not settle webhook capacity. Automatic fallback
uses a bot post with the agent name on the first answer chunk when webhook
setup or a non-429 send fails. A final 429 propagates instead of switching
routes, and bot posting can itself be rate limited.
`DAIMON_AGENT_IDENTITY__ENABLED=false` sends all guilds through
the bot without agent identity. There is currently no per-guild identity
switch; automatic fallback is per destination/turn, and removing Manage
Webhooks permission may not disable already cached hooks.
