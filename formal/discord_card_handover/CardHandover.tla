--------------------------- MODULE CardHandover ---------------------------
EXTENDS Naturals

CONSTANT SafeHandover
VARIABLES phase, handovers, current, cards, editPending, editTarget, sendPending, repairNeeded
vars == <<phase, handovers, current, cards, editPending, editTarget, sendPending, repairNeeded>>
Cards == {1, 2, 3}

Init == /\ phase = "old"
        /\ handovers = 0
        /\ current = 1
        /\ cards = [i \in Cards |-> IF i = 1 THEN "working" ELSE "absent"]
        /\ editPending = FALSE
        /\ editTarget = 0
        /\ sendPending = FALSE
        /\ repairNeeded = FALSE

DeleteCard == /\ phase = "old" /\ cards[1] = "working"
              /\ cards' = [cards EXCEPT ![1] = "deleted"]
              /\ UNCHANGED <<phase, handovers, current, editPending, editTarget, sendPending, repairNeeded>>

StartEdit == /\ phase = "old" /\ ~editPending /\ cards[current] = "working"
             /\ editPending' = TRUE
             /\ editTarget' = current
             /\ UNCHANGED <<phase, handovers, current, cards, sendPending, repairNeeded>>

StartReplacement == /\ phase = "old" /\ cards[1] = "deleted" /\ ~sendPending
                    /\ sendPending' = TRUE
                    /\ UNCHANGED <<phase, handovers, current, cards, editPending, editTarget, repairNeeded>>

TerminalFailure == /\ phase = "old"
                   /\ phase' = "failed"
                   /\ current' = IF cards[current] = "deleted" THEN 3 ELSE current
                   /\ cards' = [cards EXCEPT ![IF cards[current] = "deleted" THEN 3 ELSE current] = "failure"]
                   /\ UNCHANGED <<handovers, editPending, editTarget, sendPending, repairNeeded>>

Handover == /\ phase = "failed" /\ handovers < 2
            /\ phase' = "recovering"
            /\ handovers' = handovers + 1
            /\ UNCHANGED <<current, cards, editPending, editTarget, sendPending, repairNeeded>>

RecoverFailure == /\ phase = "recovering" /\ handovers < 2
                  /\ phase' = "failed"
                  /\ UNCHANGED <<handovers, current, cards, editPending, editTarget, sendPending, repairNeeded>>

Answer == /\ phase = "recovering"
          /\ phase' = "answered"
          /\ cards' = [cards EXCEPT ![current] = "answer"]
          /\ UNCHANGED <<handovers, current, editPending, editTarget, sendPending, repairNeeded>>

CompleteReplacement == /\ sendPending
                       /\ sendPending' = FALSE
                       /\ cards' = [cards EXCEPT ![2] = IF phase = "old" THEN "working"
                                                         ELSE IF SafeHandover THEN "deleted" ELSE "working"]
                       /\ current' = IF phase = "old" THEN 2 ELSE current
                       /\ UNCHANGED <<phase, handovers, editPending, editTarget, repairNeeded>>

CompleteEdit == /\ editPending
                /\ editPending' = FALSE
                /\ cards' = [cards EXCEPT ![editTarget] = IF (@ = "deleted") THEN @ ELSE "working"]
                /\ repairNeeded' = (phase = "answered" /\ SafeHandover
                                     /\ editTarget = current /\ cards[editTarget] # "deleted")
                /\ UNCHANGED <<phase, handovers, current, editTarget, sendPending>>

Repair == /\ phase = "answered" /\ repairNeeded
          /\ cards' = [cards EXCEPT ![current] = "answer"]
          /\ repairNeeded' = FALSE
          /\ UNCHANGED <<phase, handovers, current, editPending, editTarget, sendPending>>

Next == DeleteCard \/ StartEdit \/ StartReplacement \/ TerminalFailure \/ Handover
        \/ RecoverFailure \/ Answer \/ CompleteReplacement \/ CompleteEdit \/ Repair
Spec == Init /\ [][Next]_vars

NoStaleReplacement == (phase = "answered" /\ current # 2) => cards[2] # "working"
RepairOwned == (phase = "answered" /\ cards[current] = "working") => repairNeeded
=============================================================================
