---------------------------- MODULE AdapterOverlap ----------------------------
(***************************************************************************)
(* One platform thread across an adapter restart. The "old" process may    *)
(* die (deploy stop, crash) with a turn in flight; the "new" process boots, *)
(* runs the orphan sweep, and admits mentions. The MA session outlives the  *)
(* process: a turn the old process started keeps running on MA until MA     *)
(* finishes it. thread_sessions rows, their active-turn markers and each    *)
(* row's MA session status are shared state.                                *)
(*                                                                         *)
(* Turn path (per turn): Admit (in-memory _processing, recovery gate) ->    *)
(* Bind (per-thread advisory lock; reuse the newest live row or create one) *)
(* -> Mark (marker written after the lock is released) -> Send (open        *)
(* stream, send user.message) -> MA runs -> Observe -> Finish (clear the    *)
(* marker). A dead session is recovered outside the lock: mark_dead ->      *)
(* create_fresh_session -> link_replacement.                                *)
(*                                                                         *)
(* Sources: packages/adapters/slack/daimon/adapters/slack/app.py,           *)
(* slack/boot_sweep.py (retire_orphaned_turns), discord/bot.py              *)
(* (_retire_orphaned_turns_once, _orchestrate), discord/wizard_submit.py,   *)
(* core/session_preparation.py (prepare_session_for_turn),                  *)
(* core/turn/run.py (run_prepared_turn recovery), core/turn/driver.py.      *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS
    Overlap,               \* the new process may start while the old one is alive
    AdmitBeforeRecovery,   \* turns admitted before this process's orphan sweep finished
    SweepCAS,              \* sweep clears a marker only if unchanged since its snapshot
    SweepInterrupts,       \* #232 (merged): the sweep interrupts the orphan's MA session
    SendWaitsForIdle,      \* proposed alternative: never send user.message into a running session
    WizardBypass,          \* Discord wizard turns skip the per-thread _processing guard
    RecoveryUnderLock,     \* alternative: dead-session recovery holds the bind lock ...
    RecoveryAdopts,        \* ... and adopts a replacement another turn already made
    OldCanDie,             \* the old process can stop with a turn in flight
    MaxTurns, MaxRows, MaxDeaths

Procs == {"old", "new"}
Turns == 1..MaxTurns
Rows == 1..MaxRows
NoProc == "none"
Sources == {"mention"} \cup (IF WizardBypass THEN {"wizard"} ELSE {})
LiveTurnPcs == {"bound", "marked", "sent", "ending", "rec1", "rec2", "rec3"}

VARIABLES
    alive, started, recovered, processing,
    sweepPc, sweepSnap,
    lock,
    rows, ma, marker, replacedBy,
    pc, tproc, tsrc, trow,
    deaths, sentIntoRunning, stolenClear, staleClear

vars == <<alive, started, recovered, processing, sweepPc, sweepSnap, lock,
          rows, ma, marker, replacedBy, pc, tproc, tsrc, trow,
          deaths, sentIntoRunning, stolenClear, staleClear>>

Init ==
    /\ alive = [p \in Procs |-> p = "old"]
    /\ started = [p \in Procs |-> p = "old"]
    /\ recovered = [p \in Procs |-> p = "old"]
    /\ processing = [p \in Procs |-> FALSE]
    /\ sweepPc = [p \in Procs |-> "none"]
    /\ sweepSnap = [p \in Procs |-> [r \in Rows |-> 0]]
    /\ lock = 0
    /\ rows = [r \in Rows |-> IF r = 1 THEN "live" ELSE "none"]
    /\ ma = [r \in Rows |-> IF r = 1 THEN "idle" ELSE "none"]
    /\ marker = [r \in Rows |-> 0]
    /\ replacedBy = [r \in Rows |-> 0]
    /\ pc = [t \in Turns |-> "idle"]
    /\ tproc = [t \in Turns |-> NoProc]
    /\ tsrc = [t \in Turns |-> "mention"]
    /\ trow = [t \in Turns |-> 0]
    /\ deaths = 0
    /\ sentIntoRunning = FALSE
    /\ stolenClear = FALSE
    /\ staleClear = FALSE

LiveRows == {r \in Rows : rows[r] = "live"}
Newest(S) == CHOOSE r \in S : \A s \in S : s <= r
FreeRow == IF \E r \in Rows : rows[r] = "none"
              THEN CHOOSE r \in Rows : rows[r] = "none" /\ \A s \in Rows : rows[s] = "none" => r <= s
              ELSE 0
\* A marker belongs to a turn that is still running in a live process.
LiveOwner(t) == t # 0 /\ pc[t] \in LiveTurnPcs /\ alive[tproc[t]]

(* ---------------- processes ---------------- *)
StartNew ==
    /\ ~started["new"]
    /\ Overlap \/ ~alive["old"]
    /\ started' = [started EXCEPT !["new"] = TRUE]
    /\ alive' = [alive EXCEPT !["new"] = TRUE]
    /\ UNCHANGED <<recovered, processing, sweepPc, sweepSnap, lock, rows, ma, marker,
                   replacedBy, pc, tproc, tsrc, trow, deaths, sentIntoRunning, stolenClear, staleClear>>

\* Stop/crash of the old process: its in-memory state is gone; MA keeps running.
OldDies ==
    /\ OldCanDie /\ alive["old"]
    /\ alive' = [alive EXCEPT !["old"] = FALSE]
    /\ processing' = [processing EXCEPT !["old"] = FALSE]
    /\ pc' = [t \in Turns |-> IF tproc[t] = "old" /\ pc[t] \notin {"idle", "done"}
                                THEN "orphaned" ELSE pc[t]]
    /\ lock' = IF lock # 0 /\ tproc[lock] = "old" THEN 0 ELSE lock
    /\ UNCHANGED <<started, recovered, sweepPc, sweepSnap, rows, ma, marker, replacedBy,
                   tproc, tsrc, trow, deaths, sentIntoRunning, stolenClear, staleClear>>

\* Orphan sweep: list rows with a marker, then retire each card and clear.
SweepSnap(p) ==
    /\ alive[p] /\ ~recovered[p] /\ sweepPc[p] = "none"
    /\ sweepSnap' = [sweepSnap EXCEPT ![p] = marker]
    /\ sweepPc' = [sweepPc EXCEPT ![p] = "snap"]
    /\ UNCHANGED <<alive, started, recovered, processing, lock, rows, ma, marker, replacedBy,
                   pc, tproc, tsrc, trow, deaths, sentIntoRunning, stolenClear, staleClear>>

SweepClear(p) ==
    LET snap == sweepSnap[p]
        cleared == {r \in Rows : snap[r] # 0 /\ (SweepCAS => marker[r] = snap[r])}
    IN
    /\ alive[p] /\ sweepPc[p] = "snap"
    /\ marker' = [r \in Rows |-> IF r \in cleared THEN 0 ELSE marker[r]]
    /\ stolenClear' = (stolenClear \/ \E r \in cleared : LiveOwner(marker[r]))
    \* The narrower loss compare-and-clear prevents: the marker was written after
    \* this sweep's snapshot (it no longer names what the sweep read).
    /\ staleClear' = (staleClear \/ \E r \in cleared : marker[r] # snap[r] /\ LiveOwner(marker[r]))
    /\ ma' = [r \in Rows |-> IF SweepInterrupts /\ r \in cleared /\ ma[r] = "running"
                                THEN "idle" ELSE ma[r]]
    /\ sweepPc' = [sweepPc EXCEPT ![p] = "done"]
    /\ recovered' = [recovered EXCEPT ![p] = TRUE]
    /\ UNCHANGED <<alive, started, processing, lock, rows, replacedBy, pc, tproc, tsrc,
                   trow, deaths, sentIntoRunning, sweepSnap>>

(* ---------------- turns ---------------- *)
Admit(t, p, src) ==
    /\ pc[t] = "idle" /\ alive[p]
    /\ recovered[p] \/ AdmitBeforeRecovery
    /\ src = "mention" => ~processing[p]
    /\ processing' = IF src = "mention" THEN [processing EXCEPT ![p] = TRUE] ELSE processing
    /\ pc' = [pc EXCEPT ![t] = "admitted"]
    /\ tproc' = [tproc EXCEPT ![t] = p]
    /\ tsrc' = [tsrc EXCEPT ![t] = src]
    /\ UNCHANGED <<alive, started, recovered, sweepPc, sweepSnap, lock, rows, ma, marker,
                   replacedBy, trow, deaths, sentIntoRunning, stolenClear, staleClear>>

\* prepare_session_for_turn under the advisory lock (one atomic step). A plain
\* reuse does not look at the marker or at MA's session status.
Bind(t) ==
    /\ pc[t] = "admitted" /\ alive[tproc[t]] /\ lock = 0
    /\ IF LiveRows # {}
          THEN /\ trow' = [trow EXCEPT ![t] = Newest(LiveRows)]
               /\ UNCHANGED <<rows, ma>>
          ELSE /\ FreeRow # 0
               /\ rows' = [rows EXCEPT ![FreeRow] = "live"]
               /\ ma' = [ma EXCEPT ![FreeRow] = "idle"]
               /\ trow' = [trow EXCEPT ![t] = FreeRow]
    /\ pc' = [pc EXCEPT ![t] = "bound"]
    /\ UNCHANGED <<alive, started, recovered, processing, sweepPc, sweepSnap, lock, marker,
                   replacedBy, tproc, tsrc, deaths, sentIntoRunning, stolenClear, staleClear>>

\* The active-turn marker is written after the bind lock was released.
Mark(t) ==
    /\ pc[t] = "bound" /\ alive[tproc[t]]
    /\ marker' = [marker EXCEPT ![trow[t]] = t]
    /\ pc' = [pc EXCEPT ![t] = "marked"]
    /\ UNCHANGED <<alive, started, recovered, processing, sweepPc, sweepSnap, lock, rows, ma,
                   replacedBy, tproc, tsrc, trow, deaths, sentIntoRunning, stolenClear, staleClear>>

\* Open the stream and send user.message. MA answers 200 to a user.* event sent
\* into a running session and ignores it (driver.py, measured 2026-08-26).
Send(t) ==
    LET r == trow[t] IN
    /\ pc[t] = "marked" /\ alive[tproc[t]]
    /\ SendWaitsForIdle => ma[r] # "running"
    /\ CASE ma[r] = "dead" ->
              /\ pc' = [pc EXCEPT ![t] = "rec1"]
              /\ UNCHANGED <<ma, sentIntoRunning>>
         [] ma[r] = "running" ->
              /\ sentIntoRunning' = TRUE
              /\ pc' = [pc EXCEPT ![t] = "sent"]
              /\ UNCHANGED ma
         [] ma[r] = "idle" ->
              /\ ma' = [ma EXCEPT ![r] = "running"]
              /\ pc' = [pc EXCEPT ![t] = "sent"]
              /\ UNCHANGED sentIntoRunning
    /\ UNCHANGED <<alive, started, recovered, processing, sweepPc, sweepSnap, lock, rows,
                   marker, replacedBy, tproc, tsrc, trow, deaths, stolenClear, staleClear>>

MAFinish(r) ==
    /\ ma[r] = "running"
    /\ ma' = [ma EXCEPT ![r] = "idle"]
    /\ UNCHANGED <<alive, started, recovered, processing, sweepPc, sweepSnap, lock, rows,
                   marker, replacedBy, pc, tproc, tsrc, trow, deaths, sentIntoRunning, stolenClear, staleClear>>

MADies(r) ==
    /\ deaths < MaxDeaths /\ rows[r] = "live" /\ ma[r] \in {"idle", "running"}
    /\ ma' = [ma EXCEPT ![r] = "dead"]
    /\ deaths' = deaths + 1
    /\ UNCHANGED <<alive, started, recovered, processing, sweepPc, sweepSnap, lock, rows,
                   marker, replacedBy, pc, tproc, tsrc, trow, sentIntoRunning, stolenClear, staleClear>>

Observe(t) ==
    LET r == trow[t] IN
    /\ pc[t] = "sent" /\ alive[tproc[t]]
    /\ ma[r] \in {"idle", "dead"}
    /\ pc' = [pc EXCEPT ![t] = IF ma[r] = "idle" THEN "ending" ELSE "rec1"]
    /\ UNCHANGED <<alive, started, recovered, processing, sweepPc, sweepSnap, lock, rows, ma,
                   marker, replacedBy, tproc, tsrc, trow, deaths, sentIntoRunning, stolenClear, staleClear>>

\* run_prepared_turn recovery: mark_dead, create_fresh_session, link_replacement.
Rec1(t) ==
    LET r == trow[t] IN
    /\ pc[t] = "rec1" /\ alive[tproc[t]]
    /\ RecoveryUnderLock => lock = 0
    /\ lock' = IF RecoveryUnderLock THEN t ELSE lock
    /\ IF RecoveryAdopts /\ replacedBy[r] # 0
          THEN /\ trow' = [trow EXCEPT ![t] = replacedBy[r]]
               /\ pc' = [pc EXCEPT ![t] = "rec3"]
               /\ UNCHANGED rows
          ELSE /\ rows' = [rows EXCEPT ![r] = "dead"]
               /\ pc' = [pc EXCEPT ![t] = "rec2"]
               /\ UNCHANGED trow
    /\ UNCHANGED <<alive, started, recovered, processing, sweepPc, sweepSnap, ma, marker,
                   replacedBy, tproc, tsrc, deaths, sentIntoRunning, stolenClear, staleClear>>

Rec2(t) ==
    LET r == trow[t] n == FreeRow IN
    /\ pc[t] = "rec2" /\ alive[tproc[t]] /\ n # 0
    /\ rows' = [rows EXCEPT ![n] = "live"]
    /\ ma' = [ma EXCEPT ![n] = "idle"]
    /\ replacedBy' = [replacedBy EXCEPT ![r] = n]
    /\ trow' = [trow EXCEPT ![t] = n]
    /\ pc' = [pc EXCEPT ![t] = "rec3"]
    /\ UNCHANGED <<alive, started, recovered, processing, sweepPc, sweepSnap, lock, marker,
                   tproc, tsrc, deaths, sentIntoRunning, stolenClear, staleClear>>

Rec3(t) ==
    /\ pc[t] = "rec3" /\ alive[tproc[t]]
    /\ lock' = IF lock = t THEN 0 ELSE lock
    /\ marker' = [marker EXCEPT ![trow[t]] = t]
    /\ pc' = [pc EXCEPT ![t] = "marked"]
    /\ UNCHANGED <<alive, started, recovered, processing, sweepPc, sweepSnap, rows, ma,
                   replacedBy, tproc, tsrc, trow, deaths, sentIntoRunning, stolenClear, staleClear>>

\* Finish: clear_active_turn is unconditional on the turn's own row.
Finish(t) ==
    LET r == trow[t] p == tproc[t] IN
    /\ pc[t] = "ending" /\ alive[p]
    /\ stolenClear' = (stolenClear \/ (marker[r] # t /\ marker[r] # 0 /\ LiveOwner(marker[r])))
    /\ marker' = [marker EXCEPT ![r] = 0]
    /\ processing' = IF tsrc[t] = "mention" THEN [processing EXCEPT ![p] = FALSE] ELSE processing
    /\ pc' = [pc EXCEPT ![t] = "done"]
    /\ UNCHANGED <<alive, started, recovered, sweepPc, sweepSnap, lock, rows, ma, replacedBy,
                   tproc, tsrc, trow, deaths, sentIntoRunning, staleClear>>

Idle ==
    /\ \A t \in Turns : pc[t] \in {"done", "orphaned", "idle"}
    /\ \A r \in Rows : ma[r] # "running"
    /\ UNCHANGED vars

Next ==
    \/ StartNew \/ OldDies
    \/ \E p \in Procs : SweepSnap(p) \/ SweepClear(p)
    \/ \E t \in Turns, p \in Procs, s \in Sources : Admit(t, p, s)
    \/ \E t \in Turns : Bind(t) \/ Mark(t) \/ Send(t) \/ Observe(t)
                        \/ Rec1(t) \/ Rec2(t) \/ Rec3(t) \/ Finish(t)
    \/ \E r \in Rows : MAFinish(r) \/ MADies(r)
    \/ Idle

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ rows \in [Rows -> {"none", "live", "dead"}]
    /\ ma \in [Rows -> {"none", "idle", "running", "dead"}]
    /\ marker \in [Rows -> 0..MaxTurns]
    /\ lock \in 0..MaxTurns

\* <= 1 live thread_sessions row for the thread.
AtMostOneLiveRow == Cardinality(LiveRows) <= 1
\* No user.message is sent into a session that is still running a turn.
NoMessageIntoRunning == ~sentIntoRunning
\* A marker is cleared only by the turn that set it (or once its owner is gone).
NoStolenClear == ~stolenClear
\* The sweep never clears a live turn's marker written after the sweep's own
\* snapshot. Compare-and-clear alone guarantees this; it does not guarantee
\* NoStolenClear, because a marker written before the snapshot still matches.
NoStaleClear == ~staleClear
=============================================================================
