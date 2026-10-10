# Discord card recovery handover

Question: can a progress replacement send finish after the failed turn's card
has been replaced and a successor has answered, leaving a second Working card
with a Stop button? `NoStaleReplacement` forbids that result. The unsafe config
replays the reviewed sequence; the safe config retires the late replacement.
It also retains `RepairOwned` for an old edit that completes after the answer.

| Model action | Code boundary |
| --- | --- |
| `DeleteCard`, `StartReplacement` | `lifecycle.py:365-439` (`_edit_message`), missing-card branch and transport replacement |
| `CompleteReplacement` | `lifecycle.py:439` (`_reconcile_late_replacement`), after the awaited send or transport edit returns |
| `StartEdit`, `CompleteEdit` | `lifecycle.py:479,541`, around the awaited transport edit |
| `TerminalFailure` | `lifecycle.py:623,929`, terminal flush during failure |
| `Handover`, `RecoverFailure` | `lifecycle.py:230,290`, transfer of pending progress; a second recovery is allowed |
| `Answer` | `lifecycle.py:709`, after the successor's answer edit |
| `Repair` | `lifecycle.py:583`, after an old edit lands |

The model has three message slots: original, old progress replacement, and
terminal replacement. It permits two handovers, one outstanding replacement
and one outstanding edit, with its target recorded. The missing-card send is
serialized by the shared lock in `lifecycle.py:229,421`; a concurrent caller
rechecks the card reference after acquiring it. Transport-returned replacements
can overlap; the code retires any return whose original card is no longer
current. The model checks one of those returns at a time. The fixed completion retires an unowned replacement;
otherwise it becomes the live card. The model treats network completion and
successful deletion as one reconciliation step. In the real adapter they are
two awaits, so a stale card can be briefly visible between them. Deletion can
also fail; the code then tries to strip the Stop control and show the terminal
embed. The clean result assumes either deletion or fallback editing succeeds.
Discord's timing and permission outcomes have no live contract trace here.

| Config | Verdict | Distinct states |
| --- | --- | ---: |
| `CardHandoverSafe` | clean | 201 |
| `CardHandoverUnsafe` | violates `NoStaleReplacement` | 65 |

Counterexample: the original card is deleted; a progress replacement starts;
the failure posts a terminal replacement; recovery answers on that card; then
the old progress send returns. Without reconciliation, the old send's Working
card remains beside the answer. The Python regression replays both a missing
card send and a transport-returned replacement. These checks are bounded and
provisional for live Discord behavior.
