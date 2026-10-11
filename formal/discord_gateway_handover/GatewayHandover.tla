------------------------- MODULE GatewayHandover -------------------------
\* Discord gateway lease handover between overlapping adapter processes.
\* Process 1 (Old) starts connected and holding the lease; process 2 (New) is
\* started by the deploy; optional process 3 is a later restart.
EXTENDS Naturals, FiniteSets

CONSTANTS Procs,
          StopFirst,         \* today's deploy: Old is stopped before New starts
          WaitReady,         \* designed deploy waits until New has built its runtime before SIGTERM to Old
          UnlockBeforeClose, \* rejected design: release the lease before closing the gateway
          TrustLockOnly,     \* rejected design: connect as soon as the lock is held
          ClearOnTake,       \* the new holder resets released_at when it takes the lease
          ShortGrace,        \* heartbeat grace may elapse before a disconnected holder has stopped
          AllowLoss,         \* one owner-connection loss on a connected process
          AllowKill          \* one SIGKILL after SIGTERM

Old == 1
New == 2

VARIABLES phase,       \* off | building | ready | connected | exited
          gw,          \* connected to the Discord gateway (handling events)
          draining,    \* bot.draining: new mentions rejected
          termed,      \* SIGTERM received
          drained,     \* in-flight work finished (or grace elapsed)
          conn,        \* owner connection alive in PostgreSQL (owner lock + gateway lock live on it)
          graceOpen,   \* discord_worker_heartbeats row still inside OWNER_LOSS_GRACE_S
          ownerState,  \* process's belief: ok | retry | lost
          lockHolder,  \* holder of the GATEWAY_KEY advisory lock (0 = free)
          leaseHolder, \* lease row: last recorded holder
          leaseRel,    \* lease row: released_at set
          claim,       \* none | held | wait | go | taken
          relStage,    \* 0 none, 1 released_at written, 2 unlocked
          everConn,    \* has connected to Discord (history)
          lossSeen, killSeen  \* history: unclean events happened

vars == <<phase, gw, draining, termed, drained, conn, graceOpen, ownerState, lockHolder,
          leaseHolder, leaseRel, claim, relStage, everConn, lossSeen, killSeen>>

Running(p) == phase[p] \in {"ready", "connected"}
Alive(p) == conn[p] \/ graceOpen[p]            \* owner_is_alive: lock held OR heartbeat unexpired
Drop(p) == IF lockHolder = p THEN 0 ELSE lockHolder
Set(f, p, v) == [f EXCEPT ![p] = v]

Init == /\ phase = [p \in Procs |-> IF p = Old THEN "connected" ELSE "off"]
        /\ gw = [p \in Procs |-> p = Old]
        /\ draining = [p \in Procs |-> FALSE]
        /\ termed = [p \in Procs |-> FALSE]
        /\ drained = [p \in Procs |-> FALSE]
        /\ conn = [p \in Procs |-> p = Old]
        /\ graceOpen = [p \in Procs |-> p = Old]
        /\ ownerState = [p \in Procs |-> "ok"]
        /\ lockHolder = Old
        /\ leaseHolder = Old
        /\ leaseRel = FALSE
        /\ claim = [p \in Procs |-> IF p = Old THEN "taken" ELSE "none"]
        /\ relStage = [p \in Procs |-> 0]
        /\ everConn = [p \in Procs |-> p = Old]
        /\ lossSeen = FALSE
        /\ killSeen = FALSE

