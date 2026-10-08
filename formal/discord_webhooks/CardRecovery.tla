---------------------- MODULE CardRecovery ----------------------
EXTENDS Naturals, FiniteSets, TLC

\* One durable turn-card intent and at most two remote messages. Discord
\* accepts a post before the process knows its message ID; recovery lookup finds
\* all matching cancel IDs. Unsafe switches reproduce forbidden paths.
CONSTANTS BlindRetry, TokenAvailable, RetireReplacement,
          FinishBeforeEdit, CompleteEarly, DeleteFails,
          RetireOnDeleteFailure, EnableAgeOut, SkipAgeRetirement,
          ManageMessages, DeleteSucceeds, AnswerBeforeRetire,
          DeleteAnsweredCard, EnablePeriodic, UnsafeLiveAgeOut
VARIABLES process, intent, card, responseKnown, phase, answerPosts,
          bootRead, recoveryDone, aged, recoveryFailed
vars == <<process, intent, card, responseKnown, phase, answerPosts,
          bootRead, recoveryDone, aged, recoveryFailed>>

Init ==
    /\ process = "up"
    /\ intent = "absent"
    /\ card = [i \in 1..2 |-> "absent"]
    /\ responseKnown = FALSE
    /\ phase = "working"
    /\ answerPosts = 0
    /\ bootRead = FALSE
    /\ recoveryDone = FALSE
    /\ aged = FALSE
    /\ recoveryFailed = FALSE

CommitIntent ==
    /\ process = "up" /\ intent = "absent"
    /\ intent' = "prepared"
    /\ UNCHANGED <<process, card, responseKnown, phase, answerPosts,
                   bootRead, recoveryDone, aged, recoveryFailed>>

RemoteAccept ==
    /\ process = "up" /\ intent = "prepared"
    /\ card[1] = "absent"
    /\ card' = [card EXCEPT ![1] = "pending"]
    /\ UNCHANGED <<process, intent, responseKnown, phase, answerPosts,
                   bootRead, recoveryDone, aged, recoveryFailed>>

PersistResponse ==
    /\ process = "up" /\ intent = "prepared" /\ card[1] = "pending"
    /\ responseKnown' = TRUE
    /\ intent' = "posted"
    /\ UNCHANGED <<process, card, phase, answerPosts, bootRead, recoveryDone, aged,
                   recoveryFailed>>

\* A naive retry after a lost HTTP response makes a second visible card.
BlindPostAgain ==
    /\ BlindRetry /\ process = "up" /\ intent = "prepared"
    /\ card[1] = "pending" /\ card[2] = "absent"
    /\ card' = [card EXCEPT ![2] = "pending"]
    /\ UNCHANGED <<process, intent, responseKnown, phase, answerPosts,
                   bootRead, recoveryDone, aged, recoveryFailed>>

FinishAnswer ==
    /\ process = "up" /\ intent = "posted" /\ phase = "working"
    /\ answerPosts' = 1
    /\ phase' = "finished"
    /\ card' = IF FinishBeforeEdit THEN card
               ELSE [card EXCEPT ![1] = "terminal"]
    /\ intent' = "retired"
    /\ UNCHANGED <<process, responseKnown, bootRead, recoveryDone, aged,
                   recoveryFailed>>

\* The answer may be visible before the intent's terminal commit. A stale
\* recorded message ID can therefore point at an answered card.
AnswerVisibleBeforeRetire ==
    /\ AnswerBeforeRetire /\ process = "up" /\ intent = "posted"
    /\ phase = "working"
    /\ card' = [card EXCEPT ![1] = "answered"]
    /\ answerPosts' = 1 /\ phase' = "finished"
    /\ UNCHANGED <<process, intent, responseKnown, bootRead, recoveryDone,
                   aged, recoveryFailed>>

\* An unprompted turn with no final answer discards its transient card.
\* A failed webhook delete leaves the original card and its intent active.
SilentEnd ==
    /\ process = "up" /\ intent = "posted" /\ phase = "working"
    /\ phase' = "silent"
    /\ card' = IF DeleteFails THEN card
               ELSE [card EXCEPT ![1] = "absent"]
    /\ intent' = IF DeleteFails /\ ~RetireOnDeleteFailure
                  THEN intent ELSE "retired"
    /\ UNCHANGED <<process, responseKnown, answerPosts,
                   bootRead, recoveryDone, aged, recoveryFailed>>

Crash ==
    /\ process = "up" /\ intent \in {"prepared", "posted"}
    /\ process' = "down"
    /\ UNCHANGED <<intent, card, responseKnown, phase, answerPosts,
                   bootRead, recoveryDone, aged, recoveryFailed>>

BootLookup ==
    /\ process = "down" /\ ~bootRead
    /\ bootRead' = TRUE
    /\ UNCHANGED <<process, intent, card, responseKnown, phase,
                   answerPosts, recoveryDone, aged, recoveryFailed>>

