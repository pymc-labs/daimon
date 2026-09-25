-------------------- MODULE RetryDeadline --------------------
EXTENDS Naturals, TLC

CONSTANTS QueueBackoff, ServerWait

QueueStates == {"claimed", "retry-wait", "completed"}
Times == 0..2

VARIABLES queueState, now, notBefore, retried
vars == <<queueState, now, notBefore, retried>>

Init ==
    /\ queueState = "claimed"
    /\ now = 0
    /\ notBefore = 0
    /\ retried = FALSE

RateLimitFailure ==
    /\ queueState = "claimed"
    /\ ~retried
    /\ queueState' = "retry-wait"
    /\ notBefore' = IF QueueBackoff > ServerWait
                       THEN now + QueueBackoff
                       ELSE now + ServerWait
    /\ retried' = TRUE
    /\ UNCHANGED now

AdvanceTime ==
    /\ queueState = "retry-wait"
    /\ now < 2
    /\ now' = now + 1
    /\ UNCHANGED <<queueState, notBefore, retried>>

Claim ==
    /\ queueState = "retry-wait"
    /\ now >= notBefore
    /\ queueState' = "claimed"
    /\ UNCHANGED <<now, notBefore, retried>>

Complete ==
    /\ queueState = "claimed"
    /\ retried
    /\ queueState' = "completed"
    /\ UNCHANGED <<now, notBefore, retried>>

Next == RateLimitFailure \/ AdvanceTime \/ Claim \/ Complete
Spec == Init /\ [][Next]_vars
FairSpec == Spec /\ WF_vars(RateLimitFailure) /\ WF_vars(AdvanceTime)
          /\ WF_vars(Claim) /\ WF_vars(Complete)

NoEarlyRetry == queueState = "claimed" /\ retried => now >= notBefore
EventuallyCompletes == <> (queueState = "completed")

=============================================================================
