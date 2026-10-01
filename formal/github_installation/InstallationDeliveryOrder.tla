---------------------- MODULE InstallationDeliveryOrder ----------------------
EXTENDS FiniteSets, TLC

CONSTANTS BaseRepo, ChangedRepo, IncludeRemoval

ASSUME BaseRepo # ChangedRepo
ASSUME IncludeRemoval \in BOOLEAN

RepoUniverse == {BaseRepo, ChangedRepo}
Deliveries == {"created", "added"} \cup (IF IncludeRemoval THEN {"removed"} ELSE {})

VARIABLES repos, pending

vars == <<repos, pending>>

Init == /\ repos = {BaseRepo}
        /\ pending = Deliveries

DeliverCreated == /\ "created" \in pending
                  /\ repos' = {BaseRepo}
                  /\ pending' = pending \ {"created"}

DeliverAdded == /\ "added" \in pending
                /\ repos' = repos \cup {ChangedRepo}
                /\ pending' = pending \ {"added"}

DeliverRemoved == /\ "removed" \in pending
                  /\ repos' = repos \ {ChangedRepo}
                  /\ pending' = pending \ {"removed"}

Next == DeliverCreated \/ DeliverAdded \/ DeliverRemoved
Spec == Init /\ [][Next]_vars

TypeOK == /\ repos \subseteq RepoUniverse
          /\ pending \subseteq Deliveries

FinalSetMatchesGitHub ==
    pending = {} => repos = IF IncludeRemoval THEN {BaseRepo}
                             ELSE {BaseRepo, ChangedRepo}

=============================================================================
