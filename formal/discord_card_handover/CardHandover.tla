--------------------------- MODULE CardHandover ---------------------------
EXTENDS Naturals

CONSTANT SafeHandover
VARIABLES phase, pending, visible, repairNeeded, transferred
vars == <<phase, pending, visible, repairNeeded, transferred>>

Init == /\ phase = "old"
        /\ pending = TRUE
        /\ visible = "working"
        /\ repairNeeded = FALSE
        /\ transferred = FALSE

OldFailure == /\ phase = "old"
              /\ phase' = "failed"
              /\ visible' = "failure"
              /\ UNCHANGED <<pending, repairNeeded, transferred>>

Handover == /\ phase = "failed"
            /\ phase' = "handover"
            /\ transferred' = SafeHandover
            /\ UNCHANGED <<pending, visible, repairNeeded>>

Answer == /\ phase = "handover"
          /\ phase' = "answered"
          /\ visible' = "answer"
          /\ UNCHANGED <<pending, repairNeeded, transferred>>

CompleteOldEdit == /\ pending
                   /\ phase \in {"failed", "handover", "answered"}
                   /\ pending' = FALSE
                   /\ visible' = "working"
                   /\ repairNeeded' = (phase = "answered" /\ transferred)
                   /\ UNCHANGED <<phase, transferred>>

Repair == /\ phase = "answered"
          /\ repairNeeded
          /\ visible' = "answer"
          /\ repairNeeded' = FALSE
          /\ UNCHANGED <<phase, pending, transferred>>

Next == OldFailure \/ Handover \/ Answer \/ CompleteOldEdit \/ Repair
Spec == Init /\ [][Next]_vars

RepairOwned == (phase = "answered" /\ visible = "working") => repairNeeded
=============================================================================
