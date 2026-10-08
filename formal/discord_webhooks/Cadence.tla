------------------------- MODULE Cadence -------------------------
EXTENDS Integers, FiniteSets, TLC

\* A tick is a two-second driver render check measured from render end. Each
\* lifecycle also debounces edits: Discord 10 s (five ticks), Slack 5 s
\* (at least three ticks). BalancedPhase represents staggered ongoing turns;
\* SynchronizedPhase represents a cold burst. A synchronized burst may hit a
\* global or route bucket despite a safe long-run average.
CONSTANTS Turns, Parents, Place, HookOf, MaxHooks, Mode,
          PhaseOf, DebounceTicks, NoDebounce, InitialOpsPerTurn,
          OtherOpsPerTick, HookLimit, GlobalLimit, Horizon
ASSUME /\ Turns # {} /\ Parents # {}
       /\ Place \in [Turns -> Parents]
       /\ HookOf \in [Turns -> 1..MaxHooks]
       /\ PhaseOf \in [Turns -> 0..(DebounceTicks - 1)]
       /\ DebounceTicks > 0 /\ Horizon > 0
       /\ Mode \in {"webhook", "bot", "slack"}
       /\ InitialOpsPerTurn \in 0..1 /\ OtherOpsPerTick \in Nat
       /\ HookLimit > 0 /\ GlobalLimit > 0

FifteenTurns == 1..15
TwoHundredTurns == 1..200
FortyParents == 1..40
SixtyFiveParents == 1..65
RoundRobinPlace == [t \in Turns |-> ((t - 1) % Cardinality(Parents)) + 1]
OneHook == [t \in Turns |-> 1]
BalancedHooks == [t \in Turns |-> (((t - 1) \div Cardinality(Parents)) % MaxHooks) + 1]
BalancedPhase == [t \in Turns |-> (t - 1) % DebounceTicks]
SynchronizedPhase == [t \in Turns |-> 0]

VARIABLES tick, globalPending, hookPending
vars == <<tick, globalPending, hookPending>>

FirstEditTick == IF InitialOpsPerTurn = 1 THEN DebounceTicks ELSE 0
EditDue(t) ==
    IF NoDebounce THEN TRUE
    ELSE tick >= FirstEditTick
         /\ ((tick - FirstEditTick) % DebounceTicks) = PhaseOf[t]
EditDemand == Cardinality({t \in Turns: EditDue(t)})
InitialDemand == IF tick = 0 THEN Cardinality(Turns) * InitialOpsPerTurn ELSE 0
GlobalDemand == EditDemand + InitialDemand + OtherOpsPerTick
HookDemand(p, h) ==
    Cardinality({t \in Turns: Place[t] = p /\ HookOf[t] = h /\ EditDue(t)})
    + IF tick = 0 THEN
        Cardinality({t \in Turns: Place[t] = p /\ HookOf[t] = h}) * InitialOpsPerTurn
      ELSE 0
Excess(demand, limit) == IF demand > limit THEN demand - limit ELSE 0

Init == /\ tick = 0
        /\ globalPending = 0
        /\ hookPending = [p \in Parents |-> [h \in 1..MaxHooks |-> 0]]

Tick == /\ tick < Horizon
        /\ globalPending' = Excess(globalPending + GlobalDemand, GlobalLimit)
        /\ hookPending' =
              [p \in Parents |-> [h \in 1..MaxHooks |->
                  IF Mode = "webhook"
                  THEN Excess(hookPending[p][h] + HookDemand(p, h), HookLimit)
                  ELSE 0]]
        /\ tick' = tick + 1

Next == Tick
Spec == Init /\ [][Next]_vars
TypeOK == /\ tick \in 0..Horizon
          /\ globalPending \in Nat
          /\ hookPending \in [Parents -> [1..MaxHooks -> Nat]]
NoGlobalBacklog == globalPending = 0
NoHookBacklog == \A p \in Parents: \A h \in 1..MaxHooks: hookPending[p][h] = 0
\* Slack has no Discord global bucket. Check its five-second debounce on the
\* same driver tick without assigning Slack a Discord rate limit.
SlackEditCeiling == Mode = "slack" => EditDemand <= (Cardinality(Turns) + DebounceTicks - 1) \div DebounceTicks
=================================================================
