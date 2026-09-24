----------------------------- MODULE ThreadQueue -----------------------------
(***************************************************************************)
(* One adapter process, one platform thread: the in-memory per-thread      *)
(* admission state (_processing, _pending), the drain loop, the turn tail   *)
(* (clear the active-turn marker, dispatch queued task continuations) and   *)
(* continuation dispatch requested from outside a turn (a credential form   *)
(* submission). Every action boundary is an await point in the asyncio      *)
(* code; everything inside one action runs without yielding.                *)
(*                                                                         *)
(* Sources: packages/adapters/slack/daimon/adapters/slack/app.py            *)
(* (_orchestrate, _run_thread_turn tail, dispatch_continuations_in_thread), *)
(* packages/adapters/discord/daimon/adapters/discord/bot.py (on_message     *)
(* queueing, _drain_pending_mentions, dispatch_continuations_in_thread),    *)
(* packages/core/daimon/core/session_preparation.py (PreparationBusy).      *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets, TLC

CONSTANTS
    AppendBeforeReact,      \* Slack WR-05 / Discord #228: enqueue before awaiting the ⌛ reaction
    PartitionByAuthor,      \* 997b3d8: the drain runs one turn per author
    ClearBeforeDispatch,    \* 090ecc1: the tail clears its marker before dispatching
    RedispatchAfterRelease, \* #233 (merged): a dispatch skipped while processing reruns at release
    WithHandoff,            \* the first turn may queue a handoff continuation
    WithForm                \* a credential form may record + dispatch a continuation

Msgs == <<"m1", "m2", "m3">>
MsgSet == {"m1", "m2", "m3"}
Author == [m \in MsgSet |-> IF m = "m2" THEN "B" ELSE "A"]
None == "none"

VARIABLES
    processing,  \* thread_id in _processing
    pending,     \* _pending[thread_id]
    reacting,    \* handlers suspended in the ⌛ reaction await before enqueueing
    arrived,     \* messages delivered to the adapter
    phase,       \* idle | turn | tail1 | tail2 | posttail
    batch,       \* messages the running turn answers
    principal,   \* whose turn it is
    groups,      \* drained per-turn batches still to run
    handledBy,   \* [m -> principal of the turn that answered m]
    marker,      \* active-turn marker on the thread's session row
    handoff,     \* none | pending | ran_src | ran_dst
    formPc,      \* none | recorded | done
    formCont,    \* none | pending | done
    redispatch   \* a dispatch was skipped because the thread was processing

vars == <<processing, pending, reacting, arrived, phase, batch, principal, groups,
          handledBy, marker, handoff, formPc, formCont, redispatch>>

Init ==
    /\ processing = FALSE
    /\ pending = <<>>
    /\ reacting = {}
    /\ arrived = {}
    /\ phase = "idle"
    /\ batch = {}
    /\ principal = None
    /\ groups = <<>>
    /\ handledBy = [m \in MsgSet |-> None]
    /\ marker = FALSE
    /\ handoff = "none"
    /\ formPc = IF WithForm THEN "none" ELSE "done"
    /\ formCont = "none"
    /\ redispatch = FALSE

Range(s) == {s[i] : i \in 1..Len(s)}
\* Messages arrive in order m1, m2, m3.
NextMsg == IF Cardinality(arrived) < 3 THEN Msgs[Cardinality(arrived) + 1] ELSE None

\* Split a queue into per-author batches in first-seen order, or one batch
\* answered as the first author.
RECURSIVE AuthorsInOrder(_, _)
AuthorsInOrder(q, seen) ==
    IF q = <<>> THEN <<>>
    ELSE IF Author[Head(q)] \in seen THEN AuthorsInOrder(Tail(q), seen)
         ELSE <<Author[Head(q)]>> \o AuthorsInOrder(Tail(q), seen \cup {Author[Head(q)]})
Partition(q) ==
    IF PartitionByAuthor
       THEN LET as == AuthorsInOrder(q, {}) IN
            [i \in 1..Len(as) |-> [who |-> as[i], msgs |-> {m \in Range(q) : Author[m] = as[i]}]]
       ELSE <<[who |-> Author[Head(q)], msgs |-> Range(q)]>>

(* ---- a mention arrives ---- *)
Arrive ==
    LET m == NextMsg IN
    /\ m # None
    /\ arrived' = arrived \cup {m}
    /\ IF ~processing
          THEN \* claim the thread and run the turn
               /\ processing' = TRUE
               /\ phase' = "turn" /\ batch' = {m} /\ principal' = Author[m]
               /\ marker' = TRUE
               /\ UNCHANGED <<pending, reacting>>
          ELSE /\ IF AppendBeforeReact
                     THEN /\ pending' = Append(pending, m) /\ UNCHANGED reacting
                     ELSE /\ reacting' = reacting \cup {m} /\ UNCHANGED pending
               /\ UNCHANGED <<processing, phase, batch, principal, marker>>
    /\ UNCHANGED <<groups, handledBy, handoff, formPc, formCont, redispatch>>

\* The ⌛ reaction await returns; the old order enqueues only now.
ReactDone(m) ==
    /\ m \in reacting
    /\ reacting' = reacting \ {m}
    /\ pending' = Append(pending, m)
    /\ UNCHANGED <<processing, arrived, phase, batch, principal, groups, handledBy, marker,
                   handoff, formPc, formCont, redispatch>>

(* ---- the running turn ---- *)
QueueHandoff ==
    /\ WithHandoff /\ phase = "turn" /\ handoff = "none"
    /\ handoff' = "pending"
    /\ UNCHANGED <<processing, pending, reacting, arrived, phase, batch, principal, groups,
                   handledBy, marker, formPc, formCont, redispatch>>

TurnEnd ==
    /\ phase = "turn"
    /\ handledBy' = [m \in MsgSet |-> IF m \in batch THEN principal ELSE handledBy[m]]
    /\ phase' = "tail1"
    /\ UNCHANGED <<processing, pending, reacting, arrived, batch, principal, groups, marker,
                   handoff, formPc, formCont, redispatch>>

\* Dispatch the thread's pending continuations. A handoff changes the responder:
\* with the marker still set, bind treats a turn as running and the handoff's
\* first turn runs in the outgoing agent's session (090ecc1's bug).
DispatchAll(markerNow) ==
    /\ handoff' = IF handoff = "pending" THEN (IF markerNow THEN "ran_src" ELSE "ran_dst") ELSE handoff
    /\ formCont' = IF formCont = "pending" THEN "done" ELSE formCont