\* ---- New process start-up (no lock, no gateway) ----
StartNew(p) == /\ phase[p] = "off"
               /\ (p = New => (StopFirst => phase[Old] = "exited"))
               /\ (p > New => phase[New] # "off")
               /\ phase' = Set(phase, p, "building")
               /\ UNCHANGED <<gw, draining, termed, drained, conn, graceOpen, ownerState, lockHolder,
                              leaseHolder, leaseRel, claim, relStage, everConn, lossSeen, killSeen>>

\* build_runtime finished: owner connection, owner lock, heartbeat (runtime.py:50-110, 245-262)
Build(p) == /\ phase[p] = "building"
            /\ phase' = Set(phase, p, "ready")
            /\ conn' = Set(conn, p, TRUE)
            /\ graceOpen' = Set(graceOpen, p, TRUE)
            /\ UNCHANGED <<gw, draining, termed, drained, ownerState, lockHolder, leaseHolder,
                           leaseRel, claim, relStage, everConn, lossSeen, killSeen>>

\* ---- Acquire path (designed) ----
PollLock(p) == /\ phase[p] = "ready" /\ conn[p] /\ ~termed[p]
               /\ lockHolder = 0 /\ claim[p] = "none"
               /\ lockHolder' = p
               /\ claim' = Set(claim, p, IF TrustLockOnly THEN "go" ELSE "held")
               /\ UNCHANGED <<phase, gw, draining, termed, drained, conn, graceOpen, ownerState,
                              leaseHolder, leaseRel, relStage, everConn, lossSeen, killSeen>>

ReadLease(p) == /\ claim[p] = "held" /\ lockHolder = p
                /\ claim' = Set(claim, p, IF leaseHolder = 0 \/ leaseRel \/ leaseHolder = p
                                          THEN "go" ELSE "wait")
                /\ UNCHANGED <<phase, gw, draining, termed, drained, conn, graceOpen, ownerState,
                               lockHolder, leaseHolder, leaseRel, relStage, everConn, lossSeen, killSeen>>

Recheck(p) == /\ claim[p] = "wait" /\ lockHolder = p /\ ~Alive(leaseHolder)
              /\ claim' = Set(claim, p, "go")
              /\ UNCHANGED <<phase, gw, draining, termed, drained, conn, graceOpen, ownerState,
                             lockHolder, leaseHolder, leaseRel, relStage, everConn, lossSeen, killSeen>>

Take(p) == /\ claim[p] = "go" /\ lockHolder = p /\ ~termed[p] /\ ~TrustLockOnly
           /\ leaseHolder' = p
           /\ leaseRel' = IF ClearOnTake THEN FALSE ELSE leaseRel
           /\ claim' = Set(claim, p, "taken")
           /\ UNCHANGED <<phase, gw, draining, termed, drained, conn, graceOpen, ownerState,
                          lockHolder, relStage, everConn, lossSeen, killSeen>>

Connect(p) == /\ phase[p] = "ready" /\ ~termed[p] /\ lockHolder = p
              /\ (claim[p] = "taken" \/ (TrustLockOnly /\ claim[p] = "go"))
              /\ gw' = Set(gw, p, TRUE)
              /\ phase' = Set(phase, p, "connected")
              /\ everConn' = Set(everConn, p, TRUE)
              /\ UNCHANGED <<draining, termed, drained, conn, graceOpen, ownerState, lockHolder,
                             leaseHolder, leaseRel, claim, relStage, lossSeen, killSeen>>

\* ---- Shutdown path ----
Term(p) == /\ Running(p) /\ ~termed[p]
           /\ (p = Old => (~StopFirst => (phase[New] # "off"
                   /\ (WaitReady => phase[New] \in {"ready", "connected", "exited"}))))
           /\ (p > Old => everConn[p])
           /\ termed' = Set(termed, p, TRUE)
           /\ draining' = Set(draining, p, TRUE)
           /\ drained' = Set(drained, p, ~gw[p])
           /\ UNCHANGED <<phase, gw, conn, graceOpen, ownerState, lockHolder, leaseHolder, leaseRel,
                          claim, relStage, everConn, lossSeen, killSeen>>

DrainDone(p) == /\ Running(p) /\ draining[p] /\ gw[p] /\ ~drained[p]
                /\ drained' = Set(drained, p, TRUE)
                /\ UNCHANGED <<phase, gw, draining, termed, conn, graceOpen, ownerState, lockHolder,
                               leaseHolder, leaseRel, claim, relStage, everConn, lossSeen, killSeen>>

CloseGw(p) == /\ gw[p] /\ drained[p]
              /\ (UnlockBeforeClose => (relStage[p] = 2 \/ ~conn[p]))
              /\ gw' = Set(gw, p, FALSE)
              /\ UNCHANGED <<phase, draining, termed, drained, conn, graceOpen, ownerState, lockHolder,
                             leaseHolder, leaseRel, claim, relStage, everConn, lossSeen, killSeen>>

