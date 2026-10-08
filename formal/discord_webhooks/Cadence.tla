------------------------- MODULE Cadence -------------------------
EXTENDS Integers, FiniteSets, TLC

\* Each Tick is one render window of WindowSeconds. A turn contributes at most
\* one state-change edit per window. OtherOps represents initial/terminal
\* traffic at the stated average arrival rate. The counters diagnose global
\* and route ceilings; they do not model request burst timing within a window.
CONSTANTS Turns, Parents, Place, HookOf, MaxHooks, Mode,
          FirstTickOps, LaterTickOps, OtherOps, WindowSeconds,
          HookLimit, GlobalLimit
ASSUME /\ Turns # {} /\ Parents # {}
       /\ Place \in [Turns -> Parents]
       /\ HookOf \in [Turns -> 1..MaxHooks]
       /\ Mode \in {"webhook", "bot"}
       /\ FirstTickOps \in Nat /\ LaterTickOps \in Nat /\ OtherOps \in Nat
       /\ HookLimit > 0 /\ WindowSeconds > 0
       /\ GlobalLimit = 50 * WindowSeconds

FifteenTurns == 1..15
TwoHundredTurns == 1..200
FortyParents == 1..40
SixtyFiveParents == 1..65
RoundRobinPlace == [t \in Turns |-> ((t - 1) % Cardinality(Parents)) + 1]
OneHook == [t \in Turns |-> 1]
BalancedHooks == [t \in Turns |-> (((t - 1) \div Cardinality(Parents)) % MaxHooks) + 1]

VARIABLES tick, globalPending, hookPending
vars == <<tick, globalPending, hookPending>>

Ops == IF tick = 0 THEN FirstTickOps ELSE LaterTickOps
GlobalDemand == Cardinality(Turns) * Ops + OtherOps
HookDemand(p, h) == Cardinality({t \in Turns: Place[t] = p /\ HookOf[t] = h}) * Ops
Excess(demand, limit) == IF demand > limit THEN demand - limit ELSE 0

Init == /\ tick = 0
        /\ globalPending = 0
        /\ hookPending = [p \in Parents |-> [h \in 1..MaxHooks |-> 0]]

Tick == /\ tick < 2
        /\ globalPending' = Excess(globalPending + GlobalDemand, GlobalLimit)
        /\ hookPending' =
              [p \in Parents |-> [h \in 1..MaxHooks |->
                  IF Mode = "webhook"
                  THEN Excess(hookPending[p][h] + HookDemand(p, h), HookLimit)
                  ELSE 0]]
        /\ tick' = tick + 1

Next == Tick
Spec == Init /\ [][Next]_vars
TypeOK == /\ tick \in 0..2
          /\ globalPending \in Nat
          /\ hookPending \in [Parents -> [1..MaxHooks -> Nat]]
NoGlobalBacklog == globalPending = 0
NoHookBacklog == \A p \in Parents: \A h \in 1..MaxHooks: hookPending[p][h] = 0
=================================================================
