# Discord card write sequencer

Question: after a failed turn hands its card to a successor, can an older
progress edit or replacement leave a visible Working card after the successor
answers? The model checks one original card, one replacement send, one terminal
replacement, and one progress edit per lifecycle across at most two handovers.
Network requests start and complete in separate actions. It includes the
repair scheduling gate: `repairQueued` must be set when the last in-flight
write drains, even if that last write was a stale replacement that did not land.

| Model action | Code boundary |
| --- | --- |
| `StartEdit`, `CompleteEdit` | `lifecycle.py` `_CardWriteSequencer.begin/complete`, surrounding `_perform_edit_message`'s awaited transport edit |
| `StartSend`, `CompleteSend` | `lifecycle.py` `_perform_edit_message`'s missing-card send and returned-replacement path, followed by `_reconcile_late_replacement` |
| `Failure`, `CompleteFailure`, `Answer`, `CompleteAnswer` | `lifecycle.py` `_flush_terminal` and `_deliver_success`, around the awaited terminal transport edit; terminal requests do not wait on a progress lock |
| `Handover` | `lifecycle.py` constructor sharing `_card_writes` and calling `handover` |
| `Repair` | `lifecycle.py` `_CardWriteSequencer.queue_repair/_repair` |

`Mode="safe"` models the shared sequencer. Three unsafe modes each remove one
part of it: stale replacement retirement, successor edit tracking, or the
unconditional scheduling rule. The gate counterexample is the third review's
exact order: old replacement pending, successor edit pending, answer, successor
edit completes, old replacement completes without landing. The previous two
unsafe modes replay the earlier review sequences.

| Config | Verdict | Distinct states |
| --- | --- | ---: |
| `CardHandoverSafe` | clean | 1564 |
| `CardHandoverUnsafe` | violates `NoStaleReplacement` | 171 |
| `CardHandoverSuccessorUnsafe` | violates `SettledCards` | 154 |
| `CardHandoverGateUnsafe` | violates `RepairQueuedWhenNeeded` | 379 |

The clean result assumes all started network requests finish and that stale
replacement deletion or fallback stripping succeeds. It treats that cleanup
as one completion action; the real adapter has another await, so a stale card
may be briefly visible. The model checks bounded safety and the explicit
scheduling obligation. It does not prove live Discord delivery or permission
behavior. Python regressions replay the three counterexamples with the adapter
fakes; there is no live Discord contract trace yet.
