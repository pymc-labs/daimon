# Discord turn card lifecycle

Question: can an older Discord write restore a visible Working card after the
turn's terminal card or restarted notice has been applied?

`CardLifecycle.tla` is the current bounded-terminal model. It separates issuing
a write, dispatching it, and Discord applying it. A terminal edit can overtake
on-wire progress; its later completion schedules a repair on the same message.
`CardHandover.tla` preserves the
reviewed intermediate design from `73cef3f9c`; its three broken modes are
witnesses for Astra's replacement, successor-edit, and repair-gate reports.
The older safe mode describes that intermediate design, **not** the code on
this branch. It remains in the registry so those counterexamples cannot be
accidentally modeled away.

## Action-to-code map

| Model action | Code boundary or historical source |
| --- | --- |
| `IssueProgress` | `packages/adapters/discord/daimon/adapters/discord/lifecycle.py:754-764`, `:550-573`; shielded progress dispatches under the message lock |
| `EndTurn`, `IssueQueuedTerminal` | `lifecycle.py:802-811`, `:543-587`; terminal dispatches after a bounded settle, outside the progress lock; `IssueQueuedTerminal` is historical queue mode only |
| `ApplyProgress`, `ApplyTerminal` | `lifecycle.py:589-608`, `:611-692`; the fake's transport completion stands for Discord applying a request |
| `Handover` | `lifecycle.py:362-380`, `:442-446`; the new lifecycle shares the queue and increments its epoch |
| `DeleteCard`, `StartReplacement`, `FinishReplacement` | `lifecycle.py:595-669`, `:670-705`; #607 recovery and returned transport replacements |
| `Repair` | `lifecycle.py:147-214`; a successful stale completion schedules a same-message edit, with a strong task reference and a 120-second timeout |
| `Crash` | `packages/adapters/discord/daimon/adapters/discord/bot.py:1130-1158`; a new process enters the boot barrier |
| `RetireOrphan`, `DropOrphan` | `bot.py:1159-1275`, `packages/adapters/discord/daimon/adapters/discord/turn_card_recovery.py:511-569`; the boot sweep edits the old message or reports that it could not |
| `CardHandover` edit/send/terminal/handover/repair actions | `73cef3f9c:packages/adapters/discord/daimon/adapters/discord/lifecycle.py:80-205`, `:423-690`, `:746-775` |

`CardLifecycle` uses one card, one on-wire progress edit, one optional
replacement, and two lifecycle owners. One is enough to show terminal
overtaking; the second owner shows handover. `CardHandover` permits three
owners and three card IDs so a second handover and stale replacement can be
distinguished. The model's card/owner counts are witness bounds, not throughput
estimates. The 10-second progress debounce, five-second settle, 10-second
terminal edit and 120-second repair come from `lifecycle.py:70-73,754-764,802-811,576-587,196-204`.
The urgent webhook edit uses its own discord.py adapter lock in
`post_transport.py:490-500`. Bot-message edits remain subject to discord.py's
bucket wait, but the terminal edit timeout bounds that wait. The model assumes
the terminal edit succeeds within this bound and omits time. The three
recovery attempts and five-second retries in
`turn_card_recovery.py:361,483-505` are outside the write-order property.

| Config | Verdict | States | Meaning |
| --- | --- | ---: | --- |
| `CardPre655` | violates `TerminalStable` | 23 | original direct edits can arrive after terminal |
| `Card655` | violates `TerminalStable` | 23 | #655's repair permits a stale interval |
| `Card655Replacement` | violates `NoStaleReplacement` | 32 | a late replacement can show Working separately |
| `Card655Handover` | violates `RepairScheduled` | 61 | old repair ownership is lost at handover |
| `CardQueue` | clean | 148 | a late progress completion schedules repair within one live process |
| `CardQueueRestart` | violates `RestartStable` | 57 | an old request may land after boot retirement |
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
| Started progress calls eventually return | Needed for a late-write repair to run; an indefinitely hung call cannot overwrite the terminal card |
| Urgent terminal and repair edits succeed within their 10 and 120-second bounds | Unsupported by a Discord contract; transport failure is outside the clean model |
| Stale replacement deletion or fallback control stripping succeeds | Assumed only by historical `CardHandoverSafe`; real transport failures remain possible |

## Accepted limitations

`CardQueueRestart` deliberately remains an expected `RestartStable` violation.
The old process sends a Working edit, then dies. The new process edits that
same card to say it restarted. Discord can apply the old request afterward and
restore Working and Stop. The per-message queue exists only in the live
process, so it cannot order those two writes. The live-process invariant is:
after a stale write changes an applied terminal card, repair is scheduled;
assuming the bounded repair succeeds, the terminal card is reapplied. Readers
can see Working and Stop between the late write and the repair.
`test_lifecycle.py::test_late_progress_edits_cannot_overwrite_terminal_delivery`
replays that interval with a held progress request and checks final same-message
repair. `test_post_transport.py::test_urgent_webhook_edit_uses_an_independent_adapter`
checks the separate webhook adapter context.
Discord documents
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