\* Periodic recovery may inspect an old unresolved card while this process
\* is still up. A live turn is excluded unless the unsafe guard is enabled.
PeriodicLookup ==
    /\ EnablePeriodic /\ process = "up" /\ ~bootRead
    /\ intent \in {"prepared", "posted"}
    /\ (phase # "working" \/ UnsafeLiveAgeOut)
    /\ bootRead' = TRUE
    /\ UNCHANGED <<process, intent, card, responseKnown, phase, answerPosts,
                   recoveryDone, aged, recoveryFailed>>

\* Orphan sweep and intent reconciliation edit every known pending card.
RecoverEdit ==
    /\ process = "down" /\ bootRead /\ TokenAvailable
    /\ card[1] = "pending"
    /\ card' = [card EXCEPT ![1] = "terminal"]
    /\ UNCHANGED <<process, intent, responseKnown, phase, answerPosts,
                   bootRead, recoveryDone, aged, recoveryFailed>>

\* 10015 / missing webhook token: transport.edit posts a replacement but
\* cannot clear the old card's cancel button. Retiring here hides the gap.
RecoverReplacement ==
    /\ RetireReplacement /\ process = "down" /\ bootRead /\ ~TokenAvailable
    /\ card[1] = "pending" /\ card[2] = "absent"
    /\ card' = [card EXCEPT ![2] = "terminal"]
    /\ intent' = "retired"
    /\ UNCHANGED <<process, responseKnown, phase, answerPosts,
                   bootRead, recoveryDone, aged, recoveryFailed>>

CompleteRecovery ==
    /\ process = "down" /\ bootRead
    /\ (CompleteEarly \/ \A i \in 1..2: card[i] # "pending")
    /\ intent \in {"prepared", "posted"}
    /\ intent' = "retired"
    /\ recoveryDone' = TRUE
    /\ UNCHANGED <<process, card, responseKnown, phase, answerPosts,
                   bootRead, aged, recoveryFailed>>

\* A missing webhook/token or denied access is definite evidence. Repeated
\* failed passes also qualify after the configured count. A single 429, 5xx,
\* incomplete history read or cancellation does not enable age-out. The pass
\* counter itself is covered by the database-backed runtime tests.
RecoveryFailure ==
    /\ bootRead /\ ~TokenAvailable
    /\ (process = "down" \/ phase # "working" \/ UnsafeLiveAgeOut)
    /\ intent \in {"prepared", "posted"} /\ ~recoveryFailed
    /\ recoveryFailed' = TRUE
    /\ UNCHANGED <<process, intent, card, responseKnown, phase, answerPosts,
                   bootRead, recoveryDone, aged>>

\* A stale unresolved intent stops blocking later channel work. A bot with
\* Manage Messages makes one last delete attempt; deletion may still fail.
AgeOut ==
    /\ EnableAgeOut /\ bootRead /\ recoveryFailed /\ ~aged
    /\ (process = "down" \/ phase # "working" \/ UnsafeLiveAgeOut)
    /\ intent \in {"prepared", "posted"}
    /\ aged' = TRUE
    /\ intent' = IF SkipAgeRetirement THEN intent ELSE "unrecoverable"
    /\ card' = IF ManageMessages /\ DeleteSucceeds
                   /\ (card[1] = "pending" \/ DeleteAnsweredCard)
                THEN [card EXCEPT ![1] = "absent"] ELSE card
    /\ UNCHANGED <<process, responseKnown, phase, answerPosts,
                   bootRead, recoveryDone, recoveryFailed>>

Next == CommitIntent \/ RemoteAccept \/ PersistResponse \/ BlindPostAgain
        \/ FinishAnswer \/ AnswerVisibleBeforeRetire \/ SilentEnd \/ Crash
        \/ BootLookup \/ PeriodicLookup \/ RecoverEdit \/ RecoverReplacement \/ CompleteRecovery
        \/ RecoveryFailure \/ AgeOut
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ process \in {"up", "down"}
    /\ intent \in {"absent", "prepared", "posted", "retired", "unrecoverable"}
    /\ card \in [1..2 -> {"absent", "pending", "terminal", "answered"}]
    /\ responseKnown \in BOOLEAN
    /\ phase \in {"working", "finished", "silent"}
    /\ answerPosts \in 0..1
    /\ bootRead \in BOOLEAN /\ recoveryDone \in BOOLEAN /\ aged \in BOOLEAN
    /\ recoveryFailed \in BOOLEAN
NoDuplicateCard == Cardinality({i \in 1..2: card[i] # "absent"}) <= 1
NoPendingAfterRetirement ==
    intent = "retired" => \A i \in 1..2: card[i] # "pending"
NoPendingAfterTurnEnds ==
    phase = "finished" => \A i \in 1..2: card[i] # "pending"
NoPendingAfterRecovery ==
    recoveryDone => \A i \in 1..2: card[i] # "pending"
NoDuplicateAnswer == answerPosts <= 1
NoLostFinishedAnswer == phase = "finished" => answerPosts = 1
NoDeletedAnswer == phase = "finished" => card[1] # "absent"
NoStaleActive == aged => intent \notin {"prepared", "posted"}
NoLiveTurnRetired == process = "up" /\ phase = "working" => intent # "unrecoverable"
=================================================================
