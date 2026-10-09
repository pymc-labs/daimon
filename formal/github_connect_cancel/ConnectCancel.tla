---------------------- MODULE ConnectCancel ----------------------
EXTENDS TLC

CONSTANTS UnsafeCancel, UnsafeSweep, UnsafeSiblingClear

VARIABLES phase, token, revoke, expired, deleted, cancelSeen, siblingDone
vars == <<phase, token, revoke, expired, deleted, cancelSeen, siblingDone>>

Init ==
    /\ phase = "ready"
    /\ token = "saved"
    /\ revoke = "none"
    /\ expired = FALSE
    /\ deleted = FALSE
    /\ cancelSeen = FALSE
    /\ siblingDone = FALSE

\* cancel_flow commits under the same flow-row lock as confirm.
Cancel ==
    /\ phase = "ready"
    /\ ~expired
    /\ ~deleted
    /\ token = "saved"
    /\ phase' = IF UnsafeCancel THEN "ready" ELSE "cancelled"
    /\ cancelSeen' = TRUE
    /\ UNCHANGED <<token, revoke, expired, deleted, siblingDone>>

Confirm ==
    /\ phase = "ready"
    /\ ~expired
    /\ ~deleted
    /\ phase' = "confirmed"
    /\ token' = "cleared"
    /\ UNCHANGED <<revoke, expired, deleted, cancelSeen, siblingDone>>

\* Another browser can confirm the invitation after this flow is canceled.
SiblingConfirm ==
    /\ phase = "cancelled"
    /\ ~siblingDone
    /\ siblingDone' = TRUE
    /\ token' = IF UnsafeSiblingClear THEN "cleared" ELSE token
    /\ UNCHANGED <<phase, revoke, expired, deleted, cancelSeen>>

RevokeFails ==
    /\ phase = "cancelled"
    /\ token = "saved"
    /\ revoke = "none"
    /\ revoke' = "failed"
    /\ UNCHANGED <<phase, token, expired, deleted, cancelSeen, siblingDone>>

RevokeSucceeds ==
    /\ phase = "cancelled"
    /\ token = "saved"
    /\ revoke' = "ok"
    /\ token' = "cleared"
    /\ UNCHANGED <<phase, expired, deleted, cancelSeen, siblingDone>>

Expire ==
    /\ ~expired
    /\ expired' = TRUE
    /\ UNCHANGED <<phase, token, revoke, deleted, cancelSeen, siblingDone>>

Sweep ==
    /\ expired
    /\ ~deleted
    /\ (UnsafeSweep \/ phase # "cancelled" \/ token = "cleared")
    /\ deleted' = TRUE
    /\ UNCHANGED <<phase, token, revoke, expired, cancelSeen, siblingDone>>

Next == Cancel \/ Confirm \/ SiblingConfirm \/ RevokeFails \/ RevokeSucceeds \/ Expire \/ Sweep
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ phase \in {"ready", "cancelled", "confirmed"}
    /\ token \in {"saved", "cleared"}
    /\ revoke \in {"none", "failed", "ok"}
    /\ expired \in BOOLEAN
    /\ deleted \in BOOLEAN
    /\ cancelSeen \in BOOLEAN
    /\ siblingDone \in BOOLEAN

NoConfirmAfterCancel == cancelSeen => phase # "confirmed"
PendingTokenRetained == (cancelSeen /\ revoke # "ok") => (token = "saved" /\ ~deleted)
===============================================================
