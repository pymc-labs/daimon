# Discord turn card lifecycle

Question: can an older Discord write restore a visible Working card after the
turn's terminal card or restarted notice has been applied?

`CardLifecycle.tla` is the current queue model. It separates queuing a write,
dispatching it, and Discord applying it. `CardHandover.tla` preserves the
reviewed intermediate design from `73cef3f9c`; its three broken modes are
witnesses for Astra's replacement, successor-edit, and repair-gate reports.
The older safe mode describes that intermediate design, **not** the code on
this branch. It remains in the registry so those counterexamples cannot be
accidentally modeled away.

## Action-to-code map

| Model action | Code boundary or historical source |
| --- | --- |
| `IssueProgress` | `packages/adapters/discord/daimon/adapters/discord/lifecycle.py:737`, `:746`, `:515-581` (`_maybe_flush`, shielded task, dispatch under message lock) |
| `EndTurn`, `IssueQueuedTerminal` | `lifecycle.py:751`, `:814`, `:515-581`; the terminal request is registered before waiting for the lock |
| `ApplyProgress`, `ApplyTerminal` | `lifecycle.py:595-669`, `:515-588`; the fake's transport completion stands for Discord applying a request |
| `Handover` | `lifecycle.py:362-380`, `:442-446`; the new lifecycle shares the queue and increments its epoch |
| `DeleteCard`, `StartReplacement`, `FinishReplacement` | `lifecycle.py:595-669`, `:670-705`; #607 recovery and returned transport replacements |
| `Repair` | `lifecycle.py:165-208`; defensive repair scheduling after a detected stale completion |
| `Crash` | `packages/adapters/discord/daimon/adapters/discord/bot.py:1130-1158`; a new process enters the boot barrier |
| `RetireOrphan`, `DropOrphan` | `bot.py:1159-1275`, `packages/adapters/discord/daimon/adapters/discord/turn_card_recovery.py:511-569`; the boot sweep edits the old message or reports that it could not |
| `CardHandover` edit/send/terminal/handover/repair actions | `73cef3f9c:packages/adapters/discord/daimon/adapters/discord/lifecycle.py:80-205`, `:423-690`, `:746-775` |

`CardLifecycle` uses one card, one on-wire progress edit, one optional
replacement, and two lifecycle owners. One is enough to show terminal
overtaking; the second owner shows handover. `CardHandover` permits three
owners and three card IDs so a second handover and stale replacement can be
distinguished. The production bounds are unbounded: these are witness bounds,
not throughput estimates. Discord's 10-second progress debounce comes from
`lifecycle.py:70,737`; the five-second local wait from `:71,792`. Neither
limits an already dispatched request, so the model omits time. The three
recovery attempts and five-second retries in
`turn_card_recovery.py:361,483-505` are outside the write-order property.

| Config | Verdict | States | Meaning |
| --- | --- | ---: | --- |
| `CardPre655` | violates `TerminalStable` | 23 | original direct edits can arrive after terminal |
| `Card655` | violates `TerminalStable` | 23 | #655's repair permits a stale interval |
| `Card655Replacement` | violates `NoStaleReplacement` | 32 | a late replacement can show Working separately |
| `Card655Handover` | violates `RepairScheduled` | 61 | old repair ownership is lost at handover |
| `CardQueue` | clean | 142 | serialized writes within one live process |
| `CardQueueRestart` | violates `TerminalStable` | 58 | an old request may land after boot retirement |
| `CardQueueOrphanDrop` | violates `NoWorkingAfterRetirement` | 20 | boot lacks a route to edit the old card |
| `CardHandoverSafe` | clean | 1,564 | historical intermediate under its bounded assumptions |
| `CardHandoverUnsafe` | violates `NoStaleReplacement` | 171 | stale replacement is left visible |
| `CardHandoverSuccessorUnsafe` | violates `SettledCards` | 154 | successor edit is not tracked |
| `CardHandoverGateUnsafe` | violates `RepairQueuedWhenNeeded` | 379 | final stale completion fails to schedule repair |

Plain counterexamples:

1. #655 sends an answer before an older progress edit completes. Discord then
   applies the older edit and restores Working and Stop.
2. A deleted card prompts a replacement send. The answer uses another card;
   the older send finishes later and leaves a second Working card.
3. At handover, the old lifecycle drops repair ownership. The successor's
   answer is applied, then old progress changes that card with nobody left to
   repair it. The intermediate design also lost repair when an unrelated
   replacement completion drained last.
4. On restart, the old process has already sent progress. Boot applies the
   restarted notice, then Discord applies the old request. A process-local
   queue cannot observe that request after process death.
5. Boot finds an orphaned webhook card but has no matching webhook token. Its
   retirement edit is dropped, so the old Working card can remain visible.

## Assumptions and limits

| Assumption | Status |
| --- | --- |
| Requests to Discord may arrive in arbitrary order unless the process waits for one response before sending the next | Unsupported by a Discord contract; the fake exercises both orders |
| A returned successful edit means that edit has been applied | Unsupported by a Discord contract; required for `CardQueue` to be clean |
| A request sent before process death may still land after death | Unsupported but deliberately allowed in `CardQueueRestart` |
| A missing-card replacement can complete after its caller is cancelled | Exercised by the adapter fake; `lifecycle.py:595-705` contains awaits on this path |
| Boot can edit the orphan card | Unsupported when the webhook token, permissions, or message is unavailable; a dropped retirement is logged and is outside the clean queue config |
| Started transport calls eventually return | Fairness assumption for eventual delivery; no liveness claim is made for a hung call |
| Stale replacement deletion or fallback control stripping succeeds | Assumed only by historical `CardHandoverSafe`; real transport failures remain possible |

## Accepted limitations

`CardQueueRestart` deliberately remains an expected `TerminalStable` violation.
The old process sends a Working edit, then dies. The new process edits that
same card to say it restarted. Discord can apply the old request afterward and
restore Working and Stop. The per-message queue exists only in the live
process, so it cannot order those two writes. Discord documents
[eventual consistency and reordering](https://docs.discord.com/developers/reference#consistency)
without a bound for draining an already-sent request. No finite local wait
establishes the strict invariant across a worker restart.

`CardQueueOrphanDrop` shows another visible failure: if boot cannot edit a
webhook card, its old Working state can remain. The boot sweep logs a dropped
retirement with the reason, message ID, and turn ID when known.

`replay.py` consumes the structured `turn.card_*` log stream and maps issued,
dispatched, completed, dropped, terminal, handover, repair, and orphan events
to these actions. Its check is a trace monitor of the model's write ordering;
it cannot infer Discord-side arrival from an HTTP response. The fake trace
passes in `test_card_replay.py`. The staging sample before deployment has
zero new replayable events (0/0 traces, 1,000 legacy lines checked). Staging
replay is pending until this code reaches staging.
