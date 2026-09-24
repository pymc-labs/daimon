---------------- MODULE SessionPreparation ----------------
EXTENDS Naturals, FiniteSets, TLC

\* Bounded model of the lock, decision and replacement protocol in
\* core/session_preparation.py and session_preparation_stages.py.
Callers == {"c1", "c2"}
Changes == {"reuse", "update", "replace", "handoff"}
CallStates == {"idle", "waiting", "decide", "work", "finished"}
Stages == {"none", "decided", "checkpointed", "uploaded", "created",
           "completed", "failed"}
NoOwner == "none"

VARIABLES state, change, active, owner, stage, oldLive, successor,
          returned, attempts, uploaded, closed
vars == <<state, change, active, owner, stage, oldLive, successor,
          returned, attempts, uploaded, closed>>

Init ==
  /\ state = [c \in Callers |-> "idle"]
  /\ change = [c \in Callers |-> "reuse"]
  /\ active = [c \in Callers |-> FALSE]
  /\ owner = NoOwner
  /\ stage = "none"
  /\ oldLive = TRUE
  /\ successor = FALSE
  /\ returned = [c \in Callers |-> "none"]
  /\ attempts = 0
  /\ uploaded = FALSE
  /\ closed = FALSE

Start(c, kind, inTurn) ==
  /\ state[c] = "idle" /\ stage # "completed" /\ attempts < 4
  /\ kind \in Changes
  /\ state' = [state EXCEPT ![c] = IF owner = NoOwner THEN "decide" ELSE "waiting"]
  /\ change' = [change EXCEPT ![c] = kind]
  /\ active' = [active EXCEPT ![c] = inTurn]
  /\ owner' = IF owner = NoOwner THEN c ELSE owner
  /\ UNCHANGED <<stage, oldLive, successor, returned, attempts,
                 uploaded, closed>>

Acquire(c) ==
  /\ state[c] = "waiting" /\ owner = NoOwner
  /\ state' = [state EXCEPT ![c] = "decide"]
  /\ change' = [change EXCEPT ![c] = IF stage = "completed" THEN "reuse" ELSE @]
  /\ owner' = c
  /\ UNCHANGED <<active, stage, oldLive, successor, returned,
                 attempts, uploaded, closed>>

Reuse(c) ==
  /\ owner = c /\ state[c] = "decide" /\ change[c] = "reuse"
  /\ state' = [state EXCEPT ![c] = "finished"]
  /\ returned' = [returned EXCEPT ![c] = "prepared"]
  /\ owner' = NoOwner
  /\ UNCHANGED <<change, active, stage, oldLive, successor,
                 attempts, uploaded, closed>>

Defer(c) ==
  /\ owner = c /\ state[c] = "decide" /\ active[c]
  /\ change[c] \in {"update", "replace"}
  /\ state' = [state EXCEPT ![c] = "finished"]
  /\ returned' = [returned EXCEPT ![c] = "deferred"]
  /\ owner' = NoOwner
  /\ UNCHANGED <<change, active, stage, oldLive, successor,
                 attempts, uploaded, closed>>

BusyHandoff(c) ==
  /\ owner = c /\ state[c] = "decide" /\ active[c]
  /\ change[c] = "handoff"
  /\ state' = [state EXCEPT ![c] = "finished"]
  /\ returned' = [returned EXCEPT ![c] = "busy"]
  /\ owner' = NoOwner
  /\ UNCHANGED <<change, active, stage, oldLive, successor,
                 attempts, uploaded, closed>>

Update(c) ==
  /\ owner = c /\ state[c] = "decide" /\ change[c] = "update" /\ ~active[c]
  /\ state' = [state EXCEPT ![c] = "finished"]
  /\ returned' = [returned EXCEPT ![c] = "prepared"]
  /\ owner' = NoOwner
  /\ UNCHANGED <<change, active, stage, oldLive, successor,
                 attempts, uploaded, closed>>

BeginReplacement(c) ==
  /\ owner = c /\ state[c] = "decide" /\ change[c] \in {"replace", "handoff"}
  /\ ~active[c] /\ attempts < 4
  /\ state' = [state EXCEPT ![c] = "work"]
  /\ stage' = IF stage = "failed" THEN "failed" ELSE "decided"
  /\ attempts' = attempts + 1
  /\ UNCHANGED <<change, active, owner, oldLive, successor, returned,
                 uploaded, closed>>

Checkpoint ==
  /\ owner \in Callers /\ state[owner] = "work"
  /\ stage \in {"decided", "failed"}
  /\ stage' = "checkpointed"
  /\ UNCHANGED <<state, change, active, owner, oldLive, successor, returned,
                 attempts, uploaded, closed>>

