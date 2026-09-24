------------------------------ MODULE Metering ------------------------------
(***************************************************************************)
(* Usage metering for one Managed Agents session: the live recorder in the *)
(* turn driver, the scheduler's usage sweep, and the per-deployment ledger. *)
(*                                                                         *)
(* MA appends K span.model_request_end events and then a terminal idle to  *)
(* the session log. The driver consumes them over SSE stream generations;  *)
(* a generation can end without its terminal event (clean close, read      *)
(* timeout, dropped connection), after which the driver replays history    *)
(* and either reconnects (session still running) or finalizes (session     *)
(* idle). The sweep lists the session's events and replays each through    *)
(* the same recorder. A ledger row per (deployment, event) is written by   *)
(* whichever writer commits first (unique idempotency key, ON CONFLICT DO  *)
(* NOTHING); the losing writer is a no-op.                                 *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets, TLC

CONSTANTS
    K,                 \* model calls in the turn (span.model_request_end events)
    MaxDrops,          \* stream generations that end without the terminal event
    MaxRedeliveries,   \* already-emitted events a new generation may re-emit
    MaxSweeps,         \* sweep passes per deployment
    BillReplayed,      \* live driver bills replay-folded events it has not billed yet
    DedupeHooks,       \* delivered-event set spans stream generations (44528cb)
    LiveFrozenPrice,   \* live recorder prices at the session's frozen model (1c4df18)
    SweepEnabled,      \* the scheduler's usage sweep runs
    SharedTenant,      \* another deployment has this tenant row and lists this MA workspace
    SweepChecksOwner,  \* the sweep skips sessions another deployment created
    LiveExempt         \* the turn runs BillingExempt (daimon run; MCP caller with no platform user)

Events == 1..K
Terminal == 0                    \* the session.status_idle event, as an inbox item
Deployments == {"D1", "D2"}
Owner == "D1"                    \* the deployment whose adapter created the session
Frozen == "m0"                   \* the model in the session's creation-time snapshot
Models == {"m0", "m1"}
None == [by |-> "none", price |-> "none"]
Rows == {None} \cup [by : {"live", "sweep"}, price : Models]

VARIABLES
    emitted,       \* events MA has appended so far (1..emitted)
    maDone,        \* MA emitted the terminal idle
    agentModel,    \* responder agent's current model; "m1" after an agents.update
    stream,        \* "up" | "down"
    inbox,         \* items the current stream generation will still deliver
    delivered,     \* driver's delivered_event_ids
    hookCalls,     \* live recorder invocations per event
    drops,
    redeliveries,
    liveState,     \* "running" | "finalized" | "crashed"
    rows,          \* rows[d][e]: the ledger/usage row for event e in deployment d's DB
    sweepPhase,    \* [Deployments -> {"idle", "walk"}]
    sweepSnap,     \* events the current pass listed
    sweepCursor,
    sweeps,        \* completed passes per deployment
    finalPass      \* an Owner pass that started after MA and the driver stopped completed

vars == <<emitted, maDone, agentModel, stream, inbox, delivered, hookCalls, drops,
          redeliveries, liveState, rows, sweepPhase, sweepSnap, sweepCursor, sweeps,
          finalPass>>

LivePrice == IF LiveFrozenPrice THEN Frozen ELSE agentModel

\* First committer wins: record_turn_usage inserts usage_events and
\* tenant_ledger ON CONFLICT DO NOTHING in one transaction.
Write(r, d, e, row) == IF r[d][e] = None THEN [r EXCEPT ![d][e] = row] ELSE r

LiveRecord(r, e) ==
    IF LiveExempt THEN r ELSE Write(r, Owner, e, [by |-> "live", price |-> LivePrice])

\* Bill every replayed event the driver has not billed (the fix) or nothing (main).
ReplayBill ==
    IF BillReplayed
    THEN LET todo == {e \in 1..emitted : e \notin delivered}
         IN /\ hookCalls' = [e \in Events |-> IF e \in todo THEN hookCalls[e] + 1 ELSE hookCalls[e]]
            /\ rows' = [d \in Deployments |-> [e \in Events |->
                          IF ~LiveExempt /\ d = Owner /\ e \in todo /\ rows[d][e] = None
                          THEN [by |-> "live", price |-> LivePrice]
                          ELSE rows[d][e]]]
            /\ delivered' = delivered \cup todo
    ELSE UNCHANGED <<hookCalls, rows, delivered>>

Init ==
    /\ emitted = 0
    /\ maDone = FALSE
    /\ agentModel = Frozen
    /\ stream = "up"
    /\ inbox = <<>>
    /\ delivered = {}
    /\ hookCalls = [e \in Events |-> 0]
    /\ drops = 0
    /\ redeliveries = 0
    /\ liveState = "running"
    /\ rows = [d \in Deployments |-> [e \in Events |-> None]]
    /\ sweepPhase = [d \in Deployments |-> "idle"]
    /\ sweepSnap = [d \in Deployments |-> 0]
    /\ sweepCursor = [d \in Deployments |-> 1]
    /\ sweeps = [d \in Deployments |-> 0]
    /\ finalPass = FALSE

LiveVars == <<stream, inbox, delivered, hookCalls, drops, redeliveries, liveState>>
SweepVars == <<sweepPhase, sweepSnap, sweepCursor, sweeps, finalPass>>

(* MA appends one model-call event; an open stream carries it. *)
Emit ==
    /\ emitted < K
    /\ emitted' = emitted + 1
    /\ inbox' = IF stream = "up" /\ liveState = "running"
                THEN Append(inbox, emitted + 1) ELSE inbox
    /\ UNCHANGED <<maDone, agentModel, stream, delivered, hookCalls, drops,
                   redeliveries, liveState, rows, SweepVars>>

EmitTerminal ==
    /\ emitted = K
    /\ ~maDone
    /\ maDone' = TRUE
    /\ inbox' = IF stream = "up" /\ liveState = "running"
                THEN Append(inbox, Terminal) ELSE inbox
    /\ UNCHANGED <<emitted, agentModel, stream, delivered, hookCalls, drops,
                   redeliveries, liveState, rows, SweepVars>>

(* An agents.update after the session exists: sessions keep their snapshot. *)
ChangeAgentModel ==
    /\ agentModel = Frozen
    /\ agentModel' = "m1"
    /\ UNCHANGED <<emitted, maDone, LiveVars, rows, SweepVars>>

(* driver._consume_with_reconnect: one event through the consume loop. *)
Deliver ==
    /\ liveState = "running"
    /\ stream = "up"
    /\ inbox # <<>>
    /\ LET e == Head(inbox) IN
       /\ inbox' = Tail(inbox)
       /\ IF e = Terminal
          THEN /\ liveState' = "finalized"
               /\ stream' = "down"
               /\ UNCHANGED <<delivered, hookCalls, rows>>
          ELSE /\ UNCHANGED <<liveState, stream>>
               /\ IF DedupeHooks /\ e \in delivered
                  THEN UNCHANGED <<delivered, hookCalls, rows>>
                  ELSE /\ hookCalls' = [hookCalls EXCEPT ![e] = @ + 1]
                       /\ rows' = LiveRecord(rows, e)
                       /\ delivered' = delivered \cup {e}
    /\ UNCHANGED <<emitted, maDone, agentModel, drops, redeliveries, SweepVars>>

(* A generation ends with no terminal event; undelivered items are lost. *)
Drop ==
    /\ liveState = "running"
    /\ stream = "up"
    /\ drops < MaxDrops
    /\ stream' = "down"
    /\ inbox' = <<>>
    /\ drops' = drops + 1
    /\ UNCHANGED <<emitted, maDone, agentModel, delivered, hookCalls, redeliveries,
                   liveState, rows, SweepVars>>

(* Status running: replay + fold, then open a new generation. The new       *)
(* generation may re-emit one already-emitted event (MA's re-emission      *)
(* semantics are not documented, so both choices are explored).            *)
Reconnect ==
    /\ liveState = "running"
    /\ stream = "down"
    /\ ~maDone
    /\ ReplayBill
    /\ stream' = "up"
    /\ \/ /\ inbox' = <<>>
          /\ UNCHANGED redeliveries
       \/ /\ redeliveries < MaxRedeliveries
          /\ \E e \in 1..emitted :
                /\ inbox' = <<e>>
                /\ redeliveries' = redeliveries + 1
    /\ UNCHANGED <<emitted, maDone, agentModel, drops, liveState, SweepVars>>

(* Status idle/terminated: replay + fold and finalize without reopening. *)
FinalizeFromReplay ==
    /\ liveState = "running"
    /\ stream = "down"
    /\ maDone
    /\ ReplayBill
    /\ liveState' = "finalized"
    /\ UNCHANGED <<emitted, maDone, agentModel, stream, inbox, drops, redeliveries,
                   SweepVars>>

(* The adapter process dies mid-turn; MA keeps running the session. *)
Crash ==
    /\ liveState = "running"
    /\ liveState' = "crashed"
    /\ stream' = "down"
    /\ inbox' = <<>>
    /\ UNCHANGED <<emitted, maDone, agentModel, delivered, hookCalls, drops,
                   redeliveries, rows, SweepVars>>

(* usage_sweep.sweep_headless_usage in deployment d. A deployment bills the *)
(* session when the daimon_tenant stamp names a tenant in its own DB.       *)
Bills(d) == d = Owner \/ (SharedTenant /\ ~SweepChecksOwner)

SweepStart(d) ==
    /\ SweepEnabled
    /\ Bills(d)
    /\ sweepPhase[d] = "idle"
    /\ sweeps[d] < MaxSweeps
    /\ sweepPhase' = [sweepPhase EXCEPT ![d] = "walk"]
    /\ sweepSnap' = [sweepSnap EXCEPT ![d] = emitted]
    /\ sweepCursor' = [sweepCursor EXCEPT ![d] = 1]
    /\ UNCHANGED <<emitted, maDone, agentModel, LiveVars, rows, sweeps, finalPass>>

SweepStep(d) ==
    /\ sweepPhase[d] = "walk"
    /\ sweepCursor[d] <= sweepSnap[d]
    /\ rows' = Write(rows, d, sweepCursor[d], [by |-> "sweep", price |-> Frozen])
    /\ sweepCursor' = [sweepCursor EXCEPT ![d] = @ + 1]
    /\ UNCHANGED <<emitted, maDone, agentModel, LiveVars, sweepPhase, sweepSnap, sweeps,
                   finalPass>>

SweepEnd(d) ==
    /\ sweepPhase[d] = "walk"
    /\ sweepCursor[d] > sweepSnap[d]
    /\ sweepPhase' = [sweepPhase EXCEPT ![d] = "idle"]
    /\ sweeps' = [sweeps EXCEPT ![d] = @ + 1]
    /\ finalPass' = (finalPass \/ (d = Owner /\ sweepSnap[d] = K /\ maDone))
    /\ UNCHANGED <<emitted, maDone, agentModel, LiveVars, rows, sweepSnap, sweepCursor>>

(* Nothing left to do: MA idle, driver stopped, sweeps exhausted or idle. *)
Done ==
    /\ maDone
    /\ liveState # "running"
    /\ \A d \in Deployments : sweepPhase[d] = "idle"
    /\ \A d \in Deployments : ~(SweepEnabled /\ Bills(d) /\ sweeps[d] < MaxSweeps)
    /\ UNCHANGED vars

Next ==
    \/ Done
    \/ Emit \/ EmitTerminal \/ ChangeAgentModel
    \/ Deliver \/ Drop \/ Reconnect \/ FinalizeFromReplay \/ Crash
    \/ \E d \in Deployments : SweepStart(d) \/ SweepStep(d) \/ SweepEnd(d)

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
TypeOK ==
    /\ emitted \in 0..K
    /\ maDone \in BOOLEAN
    /\ agentModel \in Models
    /\ stream \in {"up", "down"}
    /\ delivered \subseteq Events
    /\ hookCalls \in [Events -> Nat]
    /\ liveState \in {"running", "finalized", "crashed"}
    /\ rows \in [Deployments -> [Events -> Rows]]
    /\ sweepPhase \in [Deployments -> {"idle", "walk"}]

(* 44528cb: an event redelivered after a reconnect must not re-run the     *)
(* driver's per-event hooks (the recorder is the billing one).             *)
HookOnce == \A e \in Events : hookCalls[e] <= 1

(* 1c4df18: every debit is priced at the model the session actually runs. *)
PriceAgreement ==
    \A d \in Deployments, e \in Events : rows[d][e] # None => rows[d][e].price = Frozen

(* The driver meters every model call of a turn it finalized, including  *)
(* calls it learned about only from the replay.                          *)
LiveMetersWholeTurn ==
    liveState = "finalized" => \A e \in Events : hookCalls[e] >= 1

(* docs/billing.md: a caller with no platform user identity (and `daimon   *)
(* run`) is not billed. Expected to fail with the sweep enabled: the sweep *)
(* bills any session carrying a known daimon_tenant stamp.                 *)
ExemptNotBilled ==
    LiveExempt => \A d \in Deployments, e \in Events : rows[d][e] = None

(* No deployment debits a session another deployment created. *)
NoForeignDebit ==
    \A d \in Deployments \ {Owner}, e \in Events : rows[d][e] = None

(* The sweep is the backstop: after a full Owner pass taken once MA is idle, *)
(* every model call has exactly one row, whatever happened to the driver.   *)
SweepBackstop ==
    finalPass => \A e \in Events : rows[Owner][e] # None

(* Attribution (platform_user_id, reason) is the live writer's whenever the *)
(* driver was alive to write it. Expected to fail: first writer wins.       *)
AttributionPreserved ==
    liveState # "crashed" =>
        \A e \in Events : rows[Owner][e] # None => rows[Owner][e].by = "live"
=============================================================================