WriteReleased(p) == /\ Running(p) /\ termed[p] /\ drained[p] /\ everConn[p]
                    /\ (UnlockBeforeClose \/ ~gw[p])
                    /\ conn[p] /\ ownerState[p] = "ok" /\ lockHolder = p /\ relStage[p] = 0
                    /\ leaseHolder' = p /\ leaseRel' = TRUE
                    /\ relStage' = Set(relStage, p, 1)
                    /\ UNCHANGED <<phase, gw, draining, termed, drained, conn, graceOpen, ownerState,
                                   lockHolder, claim, everConn, lossSeen, killSeen>>

Unlock(p) == /\ relStage[p] = 1 /\ conn[p] /\ lockHolder = p
             /\ lockHolder' = 0
             /\ relStage' = Set(relStage, p, 2)
             /\ UNCHANGED <<phase, gw, draining, termed, drained, conn, graceOpen, ownerState,
                            leaseHolder, leaseRel, claim, everConn, lossSeen, killSeen>>

\* Graceful runtime teardown: heartbeat row deleted and owner lock released if the connection is alive.
Exit(p) == /\ Running(p) /\ ~gw[p]
           /\ (termed[p] \/ ownerState[p] = "lost")
           /\ (~everConn[p] \/ relStage[p] = 2 \/ ~conn[p] \/ ownerState[p] = "lost")
           /\ phase' = Set(phase, p, "exited")
           /\ graceOpen' = Set(graceOpen, p, IF conn[p] THEN FALSE ELSE graceOpen[p])
           /\ conn' = Set(conn, p, FALSE)
           /\ lockHolder' = Drop(p)
           /\ UNCHANGED <<gw, draining, termed, drained, ownerState, leaseHolder, leaseRel, claim,
                          relStage, everConn, lossSeen, killSeen>>

\* os._exit after ownership_lost (__main__.py:30-40): gateway socket dies with the process.
HardExit(p) == /\ Running(p) /\ ownerState[p] = "lost"
               /\ phase' = Set(phase, p, "exited")
               /\ gw' = Set(gw, p, FALSE)
               /\ conn' = Set(conn, p, FALSE)
               /\ lockHolder' = Drop(p)
               /\ UNCHANGED <<draining, termed, drained, graceOpen, ownerState, leaseHolder, leaseRel,
                              claim, relStage, everConn, lossSeen, killSeen>>

\* docker stop timeout (95 s) then SIGKILL (deploy.yml:372).
Kill(p) == /\ AllowKill /\ ~killSeen /\ Running(p) /\ termed[p]
           /\ phase' = Set(phase, p, "exited")
           /\ gw' = Set(gw, p, FALSE)
           /\ conn' = Set(conn, p, FALSE)
           /\ lockHolder' = Drop(p)
           /\ killSeen' = TRUE
           /\ UNCHANGED <<draining, termed, drained, graceOpen, ownerState, leaseHolder, leaseRel,
                          claim, relStage, everConn, lossSeen>>

\* ---- Owner connection loss on a connected process (runtime.py:130-166) ----
ConnLoss(p) == /\ AllowLoss /\ ~lossSeen /\ Running(p) /\ gw[p] /\ conn[p]
               /\ conn' = Set(conn, p, FALSE)
               /\ lockHolder' = Drop(p)
               /\ lossSeen' = TRUE
               /\ UNCHANGED <<phase, gw, draining, termed, drained, graceOpen, ownerState, leaseHolder,
                              leaseRel, claim, relStage, everConn, killSeen>>

Probe(p) == /\ Running(p) /\ ownerState[p] = "ok" /\ ~conn[p]
            /\ ownerState' = Set(ownerState, p, "retry")
            /\ UNCHANGED <<phase, gw, draining, termed, drained, conn, graceOpen, lockHolder,
                           leaseHolder, leaseRel, claim, relStage, everConn, lossSeen, killSeen>>