Upload ==
  /\ owner \in Callers /\ state[owner] = "work" /\ stage = "checkpointed"
  /\ stage' = "uploaded" /\ uploaded' = TRUE
  /\ UNCHANGED <<state, change, active, owner, oldLive, successor, returned,
                 attempts, closed>>

CreateSuccessor ==
  /\ owner \in Callers /\ state[owner] = "work"
  /\ stage \in {"decided", "checkpointed", "uploaded"}
  /\ stage' = "created" /\ successor' = TRUE
  /\ UNCHANGED <<state, change, active, owner, oldLive, returned,
                 attempts, uploaded, closed>>

CloseOut ==
  /\ owner \in Callers /\ state[owner] = "work" /\ stage = "created"
  /\ stage' = "completed" /\ oldLive' = FALSE /\ closed' = TRUE
  /\ state' = [state EXCEPT ![owner] = "finished"]
  /\ returned' = [returned EXCEPT ![owner] = "prepared"]
  /\ owner' = NoOwner
  /\ UNCHANGED <<change, active, successor, attempts, uploaded>>

Fail ==
  /\ owner \in Callers /\ state[owner] = "work"
  /\ stage \in {"decided", "checkpointed", "uploaded"}
  /\ stage' = "failed"
  /\ state' = [state EXCEPT ![owner] = "finished"]
  /\ returned' = [returned EXCEPT ![owner] = "failure"]
  /\ owner' = NoOwner
  /\ UNCHANGED <<change, active, oldLive, successor, attempts,
                 uploaded, closed>>

Retry(c) ==
  /\ state[c] = "finished" /\ returned[c] = "failure"
  /\ owner = NoOwner /\ stage = "failed" /\ attempts < 3
  /\ state' = [state EXCEPT ![c] = "decide"]
  /\ returned' = [returned EXCEPT ![c] = "none"]
  /\ owner' = c
  /\ UNCHANGED <<change, active, stage, oldLive, successor,
                 attempts, uploaded, closed>>

EndTurn(c) ==
  /\ active[c]
  /\ active' = [active EXCEPT ![c] = FALSE]
  /\ UNCHANGED <<state, change, owner, stage, oldLive, successor, returned,
                 attempts, uploaded, closed>>

Next ==
  \/ \E c \in Callers, k \in Changes, t \in BOOLEAN: Start(c, k, t)
  \/ \E c \in Callers: Acquire(c) \/ Reuse(c) \/ Defer(c) \/ BusyHandoff(c)
                           \/ Update(c) \/ BeginReplacement(c) \/ Retry(c)
                           \/ EndTurn(c)
  \/ Checkpoint \/ Upload \/ CreateSuccessor \/ CloseOut \/ Fail

TypeOK ==
  /\ state \in [Callers -> CallStates]
  /\ change \in [Callers -> Changes]
  /\ active \in [Callers -> BOOLEAN]
  /\ owner \in Callers \cup {NoOwner}
  /\ stage \in Stages
  /\ oldLive \in BOOLEAN /\ successor \in BOOLEAN
  /\ returned \in [Callers -> {"none", "prepared", "deferred", "busy", "failure"}]
  /\ attempts \in Nat
  /\ uploaded \in BOOLEAN /\ closed \in BOOLEAN

SingleLockOwner ==
  /\ owner = NoOwner => \A c \in Callers: state[c] # "decide" /\ state[c] # "work"
  /\ owner \in Callers => state[owner] \in {"decide", "work"}
BoundedAttempts == attempts <= 4
FailurePreservesOld == stage = "failed" => oldLive
CompletedConsistent == stage = "completed" => successor /\ closed /\ ~oldLive
DeferredOnlyWhenActive ==
  \A c \in Callers: returned[c] = "deferred" => change[c] \in {"update", "replace"}
BusyOnlyHandoff == \A c \in Callers: returned[c] = "busy" => change[c] = "handoff"

Spec == Init /\ [][Next]_vars

FairPipeline ==
  /\ Spec
  /\ \A c \in Callers: WF_vars(Acquire(c))
  /\ \A c \in Callers: WF_vars(Reuse(c) \/ Defer(c) \/ BusyHandoff(c)
                                 \/ Update(c) \/ BeginReplacement(c))
  /\ WF_vars(Checkpoint)
  /\ WF_vars(Upload)
  /\ WF_vars(CreateSuccessor)
  /\ WF_vars(CloseOut)
  /\ WF_vars(Fail)
  /\ \A c \in Callers: WF_vars(Retry(c))
  /\ \A c \in Callers: WF_vars(EndTurn(c))

AllStartedEventuallyReturn ==
  \A c \in Callers: (state[c] # "idle") ~> (state[c] = "finished")

==============================================================
