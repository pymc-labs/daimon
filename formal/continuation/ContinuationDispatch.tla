---------------------- MODULE ContinuationDispatch ----------------------
EXTENDS Naturals, TLC

(***************************************************************************)
(* One durable continuation and one adapter process. Claim is committed    *)
(* before dispatch; the platform/MA turn effect and status settlement are  *)
(* separate effects. A crash ends the in-memory attempt, while Restart    *)
(* starts a fresh process that only queries pending rows.                  *)
(***************************************************************************)

CONSTANTS Pending, Claimed, Delivered, Skipped, EnableRecovery, EnableCrash
Statuses == {Pending, Claimed, Delivered, Skipped}
Terminal == {Delivered, Skipped}

VARIABLES status, processAlive, attemptActive, effectForClaim, effectCount, attempts
vars == <<status, processAlive, attemptActive, effectForClaim, effectCount, attempts>>

Init ==
    /\ status = Pending
    /\ processAlive = TRUE
    /\ attemptActive = FALSE
    /\ effectForClaim = FALSE
    /\ effectCount = 0
    /\ attempts = 0

Claim ==
    /\ processAlive
    /\ status = Pending
    /\ status' = Claimed
    /\ attempts' = attempts + 1
    /\ attemptActive' = TRUE
    /\ effectForClaim' = FALSE
    /\ UNCHANGED <<processAlive, effectCount>>

ExternalEffect ==
    /\ processAlive
    /\ attemptActive
    /\ status = Claimed
    /\ ~effectForClaim
    /\ effectForClaim' = TRUE
    /\ effectCount' = effectCount + 1
    /\ UNCHANGED <<status, processAlive, attemptActive, attempts>>

SettleDelivered ==
    /\ processAlive
    /\ attemptActive
    /\ status = Claimed
    /\ effectForClaim
    /\ status' = Delivered
    /\ UNCHANGED <<processAlive, attemptActive, effectForClaim, effectCount, attempts>>

SettleSkipped ==
    /\ processAlive
    /\ attemptActive
    /\ status = Claimed
    /\ ~effectForClaim
    /\ status' = Skipped
    /\ UNCHANGED <<processAlive, attemptActive, effectForClaim, effectCount, attempts>>

Crash ==
    /\ EnableCrash
    /\ processAlive
    /\ status = Claimed
    /\ processAlive' = FALSE
    /\ attemptActive' = FALSE
    /\ UNCHANGED <<status, effectForClaim, effectCount, attempts>>

Restart ==
    /\ ~processAlive
    /\ processAlive' = TRUE
    /\ UNCHANGED <<status, attemptActive, effectForClaim, effectCount, attempts>>

(* Hypothetical stale-claim retry. The current implementation has no such
   transition. A retry cannot distinguish pre-effect death from a lost
   settlement after the external effect. *)
RecoverClaimed ==
    /\ EnableRecovery
    /\ processAlive
    /\ ~attemptActive
    /\ status = Claimed
    /\ status' = Pending
    /\ UNCHANGED <<processAlive, attemptActive, effectForClaim, effectCount, attempts>>

Next ==
    \/ Claim
    \/ ExternalEffect
    \/ SettleDelivered
    \/ SettleSkipped
    \/ Crash
    \/ Restart
    \/ RecoverClaimed

TypeOK ==
    /\ status \in Statuses
    /\ processAlive \in BOOLEAN
    /\ attemptActive \in BOOLEAN
    /\ effectForClaim \in BOOLEAN
    /\ effectCount \in 0..2
    /\ attempts \in 0..2

AtMostOneClaim == attempts <= 1
TerminalHasExternalEffect == status = Delivered => effectCount > 0
AtMostOneExternalEffect == effectCount <= 1

Spec == Init /\ [][Next]_vars
CrashFreeProgressSpec ==
    /\ Spec
    /\ WF_vars(Claim)
    /\ WF_vars(ExternalEffect)
    /\ WF_vars(SettleDelivered)
    /\ WF_vars(SettleSkipped)

PendingEventuallySettles == status = Pending ~> status \in Terminal

THEOREM Spec => []TypeOK
THEOREM Spec => []TerminalHasExternalEffect
=============================================================================
