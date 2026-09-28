---------------------------- MODULE WakeLease ----------------------------
EXTENDS Naturals, FiniteSets, TLC

(***************************************************************************)
(* One durable wake row and two dispatchers (two processes, or two tasks   *)
(* in one). A claim names an owner and a lease; the lease can expire at    *)
(* any time, including under a live but slow owner. `started` is the fence *)
(* committed just before the turn's external effect. A takeover needs an   *)
(* expired lease AND no fence; a fenced, expired claim is only ever        *)
(* abandoned (settled skipped). Every write after the claim is guarded on  *)
(* the owner. A crash drops a dispatcher's in-memory attempt.              *)
(*                                                                         *)
(* EnableFence = FALSE is the blind stale-claim retry of                  *)
(* ContinuationDispatch's ContinuationRecovery config: a takeover ignores  *)
(* the fence, and the duplicate turn comes back.                           *)
(***************************************************************************)

CONSTANTS Dispatchers, MaxAttempts, MaxCrashes, EnableFence

VARIABLES status, owner, started, expired, attempts, pc, effects, crashes
vars == <<status, owner, started, expired, attempts, pc, effects, crashes>>

None == "none"
Terminal == {"delivered", "skipped"}

Init ==
    /\ status = "pending"
    /\ owner = None
    /\ started = FALSE
    /\ expired = FALSE
    /\ attempts = 0
    /\ pc = [d \in Dispatchers |-> "idle"]
    /\ effects = 0
    /\ crashes = 0

Holds(d) == status = "claimed" /\ owner = d

Claimable ==
    /\ attempts < MaxAttempts
    /\ \/ status = "pending"
       \/ /\ status = "claimed"
          /\ expired
          /\ (EnableFence => ~started)

Claim(d) ==
    /\ pc[d] = "idle"
    /\ Claimable
    /\ status' = "claimed"
    /\ owner' = d
    /\ started' = FALSE
    /\ expired' = FALSE
    /\ attempts' = attempts + 1
    /\ pc' = [pc EXCEPT ![d] = "claimed"]
    /\ UNCHANGED <<effects, crashes>>

(* The decision said skip: no turn, settle while still the owner. *)
SkipDecided(d) ==
    /\ pc[d] = "claimed"
    /\ Holds(d)
    /\ ~started
    /\ status' = "skipped"
    /\ pc' = [pc EXCEPT ![d] = "idle"]
    /\ UNCHANGED <<owner, started, expired, attempts, effects, crashes>>

Start(d) ==
    /\ pc[d] = "claimed"
    /\ Holds(d)
    /\ ~started
    /\ started' = TRUE
    /\ expired' = FALSE
    /\ pc' = [pc EXCEPT ![d] = "started"]
    /\ UNCHANGED <<status, owner, attempts, effects, crashes>>

(* start_wake returned False: somebody took the row over. Touch nothing. *)
StartLost(d) ==
    /\ pc[d] = "claimed"
    /\ ~(Holds(d) /\ ~started)
    /\ pc' = [pc EXCEPT ![d] = "idle"]
    /\ UNCHANGED <<status, owner, started, expired, attempts, effects, crashes>>

(* The turn itself: visible and billed, whatever the row says by now. *)
Effect(d) ==
    /\ pc[d] = "started"
    /\ effects' = effects + 1
    /\ pc' = [pc EXCEPT ![d] = "effected"]
    /\ UNCHANGED <<status, owner, started, expired, attempts, crashes>>

(* Owner-guarded settle; a dispatcher that lost the row writes nothing. *)
Settle(d) ==
    /\ pc[d] = "effected"
    /\ status' = IF Holds(d) THEN "delivered" ELSE status
    /\ pc' = [pc EXCEPT ![d] = "idle"]
    /\ UNCHANGED <<owner, started, expired, attempts, effects, crashes>>

Expire ==
    /\ status = "claimed"
    /\ ~expired
    /\ expired' = TRUE
    /\ UNCHANGED <<status, owner, started, attempts, pc, effects, crashes>>

(* abandon_interrupted_wakes: expired and fenced, or out of attempts. *)
Abandon ==
    /\ status = "claimed"
    /\ expired
    /\ (started \/ attempts >= MaxAttempts)
    /\ status' = "skipped"
    /\ UNCHANGED <<owner, started, expired, attempts, pc, effects, crashes>>

Crash(d) ==
    /\ pc[d] /= "idle"
    /\ crashes < MaxCrashes
    /\ pc' = [pc EXCEPT ![d] = "idle"]
    /\ crashes' = crashes + 1
    /\ UNCHANGED <<status, owner, started, expired, attempts, effects>>

Next ==
    \/ \E d \in Dispatchers :
        Claim(d) \/ SkipDecided(d) \/ Start(d) \/ StartLost(d) \/ Effect(d) \/ Settle(d) \/ Crash(d)
    \/ Expire
    \/ Abandon

TypeOK ==
    /\ status \in {"pending", "claimed"} \cup Terminal
    /\ owner \in Dispatchers \cup {None}
    /\ started \in BOOLEAN
    /\ expired \in BOOLEAN
    /\ attempts \in 0..MaxAttempts
    /\ pc \in [Dispatchers -> {"idle", "claimed", "started", "effected"}]
    /\ effects \in 0..(MaxAttempts + 1)
    /\ crashes \in 0..MaxCrashes

AtMostOneExternalEffect == effects <= 1
DeliveredHasExternalEffect == status = "delivered" => effects >= 1

Spec == Init /\ [][Next]_vars

(* Progress: every dispatcher step and the lease clock are weakly fair; crashes are
   bounded, so they cannot starve the row. *)
ProgressSpec ==
    /\ Spec
    /\ \A d \in Dispatchers :
        /\ WF_vars(Claim(d))
        /\ WF_vars(Start(d))
        /\ WF_vars(StartLost(d))
        /\ WF_vars(Effect(d))
        /\ WF_vars(Settle(d))
    /\ WF_vars(Expire)
    /\ WF_vars(Abandon)

PendingEventuallySettles == status = "pending" ~> status \in Terminal
=============================================================================
