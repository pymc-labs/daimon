----------------------------- MODULE RepoEditor -----------------------------
EXTENDS Integers, TLC

CONSTANTS UnsafeSnapshot, UnsafeReplay
Browsers == {1, 2}
VARIABLES revision, rendered, phase, lock, used, writes, fresh, chatDone
vars == <<revision, rendered, phase, lock, used, writes, fresh, chatDone>>

Init == /\ revision = 0
        /\ rendered = -1
        /\ phase = [b \in Browsers |-> "idle"]
        /\ lock = 0
        /\ used = FALSE
        /\ writes = 0
        /\ fresh = TRUE
        /\ chatDone = FALSE

Render == /\ rendered = -1
          /\ rendered' = revision
          /\ UNCHANGED <<revision, phase, lock, used, writes, fresh, chatDone>>

\* Both POSTs carry the same browser form. The transaction locks the flow,
\* invitation and existing grants before it checks the signed snapshot.
Begin(b) == /\ rendered # -1
            /\ phase[b] = "idle"
            /\ lock = 0
            /\ phase' = [phase EXCEPT ![b] = "locked"]
            /\ lock' = b
            /\ UNCHANGED <<revision, rendered, used, writes, fresh, chatDone>>

Check(b) == /\ lock = b
            /\ phase[b] = "locked"
            /\ phase' = [phase EXCEPT ![b] =
                 IF (UnsafeReplay \/ ~used) /\
                    (UnsafeSnapshot \/ revision = rendered)
                 THEN "ready" ELSE "rejected"]
            /\ UNCHANGED <<revision, rendered, lock, used, writes, fresh, chatDone>>

Commit(b) == /\ lock = b
             /\ phase[b] = "ready"
             /\ revision' = revision + 1
             /\ used' = TRUE
             /\ writes' = writes + 1
             /\ fresh' = (fresh /\ revision = rendered)
             /\ phase' = [phase EXCEPT ![b] = "done"]
             /\ lock' = 0
             /\ UNCHANGED <<rendered, chatDone>>

Rollback(b) == /\ lock = b
               /\ phase[b] \in {"ready", "rejected"}
               /\ phase' = [phase EXCEPT ![b] = "done"]
               /\ lock' = 0
               /\ UNCHANGED <<revision, rendered, used, writes, fresh, chatDone>>

\* A chat change to an EXISTING grant takes the same row lock. New-grant
\* insertions and authorization changes are outside this finite model.
ChatChange == /\ ~chatDone
              /\ lock = 0
              /\ revision' = revision + 1
              /\ chatDone' = TRUE
              /\ UNCHANGED <<rendered, phase, lock, used, writes, fresh>>

Next == Render \/ ChatChange \/
        (\E b \in Browsers: Begin(b) \/ Check(b) \/ Commit(b) \/ Rollback(b))
Spec == Init /\ [][Next]_vars
TypeOK == /\ revision \in 0..3
          /\ rendered \in -1..1
          /\ lock \in {0, 1, 2}
          /\ phase \in [Browsers -> {"idle", "locked", "ready", "rejected", "done"}]
          /\ used \in BOOLEAN /\ fresh \in BOOLEAN /\ chatDone \in BOOLEAN
          /\ writes \in 0..2
AtMostOneSave == writes <= 1
NoStaleSave == fresh
=============================================================================
