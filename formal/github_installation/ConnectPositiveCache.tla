---------------------- MODULE ConnectPositiveCache ----------------------
EXTENDS FiniteSets, Naturals, TLC

CONSTANTS BaseRepo, RemovedRepo, AddedRepo, InitialUnknown, FenceRevision

ASSUME Cardinality({BaseRepo, RemovedRepo, AddedRepo}) = 3
ASSUME InitialUnknown \in BOOLEAN
ASSUME FenceRevision \in BOOLEAN

RepoUniverse == {BaseRepo, RemovedRepo, AddedRepo}
VerifiedByUser == {RemovedRepo, AddedRepo}
NewCompleteSnapshot == {BaseRepo}

VARIABLES cache, cacheKnown, revision, captured, capturedRevision, fetched, fetchedRepos,
          reconciled, saved, rejected, suspended, removed

vars == <<cache, cacheKnown, revision, captured, capturedRevision, fetched, fetchedRepos,
          reconciled, saved, rejected, suspended, removed>>

Init == /\ cache = IF InitialUnknown THEN {} ELSE {BaseRepo, RemovedRepo}
        /\ cacheKnown = ~InitialUnknown
        /\ revision = 0
        /\ captured = FALSE
        /\ capturedRevision = 0
        /\ fetched = FALSE
        /\ fetchedRepos = {}
        /\ reconciled = FALSE
        /\ saved = FALSE
        /\ rejected = FALSE
        /\ suspended = TRUE
        /\ removed = TRUE

CaptureRevision == /\ ~captured
                   /\ captured' = TRUE
                   /\ capturedRevision' = revision
                   /\ UNCHANGED <<cache, cacheKnown, revision, fetched, fetchedRepos, reconciled,
                                   saved, rejected, suspended, removed>>

FetchSelected == /\ captured
                 /\ ~fetched
                 /\ fetched' = TRUE
                 /\ fetchedRepos' = IF capturedRevision = 0 THEN VerifiedByUser ELSE {}
                 /\ UNCHANGED <<cache, cacheKnown, revision, captured, capturedRevision,
                                 reconciled, saved, rejected, suspended, removed>>

Reconcile == /\ ~reconciled
             /\ cache' = NewCompleteSnapshot
             /\ cacheKnown' = TRUE
             /\ revision' = 1
             /\ reconciled' = TRUE
             /\ suspended' = FALSE
             /\ removed' = FALSE
             /\ UNCHANGED <<captured, capturedRevision, fetched, fetchedRepos,
                             saved, rejected>>

Save == /\ fetched
        /\ ~saved
        /\ saved' = TRUE
        /\ IF ~cacheKnown THEN
             /\ cache' = cache
             /\ rejected' = FALSE
           ELSE IF FenceRevision /\ revision # capturedRevision THEN
             /\ cache' = cache
             /\ rejected' = TRUE
           ELSE
             /\ cache' = cache \cup fetchedRepos
             /\ rejected' = FALSE
        /\ UNCHANGED <<cacheKnown, revision, captured, capturedRevision, fetched, fetchedRepos,
                        reconciled, suspended, removed>>

Next == CaptureRevision \/ FetchSelected \/ Reconcile \/ Save
Spec == Init /\ [][Next]_vars

TypeOK == /\ cache \subseteq RepoUniverse
          /\ cacheKnown \in BOOLEAN
          /\ revision \in 0..1
          /\ captured \in BOOLEAN
          /\ capturedRevision \in 0..1
          /\ fetched \in BOOLEAN
          /\ fetchedRepos \subseteq RepoUniverse
          /\ reconciled \in BOOLEAN
          /\ saved \in BOOLEAN
          /\ rejected \in BOOLEAN
          /\ suspended \in BOOLEAN
          /\ removed \in BOOLEAN

FreshRemovalNotReintroduced ==
    reconciled /\ saved => RemovedRepo \notin cache

UnknownPreservedUntilReconcile ==
    InitialUnknown /\ ~reconciled => ~cacheKnown

TombstonesPreservedBySave == ~reconciled => suspended /\ removed

=============================================================================
