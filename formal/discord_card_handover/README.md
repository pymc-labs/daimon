# Discord card recovery handover

Question: if the old lifecycle's progress edit completes after the recovery
lifecycle answers, does some owner still have the information needed to repair
the card? `RepairOwned` requires a queued repair whenever a late edit has
replaced an answered card with Working. The unsafe config drops ownership and
finds the answer-then-progress counterexample. The safe config transfers the
pending task and repair owner. This is bounded to one card and one old edit.

| Model action | Code boundary |
| --- | --- |
| OldFailure | `lifecycle.py:854`, after terminal edit |
| Handover | `lifecycle.py:285` and constructor `adopt_pending_progress` at `:230` |
| Answer | `lifecycle.py:624`, after answer edit |
| CompleteOldEdit | `lifecycle.py:466`, after awaited transport edit |
| Repair | `lifecycle.py:508`, after awaited edit |

The model starts with one progress edit already in flight. `SafeHandover` is
the only switch; no time constant is needed to exhibit a late completion.
Discord applying a completed edit to the same card is an assumption, supported
by the recorder in `test_recovery_answer_repairs_old_progress_edit_that_lands_late`.
Network failure during repair and multiple successive recovery lifecycles are
outside this bound. The Python regression replays the unsafe order and verifies
the repaired answer; no live Discord contract trace was used.

| Config | Verdict | Distinct states |
| --- | --- | ---: |
| `CardHandoverSafe` | clean | 8 |
| `CardHandoverUnsafe` | violates `RepairOwned` | 7 |

Counterexample: the old turn fails, the card passes to recovery, recovery
answers, then the old progress edit lands. Without transferred ownership, no
repair is pending and the card stays on Working. The check is provisional for
live Discord behavior; the recorder replay passes, but no upstream contract
trace was collected.
