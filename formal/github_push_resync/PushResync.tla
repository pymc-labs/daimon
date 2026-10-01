---------------------- MODULE PushResync ----------------------
EXTENDS Naturals, TLC

CONSTANTS UnsafeAck, UnsafeComplete, AllowCrash

Workers == {1, 2}
Phases == {"idle", "claimed", "locked", "synced"}
States == {"none", "pending", "running", "done"}

VARIABLES received, acknowledged, generation, jobState, owner, lockOwner,
          phase, claimGeneration, externalVersion, externalWrites

vars == <<received, acknowledged, generation, jobState, owner, lockOwner,
          phase, claimGeneration, externalVersion, externalWrites>>

Init ==
    /\ received = 0
    /\ acknowledged = 0
    /\ generation = 0
    /\ jobState = "none"
    /\ owner = 0
    /\ lockOwner = 0
    /\ phase = [w \in Workers |-> "idle"]
    /\ claimGeneration = [w \in Workers |-> 0]
    /\ externalVersion = 0
    /\ externalWrites = 0

\* A signed push has a monotonically newer branch version in this bound.
\* The safe path commits the receipt and coalesced row before returning 200.
Receive ==
    /\ received < 2
    /\ received' = received + 1
    /\ acknowledged' = received + 1
    /\ IF UnsafeAck
          THEN UNCHANGED <<generation, jobState>>
          ELSE /\ generation' = received + 1
               /\ jobState' = IF jobState = "running" THEN "running" ELSE "pending"
    /\ UNCHANGED <<owner, lockOwner, phase, claimGeneration,
                   externalVersion, externalWrites>>

Claim(w) ==
    /\ w \in Workers
    /\ jobState = "pending"
    /\ owner = 0
    /\ phase[w] = "idle"
    /\ owner' = w
    /\ jobState' = "running"
    /\ phase' = [phase EXCEPT ![w] = "claimed"]
    /\ claimGeneration' = [claimGeneration EXCEPT ![w] = generation]
    /\ UNCHANGED <<received, acknowledged, generation, lockOwner,
                   externalVersion, externalWrites>>

AcquireRepoLock(w) ==
    /\ w \in Workers
    /\ owner = w
    /\ phase[w] = "claimed"
    /\ lockOwner = 0
    /\ lockOwner' = w
    /\ phase' = [phase EXCEPT ![w] = "locked"]
    /\ UNCHANGED <<received, acknowledged, generation, jobState, owner,
                   claimGeneration, externalVersion, externalWrites>>

\* A pass fetches the current branch; the external MA effect may repeat.
SyncCurrentBranch(w) ==
    /\ w \in Workers
    /\ owner = w
    /\ lockOwner = w
    /\ phase[w] = "locked"
    /\ externalWrites < 3
    /\ externalVersion' = received
    /\ externalWrites' = externalWrites + 1
    /\ phase' = [phase EXCEPT ![w] = "synced"]
    /\ UNCHANGED <<received, acknowledged, generation, jobState, owner,
                   lockOwner, claimGeneration>>

Complete(w) ==
    /\ w \in Workers
    /\ owner = w
    /\ lockOwner = w
    /\ phase[w] = "synced"
    /\ jobState' = IF UnsafeComplete \/ claimGeneration[w] = generation
                      THEN "done" ELSE "pending"
    /\ owner' = 0
    /\ lockOwner' = 0
    /\ phase' = [phase EXCEPT ![w] = "idle"]
    /\ UNCHANGED <<received, acknowledged, generation, claimGeneration,
                   externalVersion, externalWrites>>

\* Collapse process death plus lease expiration into one recovery step.
CrashAndExpire(w) ==
    /\ AllowCrash
    /\ w \in Workers
    /\ owner = w
    /\ phase[w] \in {"claimed", "locked", "synced"}
    /\ owner' = 0
    /\ lockOwner' = 0
    /\ jobState' = "pending"
    /\ phase' = [phase EXCEPT ![w] = "idle"]
    /\ UNCHANGED <<received, acknowledged, generation, claimGeneration,
                   externalVersion, externalWrites>>

NoCrashNext == Receive \/ (\E w \in Workers : Claim(w) \/ AcquireRepoLock(w)
                                               \/ SyncCurrentBranch(w) \/ Complete(w))

Next == NoCrashNext
        \/ (\E w \in Workers : CrashAndExpire(w))

TypeOK ==
    /\ received \in 0..2
    /\ acknowledged \in 0..2
    /\ generation \in 0..2
    /\ jobState \in States
    /\ owner \in Workers \cup {0}
    /\ lockOwner \in Workers \cup {0}
    /\ phase \in [Workers -> Phases]
    /\ claimGeneration \in [Workers -> 0..2]
    /\ externalVersion \in 0..2
    /\ externalWrites \in 0..3

AckHasDurableGeneration == acknowledged <= generation
DoneHasCurrentBranch == jobState = "done" => externalVersion = received
NoRepeatedExternalEffect == externalWrites <= 1

Spec == Init /\ [][Next]_vars

FairSpec ==
    Init /\ [][NoCrashNext]_vars
    /\ WF_vars(Receive)
    /\ WF_vars(\E w \in Workers : Claim(w))
    /\ WF_vars(\E w \in Workers : AcquireRepoLock(w))
    /\ WF_vars(\E w \in Workers : SyncCurrentBranch(w))
    /\ WF_vars(\E w \in Workers : Complete(w))

EventuallyCurrent == <> (received = 2 /\ jobState = "done" /\ externalVersion = 2)
====================================================================
