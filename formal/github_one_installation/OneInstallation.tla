---------------------------- MODULE OneInstallation ----------------------------
EXTENDS Naturals, FiniteSets, Sequences

\* One agent, two GitHub installations, three concurrent grant writers. The
\* question: can the agent end up with grants on two installations?
\*
\* A writer is one of the three code paths that write an agent's grants:
\*   fast    explicit repo grant through the server-wide fast path, READ
\*           COMMITTED (github_connect.py, stage_grant).
\*   linked  explicit linked grant, SERIALIZABLE (github_linked_grant.py
\*           linked_grant, stage_grant).
\*   owner   owner grant, SERIALIZABLE (linked_owner_grant,
\*           require_owner_grant_installations, upsert_owner_grant).
\* Every writer first takes the agent's grant lock (lock_agent_grants), reads
\* the agent's explicit and owner grants, refuses when they sit on another
\* installation, writes its grant and commits.
\*
\* PostgreSQL semantics used (docs, PostgreSQL 16):
\*   - SERIALIZABLE takes one snapshot at the transaction's first statement
\*     and every later read sees it; READ COMMITTED takes a new snapshot per
\*     statement.
\*     https://www.postgresql.org/docs/16/transaction-iso.html
\*   - Row-level UPDATE / INSERT ... ON CONFLICT DO UPDATE waits for a
\*     concurrent transaction that touched the row; if that one commits, READ
\*     COMMITTED goes on with the new row version, and SERIALIZABLE fails with
\*     SQLSTATE 40001 when the row changed after its snapshot.
\*     https://www.postgresql.org/docs/16/transaction-iso.html
\*   - An advisory lock blocks others until commit and never refreshes a
\*     snapshot. A deadlock is detected after deadlock_timeout and one
\*     participant aborts with 40P01.
\*     https://www.postgresql.org/docs/16/explicit-locking.html
\*
\* Configuration:
\* Kinds           the writer kinds that may occur.
\* LockMode        "fence": lock_agent_grants upserts the agent's fence row
\*                 (version + 1), held until commit. "advisory": a transaction
\*                 advisory lock (the code at ac782e183).
\* FenceAborts     the 40001 on a fence row changed since the snapshot
\*                 (off = the PostgreSQL abort assumption removed).
\* OwnerOrder      "instFirst": the owner path locks the installation before
\*                 the agent lock, as the explicit path does. "fenceFirst":
\*                 the agent lock first (ac782e183).
\* MapSerialization, MapDeadlock
\*                 40001 / 40P01 becomes changed_try_again (off = the error
\*                 escapes the tool, an unhandled outcome).
\* SSI             SERIALIZABLE transactions abort at commit when another
\*                 SERIALIZABLE transaction committed a grant write after
\*                 their snapshot (an over-approximation of PostgreSQL's
\*                 serializable snapshot isolation, which never sees a READ
\*                 COMMITTED writer).
CONSTANTS Kinds, LockMode, FenceAborts, OwnerOrder, MapSerialization,
          MapDeadlock, SSI
ASSUME /\ Kinds \subseteq {"fast", "linked", "owner"} /\ Kinds # {}
       /\ LockMode \in {"fence", "advisory"}
       /\ FenceAborts \in BOOLEAN
       /\ OwnerOrder \in {"instFirst", "fenceFirst"}
       /\ MapSerialization \in BOOLEAN /\ MapDeadlock \in BOOLEAN
       /\ SSI \in BOOLEAN

Writers == {"w1", "w2", "w3"}
Insts == {"A", "B"}
None == "none"

\* Committed database state: installations that carry an explicit grant and
\* an owner grant of the agent, the committed fence version (0 = no row yet)
\* and the count of committed SERIALIZABLE writes.
VARIABLES expl, own, fv, sc
\* Locks: the agent lock (the fence row, or the advisory lock) and one lock per
\* installation row, each None or the holder.
VARIABLES agentLock, instLock
\* Per writer: its kind and target installation (chosen at Init), its position
\* in its step list, its outcome, and the snapshot it took.
VARIABLES kind, tgt, idx, out, snap
\* Ghosts: a deadlock was detected / a 40001 happened.
VARIABLES dl, sf

