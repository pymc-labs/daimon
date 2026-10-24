----------------------------- MODULE GrantLocks -----------------------------
EXTENDS Naturals, Sequences
CONSTANTS Broken, Case
Locks == {"tenant", "x", "y", "a", "b"}
Cases == {"batch", "cross", "nested"}
BaseFirst(c) == CASE c = "batch" -> <<"x", "a", "y">>
                  [] c = "cross" -> <<"x", "b", "a">>
                  [] c = "nested" -> <<"x", "a", "y">>
BaseSecond(c) == CASE c = "batch" -> <<"y", "a">>
                   [] c = "cross" -> <<"y", "a", "b">>
                   [] c = "nested" -> <<"y", "a">>
FirstOrder(c) == IF Broken THEN BaseFirst(c) ELSE <<"tenant">> \o BaseFirst(c)
SecondOrder(c) == IF Broken THEN BaseSecond(c) ELSE <<"tenant">> \o BaseSecond(c)
VARIABLES held, first, second, scenario
vars == <<held, first, second, scenario>>

Init == /\ held = [lock \in Locks |-> "none"]
        /\ first = 1
        /\ second = 1
        /\ scenario \in IF Case = "all" THEN Cases ELSE {Case}

\* Batch: panel holds x and a before finding y; confirmation holds y and waits a.
\* Cross: confirmation of x locks holder b then target a, while confirmation
\* of y locks holder a then target b.
\* Nested: completion sees a new draft y after confirmation has locked a;
\* another writer holds y and waits for a.
\* The fixed implementation takes one tenant lock before these other locks.
FirstStep ==
    IF first <= Len(FirstOrder(scenario))
    THEN LET lock == FirstOrder(scenario)[first]
         IN /\ held[lock] = "none"
            /\ held' = [held EXCEPT ![lock] = "first"]
            /\ first' = first + 1
            /\ UNCHANGED <<second, scenario>>
    ELSE /\ first = Len(FirstOrder(scenario)) + 1
         /\ held' = [lock \in Locks |-> IF held[lock] = "first" THEN "none" ELSE held[lock]]
         /\ first' = first + 1
         /\ UNCHANGED <<second, scenario>>

SecondStep ==
    IF second <= Len(SecondOrder(scenario))
    THEN LET lock == SecondOrder(scenario)[second]
         IN /\ held[lock] = "none"
            /\ held' = [held EXCEPT ![lock] = "second"]
            /\ second' = second + 1
            /\ UNCHANGED <<first, scenario>>
    ELSE /\ second = Len(SecondOrder(scenario)) + 1
         /\ held' = [lock \in Locks |-> IF held[lock] = "second" THEN "none" ELSE held[lock]]
         /\ second' = second + 1
         /\ UNCHANGED <<first, scenario>>

Done == /\ first = Len(FirstOrder(scenario)) + 2
        /\ second = Len(SecondOrder(scenario)) + 2
        /\ UNCHANGED vars
Next == FirstStep \/ SecondStep \/ Done
Spec == Init /\ [][Next]_vars
TypeOK == /\ held \in [Locks -> {"none", "first", "second"}]
          /\ scenario \in Cases
          /\ first \in 1..(Len(FirstOrder(scenario)) + 2)
          /\ second \in 1..(Len(SecondOrder(scenario)) + 2)
=============================================================================
