---------------------- MODULE AdapterRecovery ----------------------
EXTENDS Naturals, TLC

CONSTANTS NoMarker, OldMarker, NewMarker, AllowTurnBeforeRecovery, UseCAS
Markers == {NoMarker, OldMarker, NewMarker}

VARIABLES marker, snapshot, turnLive, recoveryDone
vars == <<marker, snapshot, turnLive, recoveryDone>>

Init ==
    /\ marker = OldMarker
    /\ snapshot = NoMarker
    /\ turnLive = FALSE
    /\ recoveryDone = FALSE

StartTurn ==
    /\ ~turnLive
    /\ (AllowTurnBeforeRecovery \/ recoveryDone)
    /\ marker' = NewMarker
    /\ turnLive' = TRUE
    /\ UNCHANGED <<snapshot, recoveryDone>>

TakeSnapshot ==
    /\ ~recoveryDone
    /\ snapshot = NoMarker
    /\ marker # NoMarker
    /\ snapshot' = marker
    /\ UNCHANGED <<marker, turnLive, recoveryDone>>

ClearSnapshot ==
    /\ ~recoveryDone
    /\ snapshot # NoMarker
    /\ IF UseCAS
          THEN marker' = IF marker = snapshot THEN NoMarker ELSE marker
          ELSE marker' = NoMarker
    /\ recoveryDone' = TRUE
    /\ UNCHANGED <<snapshot, turnLive>>

FinishTurn ==
    /\ turnLive
    /\ marker' = NoMarker
    /\ turnLive' = FALSE
    /\ UNCHANGED <<snapshot, recoveryDone>>

RecoveryWithoutMarker ==
    /\ ~recoveryDone
    /\ marker = NoMarker
    /\ recoveryDone' = TRUE
    /\ UNCHANGED <<marker, snapshot, turnLive>>

Next == StartTurn \/ TakeSnapshot \/ ClearSnapshot \/ FinishTurn \/ RecoveryWithoutMarker

TypeOK ==
    /\ marker \in Markers
    /\ snapshot \in Markers
    /\ turnLive \in BOOLEAN
    /\ recoveryDone \in BOOLEAN

LiveTurnHasMarker == turnLive => marker = NewMarker

Spec == Init /\ [][Next]_vars
THEOREM Spec => []TypeOK
=============================================================================