\* Reacquire owner lock (heartbeat unexpired) and, designed, the gateway lock if still free.
Recover(p) == /\ Running(p) /\ ownerState[p] = "retry" /\ graceOpen[p] /\ lockHolder = 0
              /\ conn' = Set(conn, p, TRUE)
              /\ lockHolder' = p
              /\ ownerState' = Set(ownerState, p, "ok")
              /\ UNCHANGED <<phase, gw, draining, termed, drained, graceOpen, leaseHolder, leaseRel,
                             claim, relStage, everConn, lossSeen, killSeen>>

GiveUp(p) == /\ Running(p) /\ ownerState[p] = "retry"
             /\ ownerState' = Set(ownerState, p, "lost")
             /\ draining' = Set(draining, p, TRUE)
             /\ UNCHANGED <<phase, gw, termed, drained, conn, graceOpen, lockHolder, leaseHolder,
                            leaseRel, claim, relStage, everConn, lossSeen, killSeen>>

\* Heartbeat age passes OWNER_LOSS_GRACE_S. Sound only because the holder stops within it (assumption A5).
GraceElapsed(p) == /\ graceOpen[p] /\ ~conn[p] /\ (ShortGrace \/ phase[p] = "exited")
                   /\ graceOpen' = Set(graceOpen, p, FALSE)
                   /\ UNCHANGED <<phase, gw, draining, termed, drained, conn, ownerState, lockHolder,
                                  leaseHolder, leaseRel, claim, relStage, everConn, lossSeen, killSeen>>

Next == \E p \in Procs :
          \/ StartNew(p) \/ Build(p) \/ PollLock(p) \/ ReadLease(p) \/ Recheck(p) \/ Take(p)
          \/ Connect(p) \/ Term(p) \/ DrainDone(p) \/ CloseGw(p) \/ WriteReleased(p) \/ Unlock(p)
          \/ Exit(p) \/ HardExit(p) \/ Kill(p) \/ ConnLoss(p) \/ Probe(p) \/ Recover(p)
          \/ GiveUp(p) \/ GraceElapsed(p)

Spec == Init /\ [][Next]_vars

\* Weak fairness on every protocol step and on the deploy's SIGTERM to Old; the
\* unclean events (loss, kill) are bounded to one each and stay unfair.
Fair == /\ \A p \in Procs : WF_vars(StartNew(p) \/ Build(p) \/ PollLock(p) \/ ReadLease(p) \/ Recheck(p)
                                   \/ Take(p) \/ Connect(p) \/ DrainDone(p) \/ CloseGw(p)
                                   \/ WriteReleased(p) \/ Unlock(p) \/ Exit(p) \/ HardExit(p)
                                   \/ Probe(p) \/ Recover(p) \/ GiveUp(p) \/ GraceElapsed(p))
        /\ WF_vars(Term(Old))
FairSpec == Spec /\ Fair

\* ---- Properties ----
TypeOK == /\ phase \in [Procs -> {"off", "building", "ready", "connected", "exited"}]
          /\ lockHolder \in Procs \cup {0}
          /\ leaseHolder \in Procs \cup {0}

\* (a) never two processes on the gateway at once
AtMostOneHandler == Cardinality({p \in Procs : gw[p]}) <= 1
\* weaker: never two processes accepting new work (a draining process rejects new mentions, bot.py:809)
AtMostOneAccepting == Cardinality({p \in Procs : gw[p] /\ ~draining[p]}) <= 1

\* (b) graceful run: once Old has closed and nobody is connected, New has already built its runtime
GapBounded == (~lossSeen /\ ~killSeen /\ (\A p \in Procs : ~gw[p]) /\ ~everConn[New])
                 => phase[New] = "ready"

\* witnesses: each is meant to be violated
NoUncleanHandover == ~(lossSeen /\ everConn[New])
NoKillHandover == ~(killSeen /\ everConn[New])
NoRecovery == ~(lossSeen /\ conn[Old] /\ Running(Old))
NoWaiting == claim[New] # "wait"

NewEventuallyConnects == <>(everConn[New])
==========================================================================
