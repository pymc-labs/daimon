---------------------------- MODULE SlackDedupe ----------------------------
(***************************************************************************)
(* Slack app_mention admission for one thread: Socket Mode delivery and    *)
(* redelivery, ack-first dispatch, the draining check, the committed       *)
(* slack_event_dedup insert, the in-memory per-thread queue and drain loop *)
(* (partitioned by author), the TTL prune, and adapter process exit.       *)
(*                                                                         *)
(* A handler task is spawned per delivery after the ack. Mentions are      *)
(* identified by their dedupe triple (team, channel, event_ts). A          *)
(* redelivery (same triple, fresh envelope) may arrive while the first     *)
(* handler is still running or after it finished, but only within Window  *)
(* ticks of the first delivery. The prune deletes rows older than          *)
(* Retention ticks.                                                        *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets, Sequences, TLC

CONSTANTS
    MaxRedeliveries,   \* redeliveries Slack makes in total
    Window,            \* ticks after the first delivery during which Slack redelivers
    Retention,         \* ticks a dedupe row survives the prune
    MaxClock,
    PartitionByAuthor, \* drain runs one turn per author (997b3d8)
    NotifyOnFailure,   \* a failed turn posts an error into the thread (0cda77e)
    AllowExit,         \* the adapter process may crash or finish a SIGTERM drain
    AllowDrain         \* SIGTERM sets the draining flag before the process exits

\* Three distinct user messages in one thread: A, then B, then A again.
Mentions == {1, 2, 3}
Author(m) == IF m = 2 THEN "B" ELSE "A"

Deliveries == 1..(Cardinality(Mentions) + MaxRedeliveries)

VARIABLES
    clock,
    firstAt,      \* [Mentions -> clock of first delivery, or MaxClock + 1 if none]
    handler,      \* [Deliveries -> <<mention, phase>>]; phase "none" = unused slot
    redelivered,  \* redeliveries used
    dedup,        \* [Mentions -> clock the row was inserted, or Never]
    running,      \* mentions in the turn currently running ({} = thread idle)
    groups,       \* drained author groups still to run
    busy,         \* thread_id \in self._processing
    pending,      \* self._pending[thread_id]
    turns,        \* set of [author, members] turns that ran
    notified,     \* mentions answered with an error notice
    draining,
    alive

vars == <<clock, firstAt, handler, redelivered, dedup, running, groups, busy, pending,
          turns, notified, draining, alive>>

Never == MaxClock + 1
Phases == {"none", "spawned", "checked", "admitted", "done", "dropped", "lost"}

Init ==
    /\ clock = 0
    /\ firstAt = [m \in Mentions |-> Never]
    /\ handler = [d \in Deliveries |-> <<CHOOSE m \in Mentions : TRUE, "none">>]
    /\ redelivered = 0
    /\ dedup = [m \in Mentions |-> Never]
    /\ running = {}
    /\ groups = <<>>
    /\ busy = FALSE
    /\ pending = <<>>
    /\ turns = {}
    /\ notified = {}
    /\ draining = FALSE
    /\ alive = TRUE

FreeSlot == CHOOSE d \in Deliveries : handler[d][2] = "none"
HasFreeSlot == \E d \in Deliveries : handler[d][2] = "none"
Delivered(m) == firstAt[m] # Never
SetPhase(d, p) == handler' = [handler EXCEPT ![d] = <<handler[d][1], p>>]

(* Time moves in ticks far longer than a handler's path from the ack to   *)
(* insert_if_new (milliseconds; at boot, the orphan-recovery wait). A tick *)
(* therefore never passes while a delivery has not yet reached the insert. *)
Tick ==
    /\ clock < MaxClock
    /\ \A d \in Deliveries : handler[d][2] \notin {"spawned", "checked"}
    /\ clock' = clock + 1
    /\ UNCHANGED <<firstAt, handler, redelivered, dedup, running, groups, busy, pending,
                   turns, notified, draining, alive>>

(* Slack delivers a user's mention; on_request acks, then spawns the handler. *)
Deliver(m) ==
    /\ alive
    /\ ~Delivered(m)
    /\ HasFreeSlot
    /\ firstAt' = [firstAt EXCEPT ![m] = clock]
    /\ handler' = [handler EXCEPT ![FreeSlot] = <<m, "spawned">>]
    /\ UNCHANGED <<clock, redelivered, dedup, running, groups, busy, pending, turns,
                   notified, draining, alive>>

(* Same logical event, fresh envelope_id: a lost ack, a reconnect, or a     *)
(* second Socket Mode connection.                                          *)
Redeliver(m) ==
    /\ alive
    /\ Delivered(m)
    /\ clock - firstAt[m] < Window
    /\ redelivered < MaxRedeliveries
    /\ HasFreeSlot
    /\ handler' = [handler EXCEPT ![FreeSlot] = <<m, "spawned">>]
    /\ redelivered' = redelivered + 1
    /\ UNCHANGED <<clock, firstAt, dedup, running, groups, busy, pending, turns, notified,
                   draining, alive>>

(* _handle_app_mention step 1: the draining fast path (no I/O). *)
CheckDraining(d) ==
    /\ alive
    /\ handler[d][2] = "spawned"
    /\ SetPhase(d, IF draining THEN "dropped" ELSE "checked")
    /\ UNCHANGED <<clock, firstAt, redelivered, dedup, running, groups, busy, pending,
                   turns, notified, draining, alive>>

(* Step 2: insert_if_new + commit; a conflict drops the delivery. *)
Dedupe(d) ==
    /\ alive
    /\ handler[d][2] = "checked"
    /\ LET m == handler[d][1] IN
       IF dedup[m] = Never
       THEN /\ dedup' = [dedup EXCEPT ![m] = clock]
            /\ SetPhase(d, "admitted")
       ELSE /\ SetPhase(d, "done")
            /\ UNCHANGED dedup
    /\ UNCHANGED <<clock, firstAt, redelivered, running, groups, busy, pending, turns,
                   notified, draining, alive>>

(* _orchestrate: queue behind a running turn, or take the thread. *)
Orchestrate(d) ==
    /\ alive
    /\ handler[d][2] = "admitted"
    /\ LET m == handler[d][1] IN
       IF busy
       THEN /\ pending' = Append(pending, m)
            /\ UNCHANGED <<running, busy>>
       ELSE /\ busy' = TRUE
            /\ running' = {m}
            /\ UNCHANGED pending
    /\ SetPhase(d, "done")
    /\ UNCHANGED <<clock, firstAt, redelivered, dedup, groups, turns, notified, draining,
                   alive>>

AuthorOf(S) == Author(CHOOSE m \in S : TRUE)
Elems(s) == {s[i] : i \in 1..Len(s)}

(* Partition the popped queue by author in first-seen order, or merge it   *)
(* under the first event's author (the pre-997b3d8 shape).                 *)
RECURSIVE Partition(_, _)
Partition(s, acc) ==
    IF s = <<>> THEN acc
    ELSE LET m == Head(s)
             idx == {i \in 1..Len(acc) : Author(CHOOSE x \in acc[i] : TRUE) = Author(m)}
         IN IF idx = {}
            THEN Partition(Tail(s), Append(acc, {m}))
            ELSE LET i == CHOOSE i \in idx : TRUE
                 IN Partition(Tail(s), [acc EXCEPT ![i] = @ \cup {m}])

\* A merged pre-fix turn runs as the first queued event's author.
TurnRecord(S, a) == [author |-> a, members |-> S]

(* The first turn ends. Success records the turn. Failure posts an error   *)
(* for its mention (post-0cda77e), skips the drain loop, and the finally   *)
(* block tells every queued mention to try again.                          *)
FirstTurnEnds(ok) ==
    /\ alive
    /\ running # {}
    /\ groups = <<>>
    /\ running' = {}
    /\ IF ok
       THEN /\ turns' = turns \cup {TurnRecord(running, AuthorOf(running))}
            /\ UNCHANGED <<busy, pending, notified>>
       ELSE /\ notified' = (IF NotifyOnFailure THEN notified \cup running ELSE notified)
                             \cup Elems(pending)
            /\ pending' = <<>>
            /\ busy' = FALSE
            /\ UNCHANGED turns
    /\ UNCHANGED <<clock, firstAt, handler, redelivered, dedup, groups, draining, alive>>

(* while queued := self._pending.pop(thread_id, []) *)
PopQueue ==
    /\ alive
    /\ busy
    /\ running = {}
    /\ groups = <<>>
    /\ pending # <<>>
    /\ groups' = IF PartitionByAuthor
                 THEN LET P == Partition(pending, <<>>)
                      IN [i \in 1..Len(P) |-> <<P[i], AuthorOf(P[i])>>]
                 ELSE <<<<Elems(pending), Author(Head(pending))>>>>
    /\ pending' = <<>>
    /\ UNCHANGED <<clock, firstAt, handler, redelivered, dedup, running, busy, turns,
                   notified, draining, alive>>

(* One drained group's turn; failures are isolated and notified. *)
DrainTurn(ok) ==
    /\ alive
    /\ running = {}
    /\ groups # <<>>
    /\ LET g == Head(groups) IN
       /\ turns' = IF ok THEN turns \cup {TurnRecord(g[1], g[2])} ELSE turns
       /\ notified' = IF ok \/ ~NotifyOnFailure THEN notified ELSE notified \cup g[1]
    /\ groups' = Tail(groups)
    /\ UNCHANGED <<clock, firstAt, handler, redelivered, dedup, running, busy, pending,
                   draining, alive>>

(* The finally block: release the thread once nothing is queued. *)
Release ==
    /\ alive
    /\ busy
    /\ running = {}
    /\ groups = <<>>
    /\ pending = <<>>
    /\ busy' = FALSE
    /\ UNCHANGED <<clock, firstAt, handler, redelivered, dedup, running, groups, pending,
                   turns, notified, draining, alive>>

(* slack_event_dedup_sweep: delete rows older than Retention. *)
Prune(m) ==
    /\ dedup[m] # Never
    /\ clock - dedup[m] >= Retention
    /\ dedup' = [dedup EXCEPT ![m] = Never]
    /\ UNCHANGED <<clock, firstAt, handler, redelivered, running, groups, busy, pending,
                   turns, notified, draining, alive>>

(* SIGTERM: drain_and_close sets the flag ... *)
StartDrain ==
    /\ AllowDrain
    /\ alive
    /\ ~draining
    /\ draining' = TRUE
    /\ UNCHANGED <<clock, firstAt, handler, redelivered, dedup, running, groups, busy,
                   pending, turns, notified, alive>>

(* ... and exits once _processing is empty (or the grace window ends), or  *)
(* the process simply crashes. In-memory work dies; acked events are not   *)
(* redelivered; the dedupe rows stay. A replacement process takes over.    *)
Exit ==
    /\ AllowExit
    /\ alive
    /\ (AllowDrain => draining)
    \* drain_and_close awaits (sleep/close) before exit, so an already
    \* spawned handler runs its synchronous draining check first.
    /\ (AllowDrain => \A d \in Deliveries : handler[d][2] # "spawned")
    /\ handler' = [d \in Deliveries |->
                     IF handler[d][2] \in {"spawned", "checked", "admitted"}
                     THEN <<handler[d][1], "lost">> ELSE handler[d]]
    /\ running' = {}
    /\ groups' = <<>>
    /\ busy' = FALSE
    /\ pending' = <<>>
    /\ draining' = FALSE
    /\ alive' = FALSE
    /\ UNCHANGED <<clock, firstAt, redelivered, dedup, turns, notified>>

Restart ==
    /\ ~alive
    /\ alive' = TRUE
    /\ UNCHANGED <<clock, firstAt, handler, redelivered, dedup, running, groups, busy,
                   pending, turns, notified, draining>>

Quiescent ==
    /\ alive
    /\ \A d \in Deliveries : handler[d][2] \notin {"spawned", "checked", "admitted"}
    /\ ~busy

Done ==
    /\ Quiescent
    /\ \A m \in Mentions : Delivered(m)
    /\ clock = MaxClock
    /\ UNCHANGED vars

Next ==
    \/ Done \/ Tick \/ PopQueue \/ Release \/ StartDrain \/ Exit \/ Restart
    \/ \E ok \in BOOLEAN : FirstTurnEnds(ok) \/ DrainTurn(ok)
    \/ \E m \in Mentions : Deliver(m) \/ Redeliver(m) \/ Prune(m)
    \/ \E d \in Deliveries : CheckDraining(d) \/ Dedupe(d) \/ Orchestrate(d)

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
TypeOK ==
    /\ handler \in [Deliveries -> Mentions \X Phases]
    /\ running \subseteq Mentions
    /\ busy \in BOOLEAN
    /\ notified \subseteq Mentions

TurnsWith(m) == {t \in turns : m \in t.members}
Answered(m) == TurnsWith(m) # {} \/ m \in notified

(* A delivered mention produces at most one turn. *)
AtMostOneTurn == \A m \in Mentions : Cardinality(TurnsWith(m)) <= 1

(* One turn = one caller: a turn runs only its own author's mentions. *)
TurnPrincipal == \A t \in turns : \A m \in t.members : Author(m) = t.author

(* Once the adapter is idle, every delivered mention got a turn or a notice. *)
NoSilentLoss == Quiescent => \A m \in Mentions : Delivered(m) => Answered(m)

(* The documented drop (IN-02): a mention the draining check rejected.     *)
DroppedByDrainCheck(m) == \E d \in Deliveries : handler[d] = <<m, "dropped">>

(* NoSilentLoss minus the documented drain-window drop. *)
NoUndocumentedLoss ==
    Quiescent => \A m \in Mentions : Delivered(m) => (Answered(m) \/ DroppedByDrainCheck(m))
=============================================================================
