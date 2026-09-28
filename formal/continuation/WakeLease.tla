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

CONSTANTS Dispatchers, MaxAttempts, MaxCrashes, MaxReleases, EnableFence

VARIABLES status, owner, started, expired, attempts, pc, effects, crashes, releases
vars == <<status, owner, started, expired, attempts, pc, effects, crashes, releases>>

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
    /\ releases = 0

Holds(d) == status = "claimed" /\ owner = d

(* A pending row is always claimable; a takeover also needs attempts left. *)
Claimable ==
    \/ status = "pending"
    \/ /\ status = "claimed"
       /\ expired
       /\ attempts < MaxAttempts
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
    /\ UNCHANGED <<effects, crashes, releases>>

(* The decision said skip: no turn, settle while still the owner. *)
SkipDecided(d) ==
    /\ pc[d] = "claimed"
    /\ Holds(d)
    /\ ~started
    /\ status' = "skipped"
    /\ pc' = [pc EXCEPT ![d] = "idle"]
    /\ UNCHANGED <<owner, started, expired, attempts, effects, crashes, releases>>

Start(d) ==
    /\ pc[d] = "claimed"
    /\ Holds(d)
    /\ ~started
    /\ started' = TRUE
    /\ expired' = FALSE
    /\ pc' = [pc EXCEPT ![d] = "started"]
    /\ UNCHANGED <<status, owner, attempts, effects, crashes, releases>>

(* start_wake returned False: somebody took the row over. Touch nothing. *)
StartLost(d) ==
    /\ pc[d] = "claimed"
    /\ ~(Holds(d) /\ ~started)
    /\ pc' = [pc EXCEPT ![d] = "idle"]
    /\ UNCHANGED <<status, owner, started, expired, attempts, effects, crashes, releases>>

(* The turn itself: visible and billed, whatever the row says by now. *)
Effect(d) ==
    /\ pc[d] = "started"
    /\ effects' = effects + 1
    /\ pc' = [pc EXCEPT ![d] = "effected"]
    /\ UNCHANGED <<status, owner, started, expired, attempts, crashes, releases>>

(* Owner-guarded settle; a dispatcher that lost the row writes nothing. *)
Settle(d) ==
    /\ pc[d] = "effected"
    /\ status' = IF Holds(d) THEN "delivered" ELSE status
    /\ pc' = [pc EXCEPT ![d] = "idle"]
    /\ UNCHANGED <<owner, started, expired, attempts, effects, crashes, releases>>

(* release_wake: the owner knows the turn did not run (the session was busy
   at bind, before the effect). Back to pending, fence cleared, claim refunded.
   Bounded, so progress is checked under a thread that is busy only so often. *)
Release(d) ==
    /\ pc[d] \in {"claimed", "started"}
    /\ Holds(d)
    /\ releases < MaxReleases
    /\ status' = "pending"
    /\ owner' = None
    /\ started' = FALSE
    /\ expired' = FALSE
    /\ attempts' = attempts - 1
    /\ pc' = [pc EXCEPT ![d] = "idle"]
    /\ releases' = releases + 1
    /\ UNCHANGED <<effects, crashes>>

Expire ==
    /\ status = "claimed"
    /\ ~expired
    /\ expired' = TRUE
    /\ UNCHANGED <<status, owner, started, attempts, pc, effects, crashes, releases>>

(* abandon_interrupted_wakes: expired and fenced, or out of attempts. *)
Abandon ==
    /\ status = "claimed"
    /\ expired
    /\ (started \/ attempts >= MaxAttempts)
    /\ status' = "skipped"
    /\ UNCHANGED <<owner, started, expired, attempts, pc, effects, crashes, releases>>

Crash(d) ==
    /\ pc[d] /= "idle"
    /\ crashes < MaxCrashes
    /\ pc' = [pc EXCEPT ![d] = "idle"]
    /\ crashes' = crashes + 1
    /\ UNCHANGED <<status, owner, started, expired, attempts, effects, releases>>

Next ==
    \/ \E d \in Dispatchers :
        Claim(d) \/ SkipDecided(d) \/ Start(d) \/ StartLost(d) \/ Effect(d) \/ Settle(d)
        \/ Release(d) \/ Crash(d)
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
    /\ releases \in 0..MaxReleases

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
