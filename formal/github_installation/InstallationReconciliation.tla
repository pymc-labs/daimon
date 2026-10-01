---------------------- MODULE InstallationReconciliation ----------------------
EXTENDS FiniteSets, Naturals, TLC

CONSTANTS BaseRepo, AddedRepo, GitHubRepos, MaxGeneration

ASSUME BaseRepo # AddedRepo
ASSUME GitHubRepos \subseteq {BaseRepo, AddedRepo}
ASSUME MaxGeneration \in Nat

RepoUniverse == {BaseRepo, AddedRepo}
JobStates == {"pending", "running", "done"}

VARIABLES cache, generation, state, claimedGeneration,
          fetched, fetchedGeneration, fetchedRepos

vars == <<cache, generation, state, claimedGeneration,
         fetched, fetchedGeneration, fetchedRepos>>

Init == /\ cache = {BaseRepo}
        /\ generation = 1
        /\ state = "running"
        /\ claimedGeneration = 1
        /\ fetched = FALSE
        /\ fetchedGeneration = 0
        /\ fetchedRepos = {}

Notify == /\ generation < MaxGeneration
          /\ generation' = generation + 1
          /\ state' = IF state = "running" THEN "running" ELSE "pending"
          /\ UNCHANGED <<cache, claimedGeneration,
                         fetched, fetchedGeneration, fetchedRepos>>

Claim == /\ state = "pending"
         /\ state' = "running"
         /\ claimedGeneration' = generation
         /\ fetched' = FALSE
         /\ fetchedGeneration' = 0
         /\ fetchedRepos' = {}
         /\ UNCHANGED <<cache, generation>>

FetchComplete == /\ state = "running"
                 /\ fetched' = TRUE
                 /\ fetchedGeneration' = claimedGeneration
                 /\ fetchedRepos' = GitHubRepos
                 /\ UNCHANGED <<cache, generation, state, claimedGeneration>>

CommitCurrent == /\ state = "running"
                 /\ fetched
                 /\ claimedGeneration = generation
                 /\ fetchedGeneration = claimedGeneration
                 /\ cache' = fetchedRepos
                 /\ state' = "done"
                 /\ fetched' = FALSE
                 /\ fetchedGeneration' = 0
                 /\ fetchedRepos' = {}
                 /\ UNCHANGED <<generation, claimedGeneration>>

DiscardStale == /\ state = "running"
                /\ fetched
                /\ claimedGeneration # generation
                /\ state' = "pending"
                /\ fetched' = FALSE
                /\ fetchedGeneration' = 0
                /\ fetchedRepos' = {}
                /\ UNCHANGED <<cache, generation, claimedGeneration>>

HoldDone == /\ state = "done"
           /\ UNCHANGED vars

Next == Notify \/ Claim \/ FetchComplete \/ CommitCurrent \/ DiscardStale \/ HoldDone
Spec == Init /\ [][Next]_vars

TypeOK == /\ cache \subseteq RepoUniverse
          /\ generation \in 0..MaxGeneration
          /\ state \in JobStates
          /\ claimedGeneration \in Nat
          /\ fetched \in BOOLEAN
          /\ fetchedGeneration \in Nat
          /\ fetchedRepos \subseteq RepoUniverse

CompletedSnapshotIsCurrent == state = "done" => cache = GitHubRepos

=============================================================================
