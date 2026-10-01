---------------------- MODULE InstallationUpdates ----------------------
EXTENDS FiniteSets, TLC

CONSTANTS AtomicUpdates, BaseRepo, AddA, AddB, RemoveRepo

ASSUME AtomicUpdates \in BOOLEAN
ASSUME Cardinality({BaseRepo, AddA, AddB, RemoveRepo}) = 4

RepoUniverse == {BaseRepo, AddA, AddB, RemoveRepo}

VARIABLES repos, snapshotA, snapshotB, snapshotRemove,
          readA, readB, readRemove, doneA, doneB, doneRemove

vars == <<repos, snapshotA, snapshotB, snapshotRemove,
         readA, readB, readRemove, doneA, doneB, doneRemove>>

Init == /\ repos = {BaseRepo, RemoveRepo}
        /\ snapshotA = {}
        /\ snapshotB = {}
        /\ snapshotRemove = {}
        /\ readA = FALSE
        /\ readB = FALSE
        /\ readRemove = FALSE
        /\ doneA = FALSE
        /\ doneB = FALSE
        /\ doneRemove = FALSE

ReadA == /\ ~readA
         /\ snapshotA' = repos
         /\ readA' = TRUE
         /\ UNCHANGED <<repos, snapshotB, snapshotRemove,
                         readB, readRemove, doneA, doneB, doneRemove>>

WriteA == /\ readA
          /\ ~doneA
          /\ repos' = IF AtomicUpdates THEN repos \cup {AddA}
                      ELSE snapshotA \cup {AddA}
          /\ doneA' = TRUE
          /\ UNCHANGED <<snapshotA, snapshotB, snapshotRemove,
                          readA, readB, readRemove, doneB, doneRemove>>

ReadB == /\ ~readB
         /\ snapshotB' = repos
         /\ readB' = TRUE
         /\ UNCHANGED <<repos, snapshotA, snapshotRemove,
                         readA, readRemove, doneA, doneB, doneRemove>>

WriteB == /\ readB
          /\ ~doneB
          /\ repos' = IF AtomicUpdates THEN repos \cup {AddB}
                      ELSE snapshotB \cup {AddB}
          /\ doneB' = TRUE
          /\ UNCHANGED <<snapshotA, snapshotB, snapshotRemove,
                          readA, readB, readRemove, doneA, doneRemove>>

ReadRemoval == /\ ~readRemove
               /\ snapshotRemove' = repos
               /\ readRemove' = TRUE
               /\ UNCHANGED <<repos, snapshotA, snapshotB,
                               readA, readB, doneA, doneB, doneRemove>>

WriteRemoval == /\ readRemove
                /\ ~doneRemove
                /\ repos' = IF AtomicUpdates THEN repos \ {RemoveRepo}
                            ELSE snapshotRemove \ {RemoveRepo}
                /\ doneRemove' = TRUE
                /\ UNCHANGED <<snapshotA, snapshotB, snapshotRemove,
                                readA, readB, readRemove, doneA, doneB>>

Next == ReadA \/ WriteA \/ ReadB \/ WriteB \/ ReadRemoval \/ WriteRemoval
Spec == Init /\ [][Next]_vars

TypeOK == /\ repos \subseteq RepoUniverse
          /\ snapshotA \subseteq RepoUniverse
          /\ snapshotB \subseteq RepoUniverse
          /\ snapshotRemove \subseteq RepoUniverse
          /\ readA \in BOOLEAN
          /\ readB \in BOOLEAN
          /\ readRemove \in BOOLEAN
          /\ doneA \in BOOLEAN
          /\ doneB \in BOOLEAN
          /\ doneRemove \in BOOLEAN

EachCompletedDeltaIsPresent ==
    /\ (doneA => AddA \in repos)
    /\ (doneB => AddB \in repos)
    /\ (doneRemove => RemoveRepo \notin repos)

=============================================================================
