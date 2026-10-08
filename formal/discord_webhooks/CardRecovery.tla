---------------------- MODULE CardRecovery ----------------------
EXTENDS Naturals, FiniteSets, TLC

\* One durable turn-card intent and at most two remote messages. Discord
\* accepts a post before the process knows its message ID; boot lookup finds
\* all matching cancel IDs. The two Unsafe switches reproduce forbidden paths.
CONSTANTS BlindRetry, TokenAvailable, RetireReplacement,
          FinishBeforeEdit, CompleteEarly, DeleteFails,
          RetireOnDeleteFailure, EnableAgeOut, SkipAgeRetirement,
          ManageMessages, DeleteSucceeds
VARIABLES process, intent, card, responseKnown, phase, answerPosts,
          bootRead, recoveryDone, aged
vars == <<process, intent, card, responseKnown, phase, answerPosts,
          bootRead, recoveryDone, aged>>

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

CommitIntent ==
    /\ process = "up" /\ intent = "absent"
    /\ intent' = "prepared"
    /\ UNCHANGED <<process, card, responseKnown, phase, answerPosts,
                   bootRead, recoveryDone, aged>>

RemoteAccept ==
    /\ process = "up" /\ intent = "prepared"
    /\ card[1] = "absent"
    /\ card' = [card EXCEPT ![1] = "pending"]
    /\ UNCHANGED <<process, intent, responseKnown, phase, answerPosts,
                   bootRead, recoveryDone, aged>>

PersistResponse ==
    /\ process = "up" /\ intent = "prepared" /\ card[1] = "pending"
    /\ responseKnown' = TRUE
    /\ intent' = "posted"
    /\ UNCHANGED <<process, card, phase, answerPosts, bootRead, recoveryDone, aged>>

\* A naive retry after a lost HTTP response makes a second visible card.
BlindPostAgain ==
    /\ BlindRetry /\ process = "up" /\ intent = "prepared"
    /\ card[1] = "pending" /\ card[2] = "absent"
    /\ card' = [card EXCEPT ![2] = "pending"]
    /\ UNCHANGED <<process, intent, responseKnown, phase, answerPosts,
                   bootRead, recoveryDone, aged>>

FinishAnswer ==
    /\ process = "up" /\ intent = "posted" /\ phase = "working"
    /\ answerPosts' = 1
    /\ phase' = "finished"
    /\ card' = IF FinishBeforeEdit THEN card
               ELSE [card EXCEPT ![1] = "terminal"]
    /\ intent' = "retired"
    /\ UNCHANGED <<process, responseKnown, bootRead, recoveryDone, aged>>

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
                   bootRead, recoveryDone, aged>>

Crash ==
    /\ process = "up" /\ intent \in {"prepared", "posted"}
    /\ process' = "down"
    /\ UNCHANGED <<intent, card, responseKnown, phase, answerPosts,
                   bootRead, recoveryDone, aged>>

BootLookup ==
    /\ process = "down" /\ ~bootRead
    /\ bootRead' = TRUE
    /\ UNCHANGED <<process, intent, card, responseKnown, phase,
                   answerPosts, recoveryDone, aged>>

\* Orphan sweep and intent reconciliation edit every known pending card.
RecoverEdit ==
    /\ process = "down" /\ bootRead /\ TokenAvailable
    /\ card[1] = "pending"
    /\ card' = [card EXCEPT ![1] = "terminal"]
    /\ UNCHANGED <<process, intent, responseKnown, phase, answerPosts,
                   bootRead, recoveryDone, aged>>

\* 10015 / missing webhook token: transport.edit posts a replacement but
\* cannot clear the old card's cancel button. Retiring here hides the gap.
RecoverReplacement ==
    /\ RetireReplacement /\ process = "down" /\ bootRead /\ ~TokenAvailable
    /\ card[1] = "pending" /\ card[2] = "absent"
    /\ card' = [card EXCEPT ![2] = "terminal"]
    /\ intent' = "retired"
    /\ UNCHANGED <<process, responseKnown, phase, answerPosts,
                   bootRead, recoveryDone, aged>>

CompleteRecovery ==
    /\ process = "down" /\ bootRead
    /\ (CompleteEarly \/ \A i \in 1..2: card[i] # "pending")
    /\ intent \in {"prepared", "posted"}
    /\ intent' = "retired"
    /\ recoveryDone' = TRUE
    /\ UNCHANGED <<process, card, responseKnown, phase, answerPosts,
                   bootRead, aged>>

\* A stale unresolved intent stops blocking later channel work. A bot with
\* Manage Messages makes one last delete attempt; deletion may still fail.
AgeOut ==
    /\ EnableAgeOut /\ process = "down" /\ bootRead /\ ~aged
    /\ intent \in {"prepared", "posted"}
    /\ aged' = TRUE
    /\ intent' = IF SkipAgeRetirement THEN intent ELSE "unrecoverable"
    /\ card' = IF ManageMessages /\ DeleteSucceeds
                THEN [card EXCEPT ![1] = "absent"] ELSE card
    /\ UNCHANGED <<process, responseKnown, phase, answerPosts,
                   bootRead, recoveryDone>>

Next == CommitIntent \/ RemoteAccept \/ PersistResponse \/ BlindPostAgain
        \/ FinishAnswer \/ SilentEnd \/ Crash \/ BootLookup \/ RecoverEdit
        \/ RecoverReplacement \/ CompleteRecovery \/ AgeOut
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ process \in {"up", "down"}
    /\ intent \in {"absent", "prepared", "posted", "retired", "unrecoverable"}
    /\ card \in [1..2 -> {"absent", "pending", "terminal"}]
    /\ responseKnown \in BOOLEAN
    /\ phase \in {"working", "finished", "silent"}
    /\ answerPosts \in 0..1
    /\ bootRead \in BOOLEAN /\ recoveryDone \in BOOLEAN /\ aged \in BOOLEAN
NoDuplicateCard == Cardinality({i \in 1..2: card[i] # "absent"}) <= 1
NoPendingAfterRetirement ==
    intent = "retired" => \A i \in 1..2: card[i] # "pending"
NoPendingAfterTurnEnds ==
    phase = "finished" => \A i \in 1..2: card[i] # "pending"
NoPendingAfterRecovery ==
    recoveryDone => \A i \in 1..2: card[i] # "pending"
NoDuplicateAnswer == answerPosts <= 1
NoLostFinishedAnswer == phase = "finished" => answerPosts = 1
NoStaleActive == aged => intent \notin {"prepared", "posted"}
=================================================================
