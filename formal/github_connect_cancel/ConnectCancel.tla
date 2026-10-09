---------------------- MODULE ConnectCancel ----------------------
EXTENDS TLC

CONSTANTS UnsafeCancel, UnsafeSweep, UnsafeSweepStaleSelection, UnsafeSiblingClear

VARIABLES phase, token, revoke, expired, deleted, cancelSeen, cancelStarted,
          sweepSelected, siblingDone
vars == <<phase, token, revoke, expired, deleted, cancelSeen, cancelStarted,
          sweepSelected, siblingDone>>

Init ==
    /\ phase = "ready"
    /\ token = "saved"
    /\ revoke = "none"
    /\ expired = FALSE
    /\ deleted = FALSE
    /\ cancelSeen = FALSE
    /\ cancelStarted = FALSE
    /\ sweepSelected = FALSE
    /\ siblingDone = FALSE

\* cancel_flow acquires the row lock before expiry, then may commit after it.
CancelStart ==
    /\ phase = "ready"
    /\ ~expired
    /\ ~deleted
    /\ token = "saved"
    /\ ~cancelStarted
    /\ ~cancelSeen
    /\ cancelStarted' = TRUE
    /\ UNCHANGED <<phase, token, revoke, expired, deleted, cancelSeen,
                   sweepSelected, siblingDone>>

CancelCommit ==
    /\ cancelStarted
    /\ phase' = IF UnsafeCancel THEN "ready" ELSE "cancelled"
    /\ cancelSeen' = TRUE
    /\ cancelStarted' = FALSE
    /\ UNCHANGED <<token, revoke, expired, deleted, sweepSelected, siblingDone>>

Confirm ==
    /\ phase = "ready"
    /\ ~expired
    /\ ~deleted
    /\ ~cancelStarted
    /\ phase' = "confirmed"
    /\ token' = "cleared"
    /\ UNCHANGED <<revoke, expired, deleted, cancelSeen, cancelStarted,
                   sweepSelected, siblingDone>>

\* Another browser can confirm the invitation after this flow is canceled.
SiblingConfirm ==
    /\ phase = "cancelled"
    /\ ~siblingDone
    /\ siblingDone' = TRUE
    /\ token' = IF UnsafeSiblingClear THEN "cleared" ELSE token
    /\ UNCHANGED <<phase, revoke, expired, deleted, cancelSeen, cancelStarted,
                   sweepSelected>>

RevokeFails ==
    /\ phase = "cancelled"
    /\ token = "saved"
    /\ revoke = "none"
    /\ revoke' = "failed"
    /\ UNCHANGED <<phase, token, expired, deleted, cancelSeen, cancelStarted,
                   sweepSelected, siblingDone>>

RevokeSucceeds ==
    /\ phase = "cancelled"
    /\ token = "saved"
    /\ revoke' = "ok"
    /\ token' = "cleared"
    /\ UNCHANGED <<phase, expired, deleted, cancelSeen, cancelStarted,
                   sweepSelected, siblingDone>>

Expire ==
    /\ ~expired
    /\ expired' = TRUE
    /\ UNCHANGED <<phase, token, revoke, deleted, cancelSeen, cancelStarted,
                   sweepSelected, siblingDone>>

\* PostgreSQL may select the subquery candidate before a row-lock wait. The
\* outer DELETE rechecks eligibility against the updated tuple after the wait.
SweepSelect ==
    /\ expired
    /\ ~deleted
    /\ ~sweepSelected
    /\ (UnsafeSweep \/ phase # "cancelled" \/ token = "cleared")
    /\ sweepSelected' = TRUE
    /\ UNCHANGED <<phase, token, revoke, expired, deleted, cancelSeen,
                   cancelStarted, siblingDone>>

SweepDelete ==
    /\ sweepSelected
    /\ ~deleted
    /\ ~cancelStarted
    /\ (UnsafeSweep \/ UnsafeSweepStaleSelection \/
        phase # "cancelled" \/ token = "cleared")
    /\ deleted' = TRUE
    /\ UNCHANGED <<phase, token, revoke, expired, cancelSeen, cancelStarted,
                   sweepSelected, siblingDone>>

Next == CancelStart \/ CancelCommit \/ Confirm \/ SiblingConfirm \/
        RevokeFails \/ RevokeSucceeds \/ Expire \/ SweepSelect \/ SweepDelete
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ phase \in {"ready", "cancelled", "confirmed"}
    /\ token \in {"saved", "cleared"}
    /\ revoke \in {"none", "failed", "ok"}
    /\ expired \in BOOLEAN
    /\ deleted \in BOOLEAN
    /\ cancelSeen \in BOOLEAN
    /\ cancelStarted \in BOOLEAN
    /\ sweepSelected \in BOOLEAN
    /\ siblingDone \in BOOLEAN

NoConfirmAfterCancel == cancelSeen => phase # "confirmed"
PendingTokenRetained == (cancelSeen /\ revoke # "ok") => (token = "saved" /\ ~deleted)
===============================================================
