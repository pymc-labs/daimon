---------------- MODULE DefaultsReconcile ----------------
EXTENDS Naturals, FiniteSets, TLC

\* Bounded two-call model of the list/create/sweep race in
\* core/defaults/reconcile_agents.py, the tenant lock in
\* core/defaults/_reconcile.py, and resolver ID caching in core/ma_resolver.py.
\* Resource IDs stand for MA agent IDs; the resolver cache is process-local.
Callers == {"a", "b"}
Ids == {"agent_a", "agent_b"}
NoOwner == "none"

CONSTANT Locking

VARIABLES phase, sawEmpty, resources, archived, cache, owner
vars == <<phase, sawEmpty, resources, archived, cache, owner>>

Init ==
  /\ phase = [c \in Callers |-> "ready"]
  /\ sawEmpty = [c \in Callers |-> FALSE]
  /\ resources = {}
  /\ archived = {}
  /\ cache = "none"
  /\ owner = NoOwner

CanEnter(c) == ~Locking \/ owner = NoOwner \/ owner = c

ListEmpty(c) ==
  /\ phase[c] = "ready" /\ CanEnter(c)
  /\ (Locking => owner = NoOwner)
  /\ resources = {}
  /\ phase' = [phase EXCEPT ![c] = "listed"]
  /\ sawEmpty' = [sawEmpty EXCEPT ![c] = (resources = {})]
  /\ owner' = IF Locking THEN c ELSE owner
  /\ UNCHANGED <<resources, archived, cache>>

ListExisting(c) ==
  /\ phase[c] = "ready" /\ CanEnter(c)
  /\ (Locking => owner = NoOwner)
  /\ resources # {}
  /\ phase' = [phase EXCEPT ![c] = "done"]
  /\ sawEmpty' = [sawEmpty EXCEPT ![c] = FALSE]
  /\ owner' = IF Locking THEN NoOwner ELSE owner
  /\ UNCHANGED <<resources, archived, cache>>

Create(c) ==
  /\ phase[c] = "listed" /\ sawEmpty[c]
  /\ (\A d \in Callers : Locking /\ d # c /\ phase[d] = "listed" => FALSE)
  /\ phase' = [phase EXCEPT ![c] = "created"]
  /\ resources' = resources \cup {IF c = "a" THEN "agent_a" ELSE "agent_b"}
  /\ UNCHANGED <<sawEmpty, archived, cache, owner>>

Finish(c) ==
  /\ phase[c] = "created"
  /\ phase' = [phase EXCEPT ![c] = "done"]
  /\ owner' = IF Locking /\ owner = c THEN NoOwner ELSE owner
  /\ UNCHANGED <<sawEmpty, resources, archived, cache>>

Resolve ==
  /\ cache = "none" /\ "agent_a" \in resources
  /\ cache' = "agent_a"
  /\ UNCHANGED <<phase, sawEmpty, resources, archived, owner>>

\* A later reconcile sees both matches; reconcile_agent keeps the newest
\* match and archives the older one (matches[1:]).
SweepDuplicate ==
  /\ resources = Ids
  /\ phase["a"] = "done" /\ phase["b"] = "done"
  /\ "agent_a" \in resources /\ "agent_b" \in resources
  /\ cache = "agent_a"
  /\ resources' = {"agent_b"}
  /\ archived' = archived \cup {"agent_a"}
  /\ UNCHANGED <<phase, sawEmpty, cache, owner>>

Next ==
  \/ \E c \in Callers : ListEmpty(c)
  \/ \E c \in Callers : ListExisting(c)
  \/ \E c \in Callers : Create(c)
  \/ \E c \in Callers : Finish(c)
  \/ Resolve
  \/ SweepDuplicate

TypeOK ==
  /\ phase \in [Callers -> {"ready", "listed", "created", "done"}]
  /\ sawEmpty \in [Callers -> BOOLEAN]
  /\ resources \subseteq Ids
  /\ archived \subseteq Ids
  /\ cache \in Ids \cup {"none"}
  /\ owner \in Callers \cup {NoOwner}

CachedIdLive == cache = "none" \/ cache \in resources
SingleOwner == Locking => Cardinality({c \in Callers : phase[c] \in {"listed", "created"}}) <= 1

Spec == Init /\ [][Next]_vars
===============================================================