\* Tail step 1: clear-then-dispatch (fixed) or dispatch (old order).
Tail1 ==
    /\ phase = "tail1"
    /\ IF ClearBeforeDispatch
          THEN /\ marker' = FALSE /\ UNCHANGED <<handoff, formCont>>
          ELSE /\ DispatchAll(marker) /\ UNCHANGED marker
    /\ phase' = "tail2"
    /\ UNCHANGED <<processing, pending, reacting, arrived, batch, principal, groups, handledBy,
                   formPc, redispatch>>

Tail2 ==
    /\ phase = "tail2"
    /\ IF ClearBeforeDispatch
          THEN /\ DispatchAll(marker) /\ UNCHANGED marker
          ELSE /\ marker' = FALSE /\ UNCHANGED <<handoff, formCont>>
    /\ phase' = "posttail"
    /\ UNCHANGED <<processing, pending, reacting, arrived, batch, principal, groups, handledBy,
                   formPc, redispatch>>

\* Back in _orchestrate: next drained batch, else pop the queue, else release.
\* No await between the empty check and the `finally` that releases the thread.
DrainOrRelease ==
    /\ phase = "posttail"
    /\ IF groups # <<>>
          THEN /\ phase' = "turn" /\ batch' = Head(groups).msgs /\ principal' = Head(groups).who
               /\ groups' = Tail(groups) /\ marker' = TRUE
               /\ UNCHANGED <<pending, processing, handoff, formCont, redispatch>>
          ELSE IF pending # <<>>
             THEN LET gs == Partition(pending) IN
                  /\ phase' = "turn" /\ batch' = Head(gs).msgs /\ principal' = Head(gs).who
                  /\ groups' = Tail(gs) /\ pending' = <<>> /\ marker' = TRUE
                  /\ UNCHANGED <<processing, handoff, formCont, redispatch>>
             ELSE \* finally: discard the slot and drop anything left in _pending
                  /\ processing' = FALSE /\ phase' = "idle" /\ batch' = {} /\ principal' = None
                  /\ IF RedispatchAfterRelease /\ redispatch
                        THEN /\ DispatchAll(FALSE) /\ redispatch' = FALSE
                        ELSE UNCHANGED <<handoff, formCont, redispatch>>
                  /\ UNCHANGED <<pending, groups, marker>>
    /\ UNCHANGED <<reacting, arrived, handledBy, formPc>>

(* ---- a credential form submission for this thread ---- *)
FormRecord ==
    /\ formPc = "none"
    /\ formCont' = "pending"
    /\ formPc' = "recorded"
    /\ UNCHANGED <<processing, pending, reacting, arrived, phase, batch, principal, groups,
                   handledBy, marker, handoff, redispatch>>

\* dispatch_continuations_in_thread: skipped outright while processing.
FormDispatch ==
    /\ formPc = "recorded"
    /\ IF processing
          THEN /\ redispatch' = (redispatch \/ RedispatchAfterRelease)
               /\ UNCHANGED <<formCont, handoff>>
          ELSE /\ DispatchAll(FALSE) /\ UNCHANGED redispatch
    /\ formPc' = "done"
    /\ UNCHANGED <<processing, pending, reacting, arrived, phase, batch, principal, groups,
                   handledBy, marker>>

Quiescent == phase = "idle" /\ reacting = {} /\ formPc = "done" /\ NextMsg = None

Stutter == Quiescent /\ UNCHANGED vars

Next ==
    \/ Arrive \/ (\E m \in MsgSet : ReactDone(m))
    \/ QueueHandoff \/ TurnEnd \/ Tail1 \/ Tail2 \/ DrainOrRelease
    \/ FormRecord \/ FormDispatch
    \/ Stutter

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ processing \in BOOLEAN /\ marker \in BOOLEAN
    /\ phase \in {"idle", "turn", "tail1", "tail2", "posttail"}
    /\ handoff \in {"none", "pending", "ran_src", "ran_dst"}
    /\ formCont \in {"none", "pending", "done"}

\* Each answered message was answered in its own author's turn (997b3d8).
PrincipalIsAuthor == \A m \in MsgSet : handledBy[m] \in {None, Author[m]}
\* Once the thread is idle, every delivered mention has been answered.
NoStrandedMention == Quiescent => \A m \in arrived : handledBy[m] # None
\* A handoff's first turn runs in the incoming agent's session (090ecc1).
HandoffOnDestination == handoff # "ran_src"
\* A recorded continuation is not left pending once the thread is idle.
NoStrandedContinuation == Quiescent => formCont # "pending" /\ handoff # "pending"
=============================================================================