vars == <<expl, own, fv, sc, agentLock, instLock, kind, tgt, idx, out, snap,
          dl, sf>>

Serial(w) == kind[w] # "fast"

\* The step list of a writer.
Steps(w) ==
  CASE kind[w] = "fast"   -> <<"fence", "check", "commit">>
    [] kind[w] = "linked" -> <<"snap", "inst", "fence", "check", "commit">>
    [] kind[w] = "owner"  ->
         IF OwnerOrder = "instFirst"
           THEN <<"snap", "inst", "fence", "check", "commit">>
           ELSE <<"snap", "fence", "inst", "check", "commit">>

Running(w) == out[w] = "run" /\ idx[w] <= Len(Steps(w))
Next1(w) == Steps(w)[idx[w]]

Init ==
  /\ expl = {} /\ own = {} /\ fv = 0 /\ sc = 0
  /\ agentLock = None
  /\ instLock = [i \in Insts |-> None]
  /\ kind \in [Writers -> Kinds]
  /\ tgt \in [Writers -> Insts]
  /\ idx = [w \in Writers |-> 1]
  /\ out = [w \in Writers |-> "run"]
  /\ snap = [w \in Writers |-> [e |-> {}, o |-> {}, f |-> 0, c |-> 0]]
  /\ dl = FALSE /\ sf = FALSE

\* A writer ends without writing: locks released, outcome set. why is "refused"
\* (SecondInstallationError), "serial" (40001) or "deadlock" (40P01).
Abort(w, why) ==
  /\ agentLock' = IF agentLock = w THEN None ELSE agentLock
  /\ instLock' = [i \in Insts |-> IF instLock[i] = w THEN None ELSE instLock[i]]
  /\ out' = [out EXCEPT ![w] =
       CASE why = "refused" -> "refused"
         [] why = "serial"  -> IF MapSerialization THEN "tryAgain" ELSE "unhandled"
         [] why = "deadlock" -> IF MapDeadlock THEN "tryAgain" ELSE "unhandled"]
  /\ dl' = (dl \/ why = "deadlock")
  /\ sf' = (sf \/ why = "serial")
  /\ idx' = [idx EXCEPT ![w] = Len(Steps(w)) + 1]

\* The first statement of a SERIALIZABLE transaction fixes its snapshot.
DoSnap(w) ==
  /\ Running(w) /\ Next1(w) = "snap"
  /\ snap' = [snap EXCEPT ![w] = [e |-> expl, o |-> own, f |-> fv, c |-> sc]]
  /\ idx' = [idx EXCEPT ![w] = @ + 1]
  /\ UNCHANGED <<expl, own, fv, sc, agentLock, instLock, kind, tgt, out, dl, sf>>

\* get_for_update on the installation row; waits while another holds it.
DoInst(w) ==
  /\ Running(w) /\ Next1(w) = "inst"
  /\ instLock[tgt[w]] = None
  /\ instLock' = [instLock EXCEPT ![tgt[w]] = w]
  /\ idx' = [idx EXCEPT ![w] = @ + 1]
  /\ UNCHANGED <<expl, own, fv, sc, agentLock, kind, tgt, out, snap, dl, sf>>

\* lock_agent_grants. Fence: upsert the fence row once the holder is gone;
\* a SERIALIZABLE writer whose snapshot predates the committed version fails.
DoFence(w) ==
  /\ Running(w) /\ Next1(w) = "fence"
  /\ agentLock = None
  /\ IF LockMode = "fence" /\ FenceAborts /\ Serial(w) /\ fv > snap[w].f
       THEN Abort(w, "serial") /\ UNCHANGED <<expl, own, fv, sc, kind, tgt, snap>>
       ELSE /\ agentLock' = w
            /\ idx' = [idx EXCEPT ![w] = @ + 1]
            /\ UNCHANGED <<expl, own, fv, sc, instLock, kind, tgt, out, snap, dl, sf>>

