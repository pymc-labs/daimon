--------------------- MODULE TeamsActivityClaim ---------------------
EXTENDS Naturals

CONSTANT Durable
VARIABLES claimed, phase, deliveries, runs, finished, crashed
vars == <<claimed, phase, deliveries, runs, finished, crashed>>

Init == /\ claimed = FALSE
        /\ phase = "idle"
        /\ deliveries = 0
        /\ runs = 0
        /\ finished = FALSE
        /\ crashed = FALSE

Deliver == /\ deliveries < 2
           /\ phase = "idle"
           /\ deliveries' = deliveries + 1
           /\ phase' = "delivered"
           /\ UNCHANGED <<claimed, runs, finished, crashed>>

Claim == /\ phase = "delivered"
         /\ ~claimed
         /\ claimed' = TRUE
         /\ phase' = "claimed"
         /\ UNCHANGED <<deliveries, runs, finished, crashed>>

DropDuplicate == /\ phase = "delivered"
                 /\ claimed
                 /\ phase' = "idle"
                 /\ UNCHANGED <<claimed, deliveries, runs, finished, crashed>>

Run == /\ phase = "claimed"
       /\ runs' = runs + 1
       /\ phase' = "running"
       /\ UNCHANGED <<claimed, deliveries, finished, crashed>>

Finish == /\ phase = "running"
          /\ finished' = TRUE
          /\ phase' = "idle"
          /\ UNCHANGED <<claimed, deliveries, runs, crashed>>

Crash == /\ phase \in {"claimed", "running"}
         /\ ~crashed
         /\ crashed' = TRUE
         /\ phase' = "idle"
         /\ claimed' = IF Durable THEN claimed ELSE FALSE
         /\ UNCHANGED <<deliveries, runs, finished>>

Next == Deliver \/ Claim \/ DropDuplicate \/ Run \/ Finish \/ Crash
Spec == Init /\ [][Next]_vars
NoSecondRun == runs <= 1
====================================================================
