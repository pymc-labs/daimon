------------------------------ MODULE Clawback ------------------------------
EXTENDS Naturals, TLC

CONSTANTS Refund, Dispute, NoEvent, UseCreditLock
Events == {Refund, Dispute}
Stages == {"ready", "locked", "read", "done"}

VARIABLES stage, observedTotal, clawedBack, lockOwner
vars == <<stage, observedTotal, clawedBack, lockOwner>>

Init ==
    /\ stage = [e \in Events |-> "ready"]
    /\ observedTotal = [e \in Events |-> 0]
    /\ clawedBack = 0
    /\ lockOwner = NoEvent

(* The original topup row is the common serialization point. *)
LockCredit(e) ==
    /\ UseCreditLock
    /\ e \in Events
    /\ stage[e] = "ready"
    /\ lockOwner = NoEvent
    /\ stage' = [stage EXCEPT ![e] = "locked"]
    /\ lockOwner' = e
    /\ UNCHANGED <<observedTotal, clawedBack>>

ReadCumulative(e) ==
    /\ e \in Events
    /\ IF UseCreditLock
          THEN stage[e] = "locked" /\ lockOwner = e
          ELSE stage[e] = "ready"
    /\ observedTotal' = [observedTotal EXCEPT ![e] = clawedBack]
    /\ stage' = [stage EXCEPT ![e] = "read"]
    /\ UNCHANGED <<clawedBack, lockOwner>>

(* Both callbacks imply a full reversal of one $1 original credit. The
   transaction inserts only the target minus its previously read total. *)
CommitClawback(e) ==
    /\ e \in Events
    /\ stage[e] = "read"
    /\ stage' = [stage EXCEPT ![e] = "done"]
    /\ clawedBack' = clawedBack + (IF observedTotal[e] < 1 THEN 1 - observedTotal[e] ELSE 0)
    /\ lockOwner' = IF UseCreditLock THEN NoEvent ELSE lockOwner
    /\ UNCHANGED observedTotal

Next ==
    \/ \E e \in Events : LockCredit(e)
    \/ \E e \in Events : ReadCumulative(e)
    \/ \E e \in Events : CommitClawback(e)

TypeOK ==
    /\ stage \in [Events -> Stages]
    /\ observedTotal \in [Events -> 0..2]
    /\ clawedBack \in 0..2
    /\ lockOwner \in Events \cup {NoEvent}
NeverOverClawback == clawedBack <= 1
Finished == \A e \in Events : stage[e] = "done"

Spec == Init /\ [][Next]_vars
FairSpec ==
    /\ Spec
    /\ \A e \in Events : WF_vars(LockCredit(e))
    /\ \A e \in Events : WF_vars(ReadCumulative(e))
    /\ \A e \in Events : WF_vars(CommitClawback(e))
EventuallyFinished == <>Finished
=============================================================================
