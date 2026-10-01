----------------------------- MODULE NotebookUpload -----------------------------
EXTENDS Naturals, TLC

CONSTANTS AllowCrash, BurnBeforeBody

Attempts == {"first", "second", "retry"}
Phases == {"waiting", "new", "checked", "burned", "succeeded", "failed", "rejected", "crashed"}

VARIABLES active, burned, phase, successes, crashedBeforeBody
vars == <<active, burned, phase, successes, crashedBeforeBody>>

Init ==
    /\ active = TRUE
    /\ burned = FALSE
    /\ phase = [attempt \in Attempts |-> IF attempt = "retry" THEN "waiting" ELSE "new"]
    /\ successes = 0
    /\ crashedBeforeBody = FALSE

Burn(attempt) ==
    /\ BurnBeforeBody
    /\ active
    /\ phase[attempt] = "new"
    /\ ~burned
    /\ burned' = TRUE
    /\ phase' = [phase EXCEPT ![attempt] = "burned"]
    /\ UNCHANGED <<active, successes, crashedBeforeBody>>

CheckUnusedBeforeBody(attempt) ==
    /\ ~BurnBeforeBody
    /\ active
    /\ phase[attempt] = "new"
    /\ ~burned
    /\ phase' = [phase EXCEPT ![attempt] = "checked"]
    /\ UNCHANGED <<active, burned, successes, crashedBeforeBody>>

RejectReplay(attempt) ==
    /\ active
    /\ burned
    /\ phase[attempt] = "new"
    /\ phase' = [phase EXCEPT ![attempt] = "rejected"]
    /\ UNCHANGED <<active, burned, successes, crashedBeforeBody>>

AcceptBody(attempt) ==
    /\ active
    /\ IF BurnBeforeBody
          THEN phase[attempt] = "burned"
          ELSE phase[attempt] = "checked"
    /\ phase' = [phase EXCEPT ![attempt] = "succeeded"]
    /\ burned' = TRUE
    /\ successes' = successes + 1
    /\ UNCHANGED <<active, crashedBeforeBody>>

RejectOversize(attempt) ==
    /\ active
    /\ phase[attempt] = "burned"
    /\ phase' = [phase EXCEPT ![attempt] = "failed"]
    /\ UNCHANGED <<active, burned, successes, crashedBeforeBody>>

CrashAfterBurn(attempt) ==
    /\ BurnBeforeBody
    /\ AllowCrash
    /\ active
    /\ phase[attempt] = "burned"
    /\ active' = FALSE
    /\ phase' = [phase EXCEPT ![attempt] = "crashed"]
    /\ crashedBeforeBody' = TRUE
    /\ UNCHANGED <<burned, successes>>

Restart ==
    /\ ~active
    /\ active' = TRUE
    /\ UNCHANGED <<burned, phase, successes, crashedBeforeBody>>

RetryAfterRestart ==
    /\ AllowCrash
    /\ active
    /\ crashedBeforeBody
    /\ phase["retry"] = "waiting"
    /\ phase' = [phase EXCEPT !["retry"] = "new"]
    /\ UNCHANGED <<active, burned, successes, crashedBeforeBody>>

Next ==
    \/ \E attempt \in Attempts : Burn(attempt)
    \/ \E attempt \in Attempts : CheckUnusedBeforeBody(attempt)
    \/ \E attempt \in Attempts : RejectReplay(attempt)
    \/ \E attempt \in Attempts : AcceptBody(attempt)
    \/ \E attempt \in Attempts : RejectOversize(attempt)
    \/ \E attempt \in Attempts : CrashAfterBurn(attempt)
    \/ Restart
    \/ RetryAfterRestart

Spec == Init /\ [][Next]_vars

CrashLossSpec == Spec /\ WF_vars(Restart) /\ WF_vars(RetryAfterRestart)
    /\ WF_vars(RejectReplay("retry"))

TypeOK ==
    /\ active \in BOOLEAN
    /\ burned \in BOOLEAN
    /\ phase \in [Attempts -> Phases]
    /\ successes \in Nat
    /\ crashedBeforeBody \in BOOLEAN

NoDuplicateSuccessfulUpload == successes <= 1

CrashBeforeBodyCannotLaterSucceed == crashedBeforeBody => successes = 0

CrashEventuallyUploads == [] (crashedBeforeBody => <> (successes = 1))

=============================================================================
