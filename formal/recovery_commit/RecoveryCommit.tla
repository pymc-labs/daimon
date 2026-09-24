---------------------- MODULE RecoveryCommit ----------------------
EXTENDS Naturals, TLC

CONSTANTS UnsafeInnerCommit

VARIABLES oldDead, replacementRow, replacementLinked,
          txActive, txOldDead, txReplacementRow, txReplacementLinked,
          recoveryAttempted, upstreamSession

vars == <<oldDead, replacementRow, replacementLinked,
          txActive, txOldDead, txReplacementRow, txReplacementLinked,
          recoveryAttempted, upstreamSession>>

Init ==
    /\ oldDead = FALSE
    /\ replacementRow = FALSE
    /\ replacementLinked = FALSE
    /\ txActive = FALSE
    /\ txOldDead = FALSE
    /\ txReplacementRow = FALSE
    /\ txReplacementLinked = FALSE
    /\ recoveryAttempted = FALSE
    /\ upstreamSession = FALSE

BeginRecovery ==
    /\ ~txActive
    /\ ~recoveryAttempted
    /\ txActive' = TRUE
    /\ recoveryAttempted' = TRUE
    /\ txOldDead' = oldDead
    /\ txReplacementRow' = replacementRow
    /\ txReplacementLinked' = replacementLinked
    /\ UNCHANGED <<oldDead, replacementRow, replacementLinked, upstreamSession>>

MarkOldDead ==
    /\ txActive
    /\ ~txOldDead
    /\ txOldDead' = TRUE
    /\ UNCHANGED <<oldDead, replacementRow, replacementLinked,
                   txActive, txReplacementRow, txReplacementLinked,
                   recoveryAttempted, upstreamSession>>

CreateUpstreamSession ==
    /\ txActive
    /\ txOldDead
    /\ ~upstreamSession
    /\ upstreamSession' = TRUE
    /\ UNCHANGED <<oldDead, replacementRow, replacementLinked,
                   txActive, txOldDead, txReplacementRow, txReplacementLinked,
                   recoveryAttempted>>

InsertReplacementRow ==
    /\ txActive
    /\ upstreamSession
    /\ ~txReplacementRow
    /\ IF UnsafeInnerCommit
          THEN /\ replacementRow' = TRUE
               /\ txReplacementRow' = TRUE
          ELSE /\ replacementRow' = replacementRow
               /\ txReplacementRow' = TRUE
    /\ UNCHANGED <<oldDead, replacementLinked, txActive, txOldDead,
                   txReplacementLinked, recoveryAttempted, upstreamSession>>

LinkReplacement ==
    /\ txActive
    /\ txReplacementRow
    /\ txReplacementLinked' = TRUE
    /\ UNCHANGED <<oldDead, replacementRow, replacementLinked,
                   txActive, txOldDead, txReplacementRow, recoveryAttempted,
                   upstreamSession>>

CommitRecovery ==
    /\ txActive
    /\ txOldDead
    /\ txReplacementRow
    /\ txReplacementLinked
    /\ oldDead' = txOldDead
    /\ replacementRow' = txReplacementRow
    /\ replacementLinked' = txReplacementLinked
    /\ txActive' = FALSE
    /\ UNCHANGED <<txOldDead, txReplacementRow, txReplacementLinked,
                   recoveryAttempted, upstreamSession>>

AbortRecovery ==
    /\ txActive
    /\ txActive' = FALSE
    /\ txOldDead' = oldDead
    /\ txReplacementRow' = replacementRow
    /\ txReplacementLinked' = replacementLinked
    /\ UNCHANGED <<oldDead, replacementRow, replacementLinked,
                   recoveryAttempted, upstreamSession>>

Next == BeginRecovery \/ MarkOldDead \/ CreateUpstreamSession \/ InsertReplacementRow
        \/ LinkReplacement \/ CommitRecovery \/ AbortRecovery

TypeOK ==
    /\ oldDead \in BOOLEAN
    /\ replacementRow \in BOOLEAN
    /\ replacementLinked \in BOOLEAN
    /\ txActive \in BOOLEAN
    /\ txOldDead \in BOOLEAN
    /\ txReplacementRow \in BOOLEAN
    /\ txReplacementLinked \in BOOLEAN
    /\ recoveryAttempted \in BOOLEAN
    /\ upstreamSession \in BOOLEAN

NoOrphanAfterRecovery ==
    replacementRow => (txActive \/ (oldDead /\ replacementLinked))

Spec == Init /\ [][Next]_vars
THEOREM Spec => []TypeOK
====================================================================
