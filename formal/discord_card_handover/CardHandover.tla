--------------------------- MODULE CardHandover ---------------------------
EXTENDS Naturals, FiniteSets

CONSTANT Mode
VARIABLES phase, handovers, current, cards, edits, started, targets,
          sendPending, sendUsed, terminalIssued, dirty, repairQueued
vars == <<phase, handovers, current, cards, edits, started, targets,
          sendPending, sendUsed, terminalIssued, dirty, repairQueued>>
Lifecycles == {1, 2, 3}
Cards == {1, 2, 3}

Init == /\ phase = "old"
        /\ handovers = 0
        /\ current = 1
        /\ cards = [i \in Cards |-> IF i = 1 THEN "working" ELSE "absent"]
        /\ edits = {}
        /\ started = {}
        /\ targets = [i \in Lifecycles |-> 1]
        /\ sendPending = FALSE
        /\ sendUsed = FALSE
        /\ terminalIssued = FALSE
        /\ dirty = FALSE
        /\ repairQueued = FALSE

DeleteOriginal == /\ phase = "old" /\ cards[1] = "working"
                  /\ cards' = [cards EXCEPT ![1] = "deleted"]
                  /\ UNCHANGED <<phase, handovers, current, edits, started, targets,
                                  sendPending, sendUsed, terminalIssued, dirty, repairQueued>>

StartEdit(l) == /\ l = handovers + 1
                /\ phase \in {"old", "recovering"}
                /\ l \notin started
                /\ cards[current] \in {"working", "failure"}
                /\ edits' = edits \cup {l}
                /\ started' = started \cup {l}
                /\ targets' = [targets EXCEPT ![l] = current]
                /\ UNCHANGED <<phase, handovers, current, cards, sendPending,
                                sendUsed, terminalIssued, dirty, repairQueued>>

StartSend == /\ phase = "old" /\ cards[1] = "deleted" /\ ~sendUsed
             /\ sendPending' = TRUE
             /\ sendUsed' = TRUE
             /\ UNCHANGED <<phase, handovers, current, cards, edits, started,
                             targets, terminalIssued, dirty, repairQueued>>

Failure == /\ phase \in {"old", "recovering"}
           /\ phase' = "failing"
           /\ current' = IF cards[current] = "deleted" THEN 3 ELSE current
           /\ terminalIssued' = TRUE
           /\ UNCHANGED <<handovers, cards, edits, started, targets, sendPending,
                           sendUsed, dirty, repairQueued>>

CompleteFailure == /\ phase = "failing"
                   /\ phase' = "failed"
                   /\ cards' = [cards EXCEPT ![current] = "failure"]
                   /\ UNCHANGED <<handovers, current, edits, started, targets,
                                   sendPending, sendUsed, terminalIssued, dirty, repairQueued>>

Handover == /\ phase = "failed" /\ handovers < 2
            /\ phase' = "recovering"
            /\ handovers' = handovers + 1
            /\ terminalIssued' = FALSE
            /\ dirty' = FALSE
            /\ repairQueued' = FALSE
            /\ UNCHANGED <<current, cards, edits, started, targets,
                            sendPending, sendUsed>>

Answer == /\ phase = "recovering"
          /\ phase' = "answering"
          /\ terminalIssued' = TRUE
          /\ dirty' = \E l \in edits : targets[l] = current
                                     /\ (Mode # "successor" \/ l = 1)
          /\ UNCHANGED <<handovers, current, cards, edits, started, targets,
                          sendPending, sendUsed, repairQueued>>

CompleteAnswer == /\ phase = "answering"
                  /\ phase' = "ended"
                  /\ cards' = [cards EXCEPT ![current] = "answer"]
                  /\ repairQueued' = (repairQueued \/
                       (dirty /\ edits = {} /\ ~sendPending))
                  /\ UNCHANGED <<handovers, current, edits, started, targets,
                                  sendPending, sendUsed, terminalIssued, dirty>>

CompleteEdit(l) == /\ l \in edits
                   /\ edits' = edits \ {l}
                   /\ cards' = IF cards[targets[l]] = "deleted" THEN cards
                               ELSE [cards EXCEPT ![targets[l]] = "working"]
                   /\ dirty' = (dirty \/ (phase \in {"answering", "ended"}
                                        /\ targets[l] = current
                                        /\ Mode # "successor"))
                   /\ repairQueued' = (repairQueued \/
                        (phase = "ended" /\ ~sendPending /\ edits = {l}
                         /\ (dirty \/ (targets[l] = current /\ Mode # "successor"))))
                   /\ UNCHANGED <<phase, handovers, current, started, targets,
                                   sendPending, sendUsed, terminalIssued>>

CompleteSend == /\ sendPending
                /\ sendPending' = FALSE
                /\ cards' = [cards EXCEPT ![2] = IF phase = "old" \/ Mode = "stale"
                                                  THEN "working" ELSE "retired"]
                /\ current' = IF phase = "old" THEN 2 ELSE current
                \* The old send's completion did not land on the current card.
                \* The rejected gate therefore fails to queue an existing repair.
                /\ repairQueued' = (repairQueued \/
                     (phase = "ended" /\ dirty /\ Cardinality(edits) = 0 /\ Mode # "gate"))
                /\ UNCHANGED <<phase, handovers, edits, started, targets,
                                sendUsed, terminalIssued, dirty>>

Repair == /\ phase = "ended" /\ repairQueued
          /\ cards' = [cards EXCEPT ![current] = "answer"]
          /\ repairQueued' = FALSE
          /\ dirty' = FALSE
          /\ UNCHANGED <<phase, handovers, current, edits, started, targets,
                          sendPending, sendUsed, terminalIssued>>

Next == DeleteOriginal \/ (\E l \in Lifecycles : StartEdit(l)) \/ StartSend
        \/ Failure \/ CompleteFailure \/ Handover \/ Answer \/ CompleteAnswer
        \/ (\E l \in Lifecycles : CompleteEdit(l)) \/ CompleteSend \/ Repair
Spec == Init /\ [][Next]_vars

NoStaleReplacement ==
    (phase = "ended" /\ current # 2 /\ ~sendPending) => cards[2] # "working"
RepairQueuedWhenNeeded ==
    ~(phase = "ended" /\ dirty /\ edits = {} /\ ~sendPending /\ ~repairQueued)
SettledCards ==
    (phase = "ended" /\ Cardinality(edits) = 0 /\ ~sendPending /\ ~repairQueued) =>
        (\A i \in Cards : cards[i] \in {"absent", "deleted", "retired", "failure", "answer"})
=============================================================================
