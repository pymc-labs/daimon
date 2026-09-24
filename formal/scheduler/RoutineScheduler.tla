-------------------------- MODULE RoutineScheduler --------------------------
EXTENDS Naturals, FiniteSets, TLC

(***************************************************************************)
(* One routine, two possible scheduler processes, and two recurrence slots.  *)
(* Slot is the current scheduled occurrence; Dispatches counts attempts for  *)
(* that occurrence.  Claim is an atomic abstraction of the SQL claim plus    *)
(* next-slot recomputation transaction.                                     *)
(***************************************************************************)

CONSTANTS P1, P2, NoProcess, NoResult, Success, Error, Due, Pending, Done,
          Future, Null
Processes == {P1, P2}
Statuses == {Due, Pending, Done}
FireResults == {NoResult, Success, Error}
FireTimes == {Due, Future, Null}

VARIABLES lockOwner, status, nextFire, claimedBy, dispatches, result, slot
vars == <<lockOwner, status, nextFire, claimedBy, dispatches, result, slot>>

Init ==
    /\ lockOwner = NoProcess
    /\ status = Due
    /\ nextFire = Due
    /\ claimedBy = NoProcess
    /\ dispatches = 0
    /\ result = NoResult
    /\ slot = 1

Acquire(p) ==
    /\ p \in Processes
    /\ lockOwner = NoProcess
    /\ lockOwner' = p
    /\ UNCHANGED <<status, nextFire, claimedBy, dispatches, result, slot>>

Release(p) ==
    /\ lockOwner = p
    /\ status # Pending
    /\ lockOwner' = NoProcess
    /\ UNCHANGED <<status, nextFire, claimedBy, dispatches, result, slot>>

Claim(p, recomputeOk) ==
    /\ lockOwner = p
    /\ status = Due
    /\ nextFire = Due
    /\ status' = Pending
    /\ nextFire' = IF recomputeOk THEN Future ELSE Null
    /\ claimedBy' = p
    /\ dispatches' = dispatches
    /\ result' = NoResult
    /\ UNCHANGED <<lockOwner, slot>>

DispatchSuccess(p) ==
    /\ lockOwner = p
    /\ claimedBy = p
    /\ status = Pending
    /\ dispatches = 0
    /\ status' = Done
    /\ dispatches' = 1
    /\ result' = Success
    /\ UNCHANGED <<lockOwner, nextFire, claimedBy, slot>>

DispatchError(p) ==
    /\ lockOwner = p
    /\ claimedBy = p
    /\ status = Pending
    /\ dispatches = 0
    /\ status' = Done
    /\ dispatches' = 1
    /\ result' = Error
    /\ UNCHANGED <<lockOwner, nextFire, claimedBy, slot>>

(* A failed write to last_error is swallowed by the scheduler's error boundary.
   The attempt is still terminal for this occurrence and must not be redelivered. *)
DispatchErrorRecordLost(p) ==
    /\ lockOwner = p
    /\ claimedBy = p
    /\ status = Pending
    /\ dispatches = 0
    /\ status' = Done
    /\ dispatches' = 1
    /\ result' = NoResult
    /\ UNCHANGED <<lockOwner, nextFire, claimedBy, slot>>

RecoverNull(p) ==
    /\ lockOwner = p
    /\ status = Done
    /\ nextFire = Null
    /\ nextFire' = Future
    /\ UNCHANGED <<lockOwner, status, claimedBy, dispatches, result, slot>>

NextOccurrence ==
    /\ slot < 2
    /\ status = Done
    /\ status' = Due
    /\ nextFire' = Due
    /\ claimedBy' = NoProcess
    /\ dispatches' = 0
    /\ result' = NoResult
    /\ slot' = slot + 1
    /\ UNCHANGED lockOwner

Next ==
    \/ \E p \in Processes : Acquire(p)
    \/ \E p \in Processes : Release(p)
    \/ \E p \in Processes : \E ok \in BOOLEAN : Claim(p, ok)
    \/ \E p \in Processes : DispatchSuccess(p)
    \/ \E p \in Processes : DispatchError(p)
    \/ \E p \in Processes : DispatchErrorRecordLost(p)
    \/ \E p \in Processes : RecoverNull(p)
    \/ NextOccurrence

AcquireAny == \E p \in Processes : Acquire(p)
ReleaseAny == \E p \in Processes : Release(p)
ClaimAny == \E p \in Processes : \E ok \in BOOLEAN : Claim(p, ok)
DispatchAny ==
    \/ \E p \in Processes : DispatchSuccess(p)
    \/ \E p \in Processes : DispatchError(p)
    \/ \E p \in Processes : DispatchErrorRecordLost(p)

TypeOK ==
    /\ lockOwner \in Processes \cup {NoProcess}
    /\ status \in Statuses
    /\ nextFire \in FireTimes
    /\ claimedBy \in Processes \cup {NoProcess}
    /\ dispatches \in 0..1
    /\ result \in FireResults
    /\ slot \in 1..2

AtMostOneDispatchPerOccurrence == dispatches <= 1
OnlyClaimedWorkCanComplete == status = Done => claimedBy \in Processes
TerminalResultConsistent == result \in {Success, Error} => status = Done
ClaimRequiresSchedulerLock == status = Pending => claimedBy = lockOwner

Spec == Init /\ [][Next]_vars
ProgressSpec ==
    /\ Spec
    /\ WF_vars(AcquireAny)
    /\ WF_vars(ReleaseAny)
    /\ WF_vars(ClaimAny)
    /\ WF_vars(DispatchAny)
    /\ WF_vars(NextOccurrence)

THEOREM Spec => []TypeOK
THEOREM Spec => []AtMostOneDispatchPerOccurrence
THEOREM Spec => []OnlyClaimedWorkCanComplete
THEOREM Spec => []TerminalResultConsistent
PendingEventuallyCompletes == status = Pending ~> status = Done
FirstOccurrenceAdvances == (slot = 1 /\ status = Done) ~> (slot = 2)
=============================================================================
