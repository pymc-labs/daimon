-------------------- MODULE PrivacyDelete --------------------
EXTENDS Naturals

CONSTANT DurableCommit
VARIABLES phase, queued, pending, gone, live, crashed, failures
vars == <<phase, queued, pending, gone, live, crashed, failures>>

Init ==
    /\ phase = "before"
    /\ queued = FALSE
    /\ pending = FALSE
    /\ gone = FALSE
    /\ live = TRUE
    /\ crashed = FALSE
    /\ failures = 0

\* The local purge and queue insert share one database transaction.
CommitLocal ==
    /\ phase = "before"
    /\ phase' = "local"
    /\ queued' = DurableCommit
    /\ UNCHANGED <<pending, gone, live, crashed, failures>>

\* Death can happen after local commit or while upstream work is in flight.
Crash ==
    /\ phase # "before" /\ ~crashed /\ live /\ ~gone
    /\ live' = FALSE
    /\ crashed' = TRUE
    /\ phase' = "local"
    /\ UNCHANGED <<queued, pending, gone, failures>>

Restart ==
    /\ ~live
    /\ live' = TRUE
    /\ UNCHANGED <<phase, queued, pending, gone, crashed, failures>>

EnumerationFailure ==
    /\ phase = "local" /\ queued /\ live /\ failures = 0
    /\ failures' = 1
    /\ UNCHANGED <<phase, queued, pending, gone, live, crashed>>

Enumerate ==
    /\ phase = "local" /\ queued /\ live
    /\ phase' = "found"
    /\ UNCHANGED <<queued, pending, gone, live, crashed, failures>>

Remember ==
    /\ phase = "found" /\ live
    /\ pending' = TRUE
    /\ phase' = "deleting"
    /\ UNCHANGED <<queued, gone, live, crashed, failures>>

DeleteFailure ==
    /\ phase = "deleting" /\ live /\ failures = 0
    /\ failures' = 1
    /\ phase' = "local"
    /\ UNCHANGED <<queued, pending, gone, live, crashed>>

DeleteSuccess ==
    /\ phase = "deleting" /\ live
    /\ gone' = TRUE
    /\ phase' = "deleted"
    /\ UNCHANGED <<queued, pending, live, crashed, failures>>

Acknowledge ==
    /\ phase = "deleted" /\ live
    /\ pending' = FALSE
    /\ phase' = "acknowledged"
    /\ UNCHANGED <<queued, gone, live, crashed, failures>>

Finish ==
    /\ phase = "acknowledged" /\ live /\ gone /\ ~pending
    /\ queued' = FALSE
    /\ phase' = "done"
    /\ UNCHANGED <<pending, gone, live, crashed, failures>>

Next == CommitLocal \/ Crash \/ Restart \/ EnumerationFailure \/ Enumerate
        \/ Remember \/ DeleteFailure \/ DeleteSuccess \/ Acknowledge \/ Finish
Spec == Init /\ [][Next]_vars
Progress == Spec /\ WF_vars(CommitLocal) /\ WF_vars(Restart)
            /\ WF_vars(Enumerate) /\ WF_vars(Remember)
            /\ WF_vars(DeleteSuccess) /\ WF_vars(Acknowledge) /\ WF_vars(Finish)

TypeOK ==
    /\ phase \in {"before", "local", "found", "deleting", "deleted", "acknowledged", "done"}
    /\ queued \in BOOLEAN /\ pending \in BOOLEAN /\ gone \in BOOLEAN
    /\ live \in BOOLEAN /\ crashed \in BOOLEAN /\ failures \in 0..1

LocalDeletionTracked == (phase # "before" /\ ~gone) => queued
NoPrematureFinish == (phase = "done") => gone
EventuallyGone == <>gone
===============================================================