\* The read of the agent's grants (list_agent_grants, list_owner_grants) and the
\* one-installation test. SERIALIZABLE reads its snapshot; READ COMMITTED reads
\* what is committed now.
DoCheck(w) ==
  /\ Running(w) /\ Next1(w) = "check"
  /\ LET E == IF Serial(w) THEN snap[w].e ELSE expl
         O == IF Serial(w) THEN snap[w].o ELSE own
     IN IF (E \cup O) \ {tgt[w]} # {}
          THEN Abort(w, "refused") /\ UNCHANGED <<expl, own, fv, sc, kind, tgt, snap>>
          ELSE /\ idx' = [idx EXCEPT ![w] = @ + 1]
               /\ UNCHANGED <<expl, own, fv, sc, agentLock, instLock, kind, tgt,
                              out, snap, dl, sf>>

\* Commit: the grant becomes visible, locks go. A SERIALIZABLE writer aborts if
\* the SSI over-approximation sees a concurrent SERIALIZABLE write.
DoCommit(w) ==
  /\ Running(w) /\ Next1(w) = "commit"
  /\ IF SSI /\ Serial(w) /\ sc > snap[w].c
       THEN Abort(w, "serial") /\ UNCHANGED <<expl, own, fv, sc, kind, tgt, snap>>
       ELSE /\ IF kind[w] = "owner"
                 THEN /\ own' = own \cup {tgt[w]} /\ expl' = expl
                 ELSE /\ expl' = expl \cup {tgt[w]} /\ own' = own
            /\ fv' = IF LockMode = "fence" THEN fv + 1 ELSE fv
            /\ sc' = IF Serial(w) THEN sc + 1 ELSE sc
            /\ agentLock' = IF agentLock = w THEN None ELSE agentLock
            /\ instLock' = [i \in Insts |-> IF instLock[i] = w THEN None ELSE instLock[i]]
            /\ out' = [out EXCEPT ![w] = "committed"]
            /\ idx' = [idx EXCEPT ![w] = Len(Steps(w)) + 1]
            /\ UNCHANGED <<kind, tgt, snap, dl, sf>>

\* Deadlock detection. w waits for the holder of the lock its next step needs.
Needed(w) ==
  IF Next1(w) = "inst" THEN instLock[tgt[w]]
  ELSE IF Next1(w) = "fence" THEN agentLock ELSE None
Blocked(w) == Running(w) /\ Needed(w) # None
Waits(x) == IF x # None /\ Blocked(x) THEN Needed(x) ELSE None
OnCycle(w) ==
  LET n1 == Waits(w)  n2 == Waits(n1)  n3 == Waits(n2)
  IN w \in {n1, n2, n3}

DoDeadlock(w) ==
  /\ Blocked(w) /\ OnCycle(w)
  /\ Abort(w, "deadlock")
  /\ UNCHANGED <<expl, own, fv, sc, kind, tgt, snap>>

Next ==
  \E w \in Writers :
    DoSnap(w) \/ DoInst(w) \/ DoFence(w) \/ DoCheck(w) \/ DoCommit(w)
    \/ DoDeadlock(w)

Spec == Init /\ [][Next]_vars
LiveSpec == Spec /\ WF_vars(Next)

\* ---- Properties -----------------------------------------------------------

\* The agent's explicit and owner grants span at most one installation.
AtMostOneInstallation == Cardinality(expl \cup own) <= 1

\* Every writer ends with a grant, a refusal or a clean try-again; no error
\* escapes the tool.
NoUnhandledOutcome == \A w \in Writers : out[w] # "unhandled"

\* A deadlock is never reached.
NoDeadlock == ~dl

\* Every writer ends (a deadlock is always detected, nobody waits forever).
AllEnd == \A w \in Writers : <>(out[w] # "run")

\* Witnesses, each violated on purpose.
NoSerializationFailure == ~sf
NeverRefused == \A w \in Writers : out[w] # "refused"
NoOwnerCommit == \A w \in Writers : ~(kind[w] = "owner" /\ out[w] = "committed")
NoTwoCommits ==
  Cardinality({w \in Writers : out[w] = "committed"}) < 2
================================================================================
