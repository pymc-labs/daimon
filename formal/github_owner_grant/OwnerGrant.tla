---------------------------- MODULE OwnerGrant ----------------------------
EXTENDS FiniteSets, TLC

CONSTANTS NoExclusion, MintFromCache
ASSUME NoExclusion \in BOOLEAN /\ MintFromCache \in BOOLEAN

Repos == {"alpha", "beta", "future"}
InitialCoverage == {"alpha", "beta"}

VARIABLES covered, suspended, deleted, singles, ownerGrant, ownerCache,
          exclusions, removed, token, outsideCoverage, removedMinted,
          withoutGrant, futureMinted
vars == <<covered, suspended, deleted, singles, ownerGrant, ownerCache,
          exclusions, removed, token, outsideCoverage, removedMinted,
          withoutGrant, futureMinted>>

InstallationCoverage == IF suspended \/ deleted THEN {} ELSE covered
Granted == singles \cup (IF ownerGrant THEN Repos ELSE {})
OwnerResolved ==
  IF ownerGrant
  THEN (IF MintFromCache THEN ownerCache ELSE InstallationCoverage) \ exclusions
  ELSE {}
Resolved == (singles \cap InstallationCoverage) \cup OwnerResolved

Init ==
  /\ covered = InitialCoverage
  /\ suspended = FALSE /\ deleted = FALSE
  /\ singles = {} /\ ownerGrant = FALSE /\ ownerCache = {}
  /\ exclusions = {} /\ removed = {} /\ token = {}
  /\ outsideCoverage = FALSE /\ removedMinted = FALSE
  /\ withoutGrant = FALSE /\ futureMinted = FALSE

GrantSingle(r) ==
  /\ r \in InstallationCoverage
  /\ singles' = singles \cup {r}
  /\ exclusions' = exclusions \ {r}
  /\ removed' = removed \ {r}
  /\ UNCHANGED <<covered, suspended, deleted, ownerGrant, ownerCache,
                  token, outsideCoverage, removedMinted, withoutGrant,
                  futureMinted>>

GrantOwner ==
  /\ ~ownerGrant /\ ~deleted
  /\ ownerGrant' = TRUE
  /\ ownerCache' = InstallationCoverage
  /\ UNCHANGED <<covered, suspended, deleted, singles, exclusions, removed,
                  token, outsideCoverage, removedMinted, withoutGrant,
                  futureMinted>>

RemoveSingle(r) ==
  /\ r \in Repos /\ (r \in singles \/ ownerGrant)
  /\ singles' = singles \ {r}
  /\ exclusions' = IF ownerGrant /\ ~NoExclusion
                    THEN exclusions \cup {r} ELSE exclusions
  /\ removed' = IF ownerGrant THEN removed \cup {r} ELSE removed
  /\ UNCHANGED <<covered, suspended, deleted, ownerGrant, ownerCache,
                  token, outsideCoverage, removedMinted, withoutGrant,
                  futureMinted>>

RemoveOwner ==
  /\ ownerGrant
  /\ ownerGrant' = FALSE /\ ownerCache' = {}
  /\ UNCHANGED <<covered, suspended, deleted, singles, exclusions, removed, token,
                  outsideCoverage, removedMinted, withoutGrant, futureMinted>>

CoverRepo(r) ==
  /\ r \in Repos /\ ~deleted /\ r \notin covered
  /\ covered' = covered \cup {r}
  /\ UNCHANGED <<suspended, deleted, singles, ownerGrant, ownerCache,
                  exclusions, removed, token, outsideCoverage,
                  removedMinted, withoutGrant, futureMinted>>

UncoverRepo(r) ==
  /\ r \in covered
  /\ covered' = covered \ {r}
  /\ UNCHANGED <<suspended, deleted, singles, ownerGrant, ownerCache,
                  exclusions, removed, token, outsideCoverage,
                  removedMinted, withoutGrant, futureMinted>>

Suspend ==
  /\ ~suspended /\ ~deleted
  /\ suspended' = TRUE
  /\ UNCHANGED <<covered, deleted, singles, ownerGrant, ownerCache,
                  exclusions, removed, token, outsideCoverage,
                  removedMinted, withoutGrant, futureMinted>>

Unsuspend ==
  /\ suspended /\ ~deleted
  /\ suspended' = FALSE
  /\ UNCHANGED <<covered, deleted, singles, ownerGrant, ownerCache,
                  exclusions, removed, token, outsideCoverage,
                  removedMinted, withoutGrant, futureMinted>>

DeleteInstallation ==
  /\ ~deleted
  /\ deleted' = TRUE /\ covered' = {} /\ suspended' = FALSE
  /\ UNCHANGED <<singles, ownerGrant, ownerCache, exclusions, removed,
                  token, outsideCoverage, removedMinted, withoutGrant,
                  futureMinted>>

Mint ==
  /\ token' = Resolved
  /\ outsideCoverage' = (outsideCoverage \/ ~(Resolved \subseteq InstallationCoverage))
  /\ removedMinted' = (removedMinted \/ (Resolved \cap removed # {}))
  /\ withoutGrant' = (withoutGrant \/ ~(Resolved \subseteq Granted))
  /\ futureMinted' = (futureMinted \/
                        (ownerGrant /\ "future" \in Resolved /\
                         "future" \notin singles /\ "future" \notin ownerCache))
  /\ UNCHANGED <<covered, suspended, deleted, singles, ownerGrant,
                  ownerCache, exclusions, removed>>

Next ==
  \/ \E r \in Repos : GrantSingle(r) \/ RemoveSingle(r)
  \/ GrantOwner \/ RemoveOwner
  \/ \E r \in Repos : CoverRepo(r) \/ UncoverRepo(r)
  \/ Suspend \/ Unsuspend \/ DeleteInstallation \/ Mint

Spec == Init /\ [][Next]_vars

MintedSubsetCovered == ~outsideCoverage
ExcludedNeverMinted == ~removedMinted
MintedHasGrant == ~withoutGrant
FutureRepoNeverMinted == ~futureMinted
=============================================================================
