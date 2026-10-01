---------------------- MODULE InitialCardCrash ----------------------
EXTENDS Naturals, TLC

CONSTANTS NoMarker, FirstMessage

VARIABLES firstCardVisible, firstCardFrozen, dbMarker, processAlive,
          bootSweepDone, secondCardVisible
vars == <<firstCardVisible, firstCardFrozen, dbMarker, processAlive,
          bootSweepDone, secondCardVisible>>

Init ==
    /\ firstCardVisible = FALSE
    /\ firstCardFrozen = FALSE
    /\ dbMarker = NoMarker
    /\ processAlive = TRUE
    /\ bootSweepDone = FALSE
    /\ secondCardVisible = FALSE

PostInitialCard ==
    /\ processAlive
    /\ ~firstCardVisible
    /\ firstCardVisible' = TRUE
    /\ firstCardFrozen' = TRUE
    /\ UNCHANGED <<dbMarker, processAlive, bootSweepDone, secondCardVisible>>

PersistActiveMarker ==
    /\ processAlive
    /\ firstCardVisible
    /\ dbMarker = NoMarker
    /\ dbMarker' = FirstMessage
    /\ UNCHANGED <<firstCardVisible, firstCardFrozen, processAlive,
                   bootSweepDone, secondCardVisible>>

ProcessDies ==
    /\ processAlive
    /\ processAlive' = FALSE
    /\ UNCHANGED <<firstCardVisible, firstCardFrozen, dbMarker,
                   bootSweepDone, secondCardVisible>>

BootSweep ==
    /\ ~processAlive
    /\ ~bootSweepDone
    /\ bootSweepDone' = TRUE
    /\ IF dbMarker = FirstMessage
          THEN firstCardFrozen' = FALSE
          ELSE UNCHANGED firstCardFrozen
    /\ dbMarker' = NoMarker
    /\ UNCHANGED <<firstCardVisible, processAlive, secondCardVisible>>

PostNextInitialCard ==
    /\ ~processAlive
    /\ bootSweepDone
    /\ firstCardVisible
    /\ ~secondCardVisible
    /\ secondCardVisible' = TRUE
    /\ UNCHANGED <<firstCardVisible, firstCardFrozen, dbMarker,
                   processAlive, bootSweepDone>>

Next == PostInitialCard \/ PersistActiveMarker \/ ProcessDies \/ BootSweep \/ PostNextInitialCard

TypeOK ==
    /\ firstCardVisible \in BOOLEAN
    /\ firstCardFrozen \in BOOLEAN
    /\ dbMarker \in {NoMarker, FirstMessage}
    /\ processAlive \in BOOLEAN
    /\ bootSweepDone \in BOOLEAN
    /\ secondCardVisible \in BOOLEAN

NoFrozenCardAfterRecovery ==
    bootSweepDone => ~firstCardFrozen

NoDuplicateVisibleCards ==
    ~(firstCardFrozen /\ secondCardVisible)

Spec == Init /\ [][Next]_vars
THEOREM Spec => []TypeOK
======================================================================
