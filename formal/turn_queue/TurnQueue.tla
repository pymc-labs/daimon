------------------------------ MODULE TurnQueue ------------------------------
(***************************************************************************)
(* One adapter process's turn slots and the queue in front of them: a      *)
(* global cap, a per-tenant cap, a bounded per-tenant FIFO queue served    *)
(* round-robin across tenants, the max-wait safeguard, Stop while queued   *)
(* and while running, every end path of a running turn, and a restart     *)
(* that drops the in-process queue and leaves the turn cards to the orphan *)
(* sweep.                                                                  *)
(*                                                                         *)
(* Every action boundary is an await in the asyncio code; everything      *)
(* inside one action runs without yielding. A slot release and the        *)
(* dispatch it owes run in one synchronous span (`owed` blocks every other *)
(* action until Dispatch has run).                                         *)
(*                                                                         *)
(* Source: packages/core/daimon/core/turn/slot_queue.py (TurnSlotQueue).   *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets, TLC

CONSTANTS
    Tenants,          \* tenants (Discord guilds, Slack workspaces, Teams tenants)
    Flooder,          \* the busy tenant
    FloodLoad,        \* turns the busy tenant sends
    OtherLoad,        \* turns each other tenant sends
    GlobalCap,        \* max_concurrent_turns
    FlooderCap,       \* the busy tenant's turn cap (raised for the event)
    OtherCap,         \* max_concurrent_turns_per_tenant
    TenantQueueMax,   \* turn_queue_max_per_tenant
    GlobalQueueMax,   \* turn_queue_max
    MaxRestarts,      \* process restarts in one behavior
    WithMaxWait,      \* the max-wait safeguard may expire a queued turn
    RoundRobin,       \* TRUE: round-robin over tenants; FALSE: one global FIFO by arrival
    ReleaseOnFailure, \* FALSE: the failure end path forgets to return its slot
    AtomicGrant       \* FALSE: dispatch starts the head and pops it after an await

ASSUME GlobalCap >= 1 /\ FlooderCap >= 1 /\ OtherCap >= 1
ASSUME TenantQueueMax >= 0 /\ GlobalQueueMax >= 0
ASSUME Flooder \in Tenants

Load(t) == IF t = Flooder THEN FloodLoad ELSE OtherLoad
Cap(t) == IF t = Flooder THEN FlooderCap ELSE OtherCap
Turns == UNION {{<<t, i>> : i \in 1..Load(t)} : t \in Tenants}
Tenant(x) == x[1]
None == "none"

\* st: where the turn is in this process. "dead": it was queued or running
\* when the process restarted. "ended": it left the queue or finished.
States == {"new", "queued", "running", "ended", "refused", "dead"}
\* card: what the person sees. "working" is the ordinary status card with
\* Stop; a queued turn shows exactly the same card as a running one.
Cards == {"none", "working", "answered", "failed", "stopped", "refused", "restarted"}

VARIABLES
    st,         \* [Turns -> States]
    card,       \* [Turns -> Cards]
    cancelReq,  \* [Turns -> BOOLEAN]: Stop was clicked
    queue,      \* [Tenants -> Seq(Turns)]: per-tenant FIFO
    order,      \* Seq(Tenants): round-robin rotation of tenants with queued turns
    used,       \* [Tenants -> Nat]: the slot counters the code keeps
    starts,     \* [Turns -> Nat]: times the turn body was started
    arrival,    \* [Turns -> Nat]: arrival stamp (for the global-FIFO variant)
    clock,      \* next arrival stamp
    unpopped,   \* turns started by dispatch but still at their queue head (AtomicGrant = FALSE)
    owed,       \* a slot was released and its dispatch has not run yet
    overtaken,  \* [Tenants -> Nat]: other tenants started while this one was eligible
    restarts

vars == <<st, card, cancelReq, queue, order, used, starts, arrival, clock,
          unpopped, owed, overtaken, restarts>>

RECURSIVE SumOver(_, _)
SumOver(f, S) ==
    IF S = {} THEN 0 ELSE LET t == CHOOSE u \in S : TRUE IN f[t] + SumOver(f, S \ {t})

Range(s) == {s[i] : i \in 1..Len(s)}
Remove(s, x) == SelectSeq(s, LAMBDA y : y # x)
GlobalUsed == SumOver(used, Tenants)
QueueDepth == SumOver([t \in Tenants |-> Len(queue[t])], Tenants)
GlobalFree == GlobalUsed < GlobalCap
Eligible(t) == queue[t] # <<>> /\ used[t] < Cap(t)

\* Round robin: the first eligible tenant in rotation order.
RRPick ==
    LET idx == {i \in 1..Len(order) : Eligible(order[i])}
    IN IF idx = {} THEN None
       ELSE order[CHOOSE i \in idx : \A j \in idx : i <= j]

\* Global FIFO: the eligible head that arrived first, whatever its tenant.
FifoPick ==
    LET ts == {t \in Tenants : Eligible(t)}
    IN IF ts = {} THEN None
       ELSE CHOOSE t \in ts : \A u \in ts : arrival[queue[t][1]] <= arrival[queue[u][1]]

Pick == IF RoundRobin THEN RRPick ELSE FifoPick

\* Overtaking bookkeeping for a start of tenant u.
OvertakeAfter(u) ==
    [t \in Tenants |-> IF t = u THEN 0 ELSE IF Eligible(t) THEN overtaken[t] + 1 ELSE 0]

Init ==
    /\ st = [x \in Turns |-> "new"]
    /\ card = [x \in Turns |-> "none"]
    /\ cancelReq = [x \in Turns |-> FALSE]
    /\ queue = [t \in Tenants |-> <<>>]
    /\ order = <<>>
    /\ used = [t \in Tenants |-> 0]
    /\ starts = [x \in Turns |-> 0]
    /\ arrival = [x \in Turns |-> 0]
    /\ clock = 1
    /\ unpopped = {}
    /\ owed = FALSE
    /\ overtaken = [t \in Tenants |-> 0]
    /\ restarts = 0

(***************************************************************************)
(* Admission. A turn runs at once when its tenant and the process both    *)
(* have a free slot and nobody of its tenant is waiting; otherwise it     *)
(* joins its tenant's queue and posts the ordinary card; a full queue is   *)
(* the last resort and refuses with the plain notice.                     *)
(***************************************************************************)
Arrive(x) ==
    LET t == Tenant(x) IN
    /\ ~owed
    /\ st[x] = "new"
    \* Only the global-FIFO variant reads arrival order.
    /\ arrival' = IF RoundRobin THEN arrival ELSE [arrival EXCEPT ![x] = clock]
    /\ clock' = IF RoundRobin THEN clock ELSE clock + 1
    /\ IF used[t] < Cap(t) /\ GlobalFree /\ queue[t] = <<>>
       THEN /\ st' = [st EXCEPT ![x] = "running"]
            /\ card' = [card EXCEPT ![x] = "working"]
            /\ used' = [used EXCEPT ![t] = @ + 1]
            /\ starts' = [starts EXCEPT ![x] = @ + 1]
            /\ overtaken' = OvertakeAfter(t)
            /\ UNCHANGED <<queue, order>>
       ELSE IF Len(queue[t]) < TenantQueueMax /\ QueueDepth < GlobalQueueMax
       THEN /\ st' = [st EXCEPT ![x] = "queued"]
            /\ card' = [card EXCEPT ![x] = "working"]
            /\ queue' = [queue EXCEPT ![t] = Append(@, x)]
            /\ order' = IF t \in Range(order) THEN order ELSE Append(order, t)
            /\ UNCHANGED <<used, starts, overtaken>>
       ELSE /\ st' = [st EXCEPT ![x] = "refused"]
            /\ card' = [card EXCEPT ![x] = "refused"]
            /\ UNCHANGED <<queue, order, used, starts, overtaken>>
    /\ UNCHANGED <<cancelReq, unpopped, owed, restarts>>

(***************************************************************************)
(* Dispatch: the synchronous span after a slot release. Starts the picked *)
(* tenant's head and moves that tenant to the back of the rotation.       *)
(***************************************************************************)
Dispatch ==
    /\ owed
    /\ owed' = FALSE
    /\ LET t == Pick IN
       IF GlobalFree /\ t # None
       THEN LET x == queue[t][1]
                rest == IF AtomicGrant THEN Tail(queue[t]) ELSE queue[t]
                rotated == Remove(order, t)
            IN /\ st' = [st EXCEPT ![x] = "running"]
               /\ used' = [used EXCEPT ![t] = @ + 1]
               /\ starts' = [starts EXCEPT ![x] = @ + 1]
               /\ overtaken' = OvertakeAfter(t)
               /\ queue' = [queue EXCEPT ![t] = rest]
               /\ order' = IF rest = <<>> THEN rotated ELSE Append(rotated, t)
               /\ unpopped' = IF AtomicGrant THEN unpopped ELSE unpopped \cup {x}
       ELSE UNCHANGED <<st, used, starts, overtaken, queue, order, unpopped>>
    /\ UNCHANGED <<card, cancelReq, arrival, clock, restarts>>

\* AtomicGrant = FALSE only: the started head is popped after an await.
Pop(x) ==
    LET t == Tenant(x) IN
    /\ ~owed
    /\ x \in unpopped
    /\ unpopped' = unpopped \ {x}
    /\ queue' = [queue EXCEPT ![t] = IF @ # <<>> THEN Tail(@) ELSE @]
    /\ order' = IF queue'[t] = <<>> THEN Remove(order, t) ELSE order
    /\ UNCHANGED <<st, card, cancelReq, used, starts, arrival, clock, owed, overtaken, restarts>>

\* The Stop button: sets the turn's cancel event, queued or running.
Stop(x) ==
    /\ ~owed
    /\ st[x] \in {"queued", "running"}
    /\ ~cancelReq[x]
    /\ cancelReq' = [cancelReq EXCEPT ![x] = TRUE]
    /\ UNCHANGED <<st, card, queue, order, used, starts, arrival, clock, unpopped, owed,
                   overtaken, restarts>>

\* Leave the queue without a slot: the waiter woke on Stop, or the max
\* wait passed. Removal and the card's end happen in one span.
Leave(x, how) ==
    LET t == Tenant(x)
        rest == Remove(queue[t], x)
    IN
    /\ ~owed
    /\ st[x] = "queued"
    /\ st' = [st EXCEPT ![x] = "ended"]
    /\ card' = [card EXCEPT ![x] = how]
    /\ cancelReq' = [cancelReq EXCEPT ![x] = FALSE]
    /\ queue' = [queue EXCEPT ![t] = rest]
    /\ order' = IF rest = <<>> THEN Remove(order, t) ELSE order
    /\ overtaken' = IF rest = <<>> THEN [overtaken EXCEPT ![t] = 0] ELSE overtaken
    /\ UNCHANGED <<used, starts, arrival, clock, unpopped, owed, restarts>>

Withdraw(x) == cancelReq[x] /\ Leave(x, "stopped")
Expire(x) == WithMaxWait /\ Leave(x, "failed")

(***************************************************************************)
(* Every end path of a running turn: an answer, a failure, the turn       *)
(* ceiling, and Stop. Each returns the slot (unless the unsafe switch     *)
(* drops it on failure) and owes a dispatch.                               *)
(***************************************************************************)
EndPaths == {"answered", "failed", "ceiling", "stopped"}

Finish(x, how) ==
    LET t == Tenant(x)
        returns == how # "failed" \/ ReleaseOnFailure
    IN
    /\ ~owed
    /\ st[x] = "running"
    /\ how = "stopped" => cancelReq[x]
    /\ st' = [st EXCEPT ![x] = "ended"]
    /\ card' = [card EXCEPT ![x] = IF how = "ceiling" THEN "failed" ELSE how]
    /\ used' = IF returns THEN [used EXCEPT ![t] = @ - 1] ELSE used
    /\ owed' = returns
    /\ cancelReq' = [cancelReq EXCEPT ![x] = FALSE]
    /\ UNCHANGED <<queue, order, starts, arrival, clock, unpopped, overtaken, restarts>>

(***************************************************************************)
(* Restart: the in-process queue and counters are gone; queued and        *)
(* running turns are dead with their cards still "working". The boot      *)
(* sweep (turn_card_recovery, turn.orphans_found) retires each card.      *)
(***************************************************************************)
Restart ==
    /\ ~owed
    /\ restarts < MaxRestarts
    /\ \E x \in Turns : st[x] \in {"queued", "running"}
    /\ st' = [x \in Turns |-> IF st[x] \in {"queued", "running"} THEN "dead" ELSE st[x]]
    /\ queue' = [t \in Tenants |-> <<>>]
    /\ order' = <<>>
    /\ used' = [t \in Tenants |-> 0]
    /\ unpopped' = {}
    /\ overtaken' = [t \in Tenants |-> 0]
    /\ restarts' = restarts + 1
    /\ UNCHANGED <<card, cancelReq, starts, arrival, clock, owed>>

Recover(x) ==
    /\ ~owed
    /\ st[x] = "dead"
    /\ card[x] = "working"
    /\ card' = [card EXCEPT ![x] = "restarted"]
    /\ UNCHANGED <<st, cancelReq, queue, order, used, starts, arrival, clock, unpopped, owed,
                   overtaken, restarts>>

Next ==
    \/ \E x \in Turns : Arrive(x) \/ Stop(x) \/ Withdraw(x) \/ Expire(x) \/ Recover(x) \/ Pop(x)
    \/ \E x \in Turns, how \in EndPaths : Finish(x, how)
    \/ Dispatch
    \/ Restart

\* Fairness: a released slot is dispatched, a running turn ends somehow,
\* a stopped waiter wakes, a started head is popped, the boot sweep runs.
\* No fairness on arrivals, Stop, the max wait or restarts.
Fairness ==
    /\ WF_vars(Dispatch)
    /\ \A x \in Turns :
        /\ WF_vars(\E how \in EndPaths : Finish(x, how))
        /\ WF_vars(Withdraw(x))
        /\ WF_vars(Pop(x))
        /\ WF_vars(Recover(x))

Spec == Init /\ [][Next]_vars /\ Fairness

-----------------------------------------------------------------------------
TypeOK ==
    /\ st \in [Turns -> States]
    /\ card \in [Turns -> Cards]
    /\ cancelReq \in [Turns -> BOOLEAN]
    /\ \A t \in Tenants : queue[t] \in Seq(Turns)
    /\ used \in [Tenants -> Nat]
    /\ starts \in [Turns -> Nat]
    /\ unpopped \subseteq Turns
    /\ owed \in BOOLEAN

\* No turn runs twice.
NoDoubleStart == \A x \in Turns : starts[x] <= 1

\* No slot leaks: the counters always equal the turns actually running.
SlotAccounting ==
    \A t \in Tenants : used[t] = Cardinality({x \in Turns : Tenant(x) = t /\ st[x] = "running"})

\* Slots in use never exceed either cap.
WithinCaps == GlobalUsed <= GlobalCap /\ \A t \in Tenants : used[t] <= Cap(t)

\* Queue depth stays bounded, per tenant and in total.
QueueBounded == QueueDepth <= GlobalQueueMax /\ \A t \in Tenants : Len(queue[t]) <= TenantQueueMax

\* No turn is lost: a queued turn sits exactly once in its own tenant's
\* queue, every queue entry is a queued turn of that tenant, and the
\* rotation holds exactly the tenants with queued turns.
NoLostTurn ==
    /\ \A x \in Turns : st[x] = "queued" =>
        Cardinality({i \in 1..Len(queue[Tenant(x)]) : queue[Tenant(x)][i] = x}) = 1
    /\ \A t \in Tenants : \A i \in 1..Len(queue[t]) :
        Tenant(queue[t][i]) = t /\ (st[queue[t][i]] = "queued" \/ queue[t][i] \in unpopped)
    /\ Range(order) = {t \in Tenants : queue[t] # <<>>}

\* The card tells the truth: "working" exactly while the turn is queued or
\* running, or dead and not yet swept.
CardTruth ==
    \A x \in Turns :
        /\ st[x] \in {"queued", "running"} => card[x] = "working"
        /\ card[x] = "working" => st[x] \in {"queued", "running", "dead"}

\* Work conservation: once the dispatch has run, no eligible turn waits
\* while the process has a free slot.
WorkConserving == ~owed => ~(GlobalFree /\ \E t \in Tenants : Eligible(t))

\* Round-robin fairness: while a tenant has an eligible queued turn, at most
\* one start per other tenant happens before it is served.
NoStarvation == \A t \in Tenants : overtaken[t] <= Cardinality(Tenants) - 1

\* Reachability witness: a config whose run violates this shows that the
\* plain refusal is reachable (only from a full queue, by Arrive).
NoRefusal == \A x \in Turns : st[x] # "refused"

\* Liveness: every queued turn starts, is stopped, times out, or dies in a
\* restart (and is then swept).
QueuedResolves ==
    \A x \in Turns : st[x] = "queued" ~>
        (starts[x] > 0 \/ card[x] \in {"stopped", "failed"} \/ st[x] = "dead")

\* Liveness: no card stays "working" forever, restarts included.
CardsSettle == \A x \in Turns : card[x] = "working" ~> card[x] # "working"

\* Liveness: every slot taken is returned.
SlotsReturned == <>[](\A t \in Tenants : used[t] = 0)
=============================================================================
