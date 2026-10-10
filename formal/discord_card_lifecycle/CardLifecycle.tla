--------------------------- MODULE CardLifecycle ---------------------------
EXTENDS Naturals, FiniteSets

CONSTANT Mode, AllowCrash
VARIABLES phase, card, extra, progress, terminal, outcome, ended, sealed,
          repair, replacement, owner, restart, oldWire
vars == <<phase, card, extra, progress, terminal, outcome, ended, sealed,
          repair, replacement, owner, restart, oldWire>>

\* One original card, one pending progress edit, and one replacement suffice
\* to expose all ordering failures. The on-wire edit survives process death.
Init == /\ phase = "running"
        /\ card = "working"
        /\ extra = "absent"
        /\ progress = FALSE
        /\ terminal = FALSE
        /\ outcome = "none"
        /\ ended = FALSE
        /\ sealed = FALSE
        /\ repair = FALSE
        /\ replacement = FALSE
        /\ owner = 1
        /\ restart = FALSE
        /\ oldWire = FALSE

IssueProgress == /\ phase = "running" /\ ~progress /\ ~terminal
                 /\ progress' = TRUE
                 /\ oldWire' = TRUE
                 /\ UNCHANGED <<phase, card, extra, terminal, outcome, ended, sealed,
                                 repair, replacement, owner, restart>>

\* A terminal request is issued without waiting in pre-#655 and #655.
EndTurn(k) == /\ phase = "running" /\ k \in {"answer", "failure"}
           /\ phase' = "ending"
           /\ ended' = TRUE
           /\ terminal' = (Mode # "queue" \/ ~(progress \/ replacement))
           /\ outcome' = k
           /\ UNCHANGED <<card, extra, progress, sealed, repair, replacement,
                           owner, restart, oldWire>>

IssueQueuedTerminal == /\ phase = "ending" /\ ~terminal
                       /\ ~progress /\ ~replacement
                       /\ terminal' = TRUE
                       /\ UNCHANGED <<phase, card, extra, progress, outcome, ended, sealed,
                                       repair, replacement, owner, restart, oldWire>>

ApplyTerminal == /\ terminal /\ phase = "ending"
                 /\ card' = outcome
                 /\ sealed' = TRUE
                 /\ phase' = "ended"
                 /\ terminal' = FALSE
                 /\ UNCHANGED <<extra, progress, outcome, ended, repair, replacement,
                                 owner, restart, oldWire>>

ApplyProgress == /\ progress
                 /\ progress' = FALSE
                 /\ oldWire' = FALSE
                 /\ card' = IF card = "deleted" THEN card ELSE "working"
                 /\ repair' = IF Mode = "655" /\ phase = "ended" /\ owner = 1
                              THEN TRUE ELSE repair
                 /\ UNCHANGED <<phase, extra, terminal, outcome, ended, sealed,
                                 replacement, owner, restart>>

Repair == /\ repair /\ ~progress /\ phase = "ended"
          /\ card' = outcome
          /\ repair' = FALSE
          /\ UNCHANGED <<phase, extra, progress, terminal, outcome, ended, sealed,
                          replacement, owner, restart, oldWire>>

\* Dead-session recovery hands the same message to a new lifecycle.
Handover == /\ phase = "ended" /\ card = "failure"
            /\ owner = 1 /\ ~restart
            /\ owner' = 2
            /\ phase' = "running"
            /\ ended' = FALSE
            /\ sealed' = FALSE
            /\ repair' = IF Mode = "655" THEN FALSE ELSE repair
            /\ UNCHANGED <<card, extra, progress, terminal, outcome,
                            replacement, restart, oldWire>>

\* Missing-card recovery can send a replacement while the old request waits.
DeleteCard == /\ phase = "running" /\ card = "working"
              /\ card' = "deleted"
              /\ UNCHANGED <<phase, extra, progress, terminal, outcome, ended, sealed,
                              repair, replacement, owner, restart, oldWire>>
StartReplacement == /\ card = "deleted" /\ ~replacement /\ extra = "absent"
                    /\ replacement' = TRUE
                    /\ UNCHANGED <<phase, card, extra, progress, terminal, outcome,
                                    ended, sealed, repair, owner, restart, oldWire>>
FinishReplacement == /\ replacement
                     /\ replacement' = FALSE
                     /\ extra' = IF ended /\ Mode = "queue" THEN "retired"
                                  ELSE "working"
                     /\ UNCHANGED <<phase, card, progress, terminal, outcome, ended,
                                     sealed, repair, owner, restart, oldWire>>

\* Boot orphan retirement edits the old card in existing code. A process-local
\* queue cannot know whether an old request will still land afterwards.
Crash == /\ AllowCrash /\ phase \in {"running", "ending"} /\ ~restart
         /\ phase' = "dead"
         /\ restart' = TRUE
         /\ ended' = TRUE
         /\ terminal' = FALSE
         /\ UNCHANGED <<card, extra, progress, outcome, sealed, repair, replacement,
                         owner, oldWire>>
RetireOrphan == /\ phase = "dead"
                /\ card' = "restarted"
                /\ sealed' = TRUE
                /\ phase' = "retired"
                /\ UNCHANGED <<extra, progress, terminal, outcome, ended, repair,
                                replacement, owner, restart, oldWire>>

DropOrphan == /\ phase = "dead"
              /\ phase' = "retired"
              /\ UNCHANGED <<card, extra, progress, terminal, outcome, ended,
                              sealed, repair, replacement, owner, restart, oldWire>>

Next == IssueProgress \/ (\E k \in {"answer", "failure"} : EndTurn(k))
        \/ IssueQueuedTerminal \/ ApplyTerminal
        \/ ApplyProgress \/ Repair \/ Handover \/ DeleteCard
        \/ StartReplacement \/ FinishReplacement \/ Crash \/ RetireOrphan \/ DropOrphan
Spec == Init /\ [][Next]_vars

\* Once a terminal render is applied, no later write may change its card.
TerminalStable == sealed => card \in {"answer", "failure", "restarted", "deleted"}
\* If a stale edit did land, a repair must at least be scheduled.
RepairScheduled == (sealed /\ card = "working" /\ ~progress) => repair
NoStaleReplacement == (ended /\ ~replacement) => extra # "working"
NoWorkingAfterRetirement == (phase = "retired") => card # "working"
=============================================================================
