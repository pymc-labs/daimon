--------------------- MODULE InitialCardIntent ---------------------
EXTENDS Naturals, TLC

VARIABLES processAlive, intentCommitted, intentStatus, cardVisible,
          messageIdRecorded, bootRead, bootFoundIntent
vars == <<processAlive, intentCommitted, intentStatus, cardVisible,
          messageIdRecorded, bootRead, bootFoundIntent>>

Init ==
    /\ processAlive = TRUE
    /\ intentCommitted = FALSE
    /\ intentStatus = "absent"
    /\ cardVisible = FALSE
    /\ messageIdRecorded = FALSE
    /\ bootRead = FALSE
    /\ bootFoundIntent = FALSE

CommitPreparedIntent ==
    /\ processAlive
    /\ intentStatus = "absent"
    /\ intentCommitted' = TRUE
    /\ intentStatus' = "prepared"
    /\ UNCHANGED <<processAlive, cardVisible, messageIdRecorded,
                   bootRead, bootFoundIntent>>

PostInitialCard ==
    /\ processAlive
    /\ intentCommitted
    /\ intentStatus = "prepared"
    /\ ~cardVisible
    /\ cardVisible' = TRUE
    /\ UNCHANGED <<processAlive, intentCommitted, intentStatus,
                   messageIdRecorded, bootRead, bootFoundIntent>>

PersistMessageId ==
    /\ processAlive
    /\ cardVisible
    /\ intentStatus = "prepared"
    /\ messageIdRecorded' = TRUE
    /\ intentStatus' = "posted"
    /\ UNCHANGED <<processAlive, intentCommitted, cardVisible,
                   bootRead, bootFoundIntent>>

ProcessDies ==
    /\ processAlive
    /\ processAlive' = FALSE
    /\ UNCHANGED <<intentCommitted, intentStatus, cardVisible,
                   messageIdRecorded, bootRead, bootFoundIntent>>

BootListIntents ==
    /\ ~processAlive
    /\ ~bootRead
    /\ bootRead' = TRUE
    /\ bootFoundIntent' = intentCommitted /\ intentStatus \in {"prepared", "posted"}
    /\ UNCHANGED <<processAlive, intentCommitted, intentStatus, cardVisible,
                   messageIdRecorded>>

Next == CommitPreparedIntent \/ PostInitialCard \/ PersistMessageId \/ ProcessDies \/ BootListIntents

PostBeforeIntent ==
    /\ processAlive
    /\ intentStatus = "absent"
    /\ ~intentCommitted
    /\ ~cardVisible
    /\ cardVisible' = TRUE
    /\ UNCHANGED <<processAlive, intentCommitted, intentStatus,
                   messageIdRecorded, bootRead, bootFoundIntent>>

PreWiringNext == Next \/ PostBeforeIntent

TypeOK ==
    /\ processAlive \in BOOLEAN
    /\ intentCommitted \in BOOLEAN
    /\ intentStatus \in {"absent", "prepared", "posted"}
    /\ cardVisible \in BOOLEAN
    /\ messageIdRecorded \in BOOLEAN
    /\ bootRead \in BOOLEAN
    /\ bootFoundIntent \in BOOLEAN

PostedCardHasCommittedIntent == cardVisible => intentCommitted
PostedStateHasMessageId == (intentStatus = "posted") => messageIdRecorded
BootListsEveryActiveIntent ==
    (bootRead /\ intentCommitted /\ intentStatus \in {"prepared", "posted"})
        => bootFoundIntent

Spec == Init /\ [][Next]_vars
PreWiringSpec == Init /\ [][PreWiringNext]_vars
=====================================================================
