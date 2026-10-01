-------------------- MODULE BindingResult --------------------
EXTENDS Naturals, TLC

CONSTANTS IgnoreTransientReport, RetryPermanent

QueueStates == {"pending", "claimed", "retry-wait", "completed"}
Errors == {"none", "transient", "permanent"}

VARIABLES queueState, storedError, transientSeen, transientRecovered,
          permanentSeen
vars == <<queueState, storedError, transientSeen, transientRecovered,
          permanentSeen>>

Init ==
    /\ queueState = "pending"
    /\ storedError = "none"
    /\ transientSeen = FALSE
    /\ transientRecovered = FALSE
    /\ permanentSeen = FALSE

Claim ==
    /\ queueState \in {"pending", "retry-wait"}
    /\ queueState' = "claimed"
    /\ UNCHANGED <<storedError, transientSeen, transientRecovered,
                   permanentSeen>>

TransientFetchFailure ==
    /\ queueState = "claimed"
    /\ ~transientSeen
    /\ transientSeen' = TRUE
    /\ UNCHANGED <<transientRecovered, permanentSeen>>
    /\ IF IgnoreTransientReport
          THEN /\ queueState' = "completed"
               /\ storedError' = "none"
          ELSE /\ queueState' = "retry-wait"
               /\ storedError' = "transient"

PermanentAttachFailure ==
    /\ queueState = "claimed"
    /\ ~transientSeen
    /\ IF RetryPermanent
          THEN /\ queueState' = "retry-wait"
               /\ storedError' = "permanent"
          ELSE /\ queueState' = "completed"
               /\ storedError' = "permanent"
    /\ permanentSeen' = TRUE
    /\ UNCHANGED <<transientSeen, transientRecovered>>

RetrySucceeds ==
    /\ queueState = "claimed"
    /\ transientSeen
    /\ queueState' = "completed"
    /\ storedError' = "none"
    /\ transientRecovered' = TRUE
    /\ UNCHANGED <<transientSeen, permanentSeen>>

Next == Claim \/ TransientFetchFailure \/ PermanentAttachFailure \/ RetrySucceeds
Spec == Init /\ [][Next]_vars
FairSpec == Spec /\ WF_vars(Claim) /\ WF_vars(TransientFetchFailure)
          /\ WF_vars(PermanentAttachFailure) /\ WF_vars(RetrySucceeds)

NoTransientSuccessAck ==
    queueState = "completed" /\ transientSeen => transientRecovered

RetryWaitHasError == queueState = "retry-wait" => storedError # "none"
PermanentFailureNotRetried == queueState = "retry-wait" => storedError # "permanent"
PermanentFailureIsActionable ==
    permanentSeen /\ queueState = "completed" => storedError = "permanent"
EventuallyCompletes == <> (queueState = "completed")

=============================================================================
