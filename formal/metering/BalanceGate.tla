---------------------------- MODULE BalanceGate ----------------------------
(***************************************************************************)
(* One tenant's balance gate against its spend. Each activity is admitted  *)
(* once (balance read, deny unless strictly positive), then spends C model *)
(* calls of one unit each. A chat turn's debit lands per call through the  *)
(* live recorder; a headless MCP turn's debit lands only when the usage    *)
(* sweep runs. Nothing re-checks the balance after admission, so the gate  *)
(* is a check-then-act by design; the question is how far below zero the   *)
(* ledger can go, and whether every unit of spend was gated and debited.   *)
(***************************************************************************)
EXTENDS Integers, FiniteSets, TLC

CONSTANTS
    NNew,              \* chat turns that create a session
    NReused,           \* chat turns on an already-live thread session
    NClassifier,       \* thread-participation classifier calls
    NHeadless,         \* MCP start_turn sessions (driven by nobody; billed by the sweep)
    B0,                \* starting balance (trial credit), in units
    C,                 \* model calls per activity
    MaxInflight,       \* activities running at once (per-tenant concurrency cap)
    GateOnReuse,       \* reused-session turns pass the gates (8192ff7)
    MeterReuseLive,    \* reused-session turns bind the live recorder (8192ff7)
    GateClassifier,    \* the classifier call runs behind the gates (fa83d3c)
    MeterClassifier,   \* the classifier call is debited (fa83d3c)
    MaxSweeps

N == NNew + NReused + NClassifier + NHeadless
Activities == 1..N
Kind(a) ==
    IF a <= NNew THEN "new"
    ELSE IF a <= NNew + NReused THEN "reused"
    ELSE IF a <= NNew + NReused + NClassifier THEN "classifier"
    ELSE "headless"

Gated(a) ==
    CASE Kind(a) = "new" -> TRUE
      [] Kind(a) = "reused" -> GateOnReuse
      [] Kind(a) = "classifier" -> GateClassifier
      [] Kind(a) = "headless" -> TRUE

\* How a unit of spend reaches the ledger: at the call, via the sweep, or never.
Metering(a) ==
    CASE Kind(a) = "new" -> "live"
      [] Kind(a) = "reused" -> IF MeterReuseLive THEN "live" ELSE "sweep"
      [] Kind(a) = "classifier" -> IF MeterClassifier THEN "live" ELSE "never"
      [] Kind(a) = "headless" -> "sweep"

\* admitBalance[a]: the ledger balance when a was let through (0 until then).
VARIABLES status, spent, debited, admitBalance, sweeps
vars == <<status, spent, debited, admitBalance, sweeps>>

RECURSIVE SumOver(_, _)
SumOver(f, S) == IF S = {} THEN 0 ELSE LET x == CHOOSE x \in S : TRUE IN f[x] + SumOver(f, S \ {x})

\* tenant_ledger balance = SUM(delta_usd); integers stand in for dollars.
Balance == B0 - SumOver(debited, Activities)
Inflight == Cardinality({a \in Activities : status[a] = "running"})

Init ==
    /\ status = [a \in Activities |-> "idle"]
    /\ spent = [a \in Activities |-> 0]
    /\ debited = [a \in Activities |-> 0]
    /\ admitBalance = [a \in Activities |-> 0]
    /\ sweeps = 0

(* admission.admit / _ctx._admit / the routine fire: read, then decide. *)
Admit(a) ==
    /\ status[a] = "idle"
    /\ Inflight < MaxInflight
    /\ IF Gated(a) /\ Balance <= 0
       THEN /\ status' = [status EXCEPT ![a] = "denied"]
            /\ UNCHANGED admitBalance
       ELSE /\ status' = [status EXCEPT ![a] = "running"]
            /\ admitBalance' = [admitBalance EXCEPT ![a] = Balance]
    /\ UNCHANGED <<spent, debited, sweeps>>

(* One span.model_request_end; the live recorder debits it inline. *)
Spend(a) ==
    /\ status[a] = "running"
    /\ spent[a] < C
    /\ spent' = [spent EXCEPT ![a] = @ + 1]
    /\ debited' = IF Metering(a) = "live" THEN [debited EXCEPT ![a] = @ + 1] ELSE debited
    /\ UNCHANGED <<status, admitBalance, sweeps>>

Finish(a) ==
    /\ status[a] = "running"
    /\ spent[a] = C
    /\ status' = [status EXCEPT ![a] = "done"]
    /\ UNCHANGED <<spent, debited, admitBalance, sweeps>>

(* usage_sweep: catches up every MA session's recorded calls. *)
Sweep ==
    /\ sweeps < MaxSweeps
    /\ debited' = [a \in Activities |-> IF Metering(a) = "sweep" THEN spent[a] ELSE debited[a]]
    /\ sweeps' = sweeps + 1
    /\ UNCHANGED <<status, spent, admitBalance>>

Done ==
    /\ \A a \in Activities : status[a] \in {"done", "denied"}
    /\ sweeps = MaxSweeps
    /\ UNCHANGED vars

Next == Done \/ Sweep \/ \E a \in Activities : Admit(a) \/ Spend(a) \/ Finish(a)

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
TypeOK ==
    /\ status \in [Activities -> {"idle", "running", "done", "denied"}]
    /\ spent \in [Activities -> 0..C]
    /\ debited \in [Activities -> 0..C]

(* Every unit of spend belongs to an activity that started while the      *)
(* ledger balance was positive. Stated over the balance recorded at        *)
(* admission, not over Gated(a), so it does not restate the fix flags: an  *)
(* ungated activity violates it only if it actually starts on a balance    *)
(* that is already zero or below.                                          *)
GatedSpend == \A a \in Activities : spent[a] > 0 => admitBalance[a] > 0

(* A finished activity that is not left to the sweep is fully debited. *)
SpendMetered ==
    \A a \in Activities :
        (status[a] = "done" /\ Metering(a) # "sweep") => debited[a] = spent[a]

(* The documented bound: at most MaxInflight activities can pass the gate  *)
(* on one positive balance, so the ledger never falls below                *)
(* -(MaxInflight * C) (B0 > 0 is consumed first).                          *)
OverdraftBound == Balance >= -(MaxInflight * C)
=============================================================================
