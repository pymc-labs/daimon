---------------------- MODULE SessionCreateCleanup ----------------------
(***************************************************************************)
(* One turn creates (or adopts) an upstream Managed Agents session and     *)
(* either commits a row naming it or fails.  Question: can a turn leave a  *)
(* live upstream session that no row names, and can a cleanup archive a   *)
(* session that a row names or that another turn owns?                     *)
(*                                                                         *)
(* A session is owned by exactly one party at a time:                      *)
(*   inside create_session           : create_session (assumed correct)    *)
(*   create returned .. CreatedSession returned : create_ma_session        *)
(*   after CreatedSession is returned : the caller                         *)
(* The unsafe configs remove or misplace one of these cleanups.            *)
(***************************************************************************)
EXTENDS Naturals, TLC

CONSTANTS
    MaxAttempts,            \* attempts per turn (retry after a failed one)
    CleanupInCreate,        \* create_ma_session archives on a failure in its post-create window
    CallerCleans,           \* the caller archives a CreatedSession it holds when it fails
    CallerArchivesAdopted,  \* the caller also archives an adopted session when it fails (rejected design)
    AmbiguousCommit,        \* a commit can land and still raise in the caller
    ReplayPossible,         \* attempt 1's create is a replay of an earlier attempt's recorded create
    GuardReplays,           \* the window archives a replayed session only if a row read shows no row names it
    RowReadFails            \* that row read fails (the replayed session is then left alone)

VARIABLES
    up,       \* up[i]: upstream state of the session attempt i created
    row,      \* row[i]: a committed row names that session
    by,       \* by[i]: which cleanup archived it
    upA,      \* upstream state of the session another turn created and a row names
    att,      \* the attempt in flight
    pc,       \* where the in-flight attempt is
    outcome   \* of the in-flight attempt

vars == <<up, row, by, upA, att, pc, outcome>>

Attempts == 1..MaxAttempts

\* A replayed session was created by an earlier attempt: live upstream, and that
\* attempt may or may not have committed a row for it before it died.
ReplayedRow == IF ReplayPossible THEN BOOLEAN ELSE {FALSE}

Init ==
    \E named \in ReplayedRow :
        /\ up = [i \in Attempts |-> IF ReplayPossible /\ i = 1 THEN "live" ELSE "none"]
        /\ row = [i \in Attempts |-> named /\ i = 1]
        /\ by = [i \in Attempts |-> "none"]
        /\ upA = "live"
        /\ att = 1
        /\ pc = "idle"
        /\ outcome = "none"

Archive(i, who) ==
    /\ up' = [up EXCEPT ![i] = "archived"]
    /\ by' = [by EXCEPT ![i] = who]

\* The turn finds no live row to adopt and builds a session.
StartCreate ==
    /\ pc = "idle"
    /\ pc' = "in_create"
    /\ UNCHANGED <<up, row, by, upA, att, outcome>>

\* Recovery finds a live row another turn created and adopts it; create_ma_session is never called.
StartAdopt ==
    /\ pc = "idle"
    /\ ~(ReplayPossible /\ att = 1)   \* a replaying attempt is one that creates
    /\ pc' = "adopted"
    /\ UNCHANGED <<up, row, by, upA, att, outcome>>

\* sessions.create returns and the lookup starts: no await between them.
Create ==
    /\ pc = "in_create"
    /\ IF ReplayPossible /\ att = 1
          THEN UNCHANGED up
          ELSE up' = [up EXCEPT ![att] = "live"]
    /\ pc' = "lookup"
    /\ UNCHANGED <<row, by, upA, att, outcome>>

\* create_session failed: it archived its own session (assumed), the turn fails.
\* Not applied to a replay: what create_session does with a replayed session when it
\* fails is its own cleanup and is not modelled.
FailBeforeCreate ==
    /\ pc = "in_create"
    /\ ~(ReplayPossible /\ att = 1)
    /\ pc' = "done"
    /\ outcome' = "fail"
    /\ UNCHANGED <<up, row, by, upA, att>>

LookupDone ==
    /\ pc = "lookup"
    /\ pc' = "closing"
    /\ UNCHANGED <<up, row, by, upA, att, outcome>>

CloseDone ==
    /\ pc = "closing"
    /\ pc' = "returned"
    /\ UNCHANGED <<up, row, by, upA, att, outcome>>

\* What the window cleanup may archive: a session this attempt created, or a replayed
\* one that the row read shows no row names (a failed read archives nothing).
WindowMayArchive(i) ==
    IF ReplayPossible /\ i = 1 /\ GuardReplays THEN ~row[i] /\ ~RowReadFails ELSE TRUE

\* A cancel, a ceiling or a database error at either await of the window.
FailInWindow ==
    /\ pc \in {"lookup", "closing"}
    /\ IF CleanupInCreate /\ WindowMayArchive(att)
          THEN Archive(att, "create")
          ELSE UNCHANGED <<up, by>>
    /\ pc' = "done"
    /\ outcome' = "fail"
    /\ UNCHANGED <<row, upA, att>>

\* The caller inserts the row in its own transaction.
Insert ==
    /\ pc = "returned"
    /\ pc' = "inserted"
    /\ UNCHANGED <<up, row, by, upA, att, outcome>>

Commit ==
    /\ pc = "inserted"
    /\ row' = [row EXCEPT ![att] = TRUE]
    /\ pc' = "done"
    /\ outcome' = "success"
    /\ UNCHANGED <<up, by, upA, att>>

\* The commit lands but the caller sees an error and cleans up.
CommitThenError ==
    /\ AmbiguousCommit
    /\ pc = "inserted"
    /\ row' = [row EXCEPT ![att] = TRUE]
    /\ IF CallerCleans THEN Archive(att, "caller") ELSE UNCHANGED <<up, by>>
    /\ pc' = "done"
    /\ outcome' = "fail"
    /\ UNCHANGED <<upA, att>>

\* A cancel, a ceiling or an error after CreatedSession was returned, before the commit.
CallerFail ==
    /\ pc \in {"returned", "inserted"}
    /\ IF CallerCleans THEN Archive(att, "caller") ELSE UNCHANGED <<up, by>>
    /\ pc' = "done"
    /\ outcome' = "fail"
    /\ UNCHANGED <<row, upA, att>>

AdoptedOk ==
    /\ pc = "adopted"
    /\ pc' = "done"
    /\ outcome' = "success"
    /\ UNCHANGED <<up, row, by, upA, att>>

AdoptedFail ==
    /\ pc = "adopted"
    /\ IF CallerArchivesAdopted THEN upA' = "archived" ELSE UNCHANGED upA
    /\ pc' = "done"
    /\ outcome' = "fail"
    /\ UNCHANGED <<up, row, by, att>>

\* A skill refusal, a busy seal or the user asking again: a new attempt with a new session.
Retry ==
    /\ pc = "done"
    /\ outcome = "fail"
    /\ att < MaxAttempts
    /\ att' = att + 1
    /\ pc' = "idle"
    /\ outcome' = "none"
    /\ UNCHANGED <<up, row, by, upA>>

Next ==
    \/ StartCreate \/ StartAdopt \/ Create \/ FailBeforeCreate
    \/ LookupDone \/ CloseDone \/ FailInWindow
    \/ Insert \/ Commit \/ CommitThenError \/ CallerFail
    \/ AdoptedOk \/ AdoptedFail \/ Retry

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ up \in [Attempts -> {"none", "live", "archived"}]
    /\ row \in [Attempts -> BOOLEAN]
    /\ by \in [Attempts -> {"none", "create", "caller"}]
    /\ upA \in {"live", "archived"}
    /\ att \in Attempts
    /\ pc \in {"idle", "in_create", "lookup", "closing", "returned", "inserted", "adopted", "done"}
    /\ outcome \in {"none", "success", "fail"}

Finished(i) == i < att \/ pc = "done"

\* Once an attempt is over, a live upstream session is one a row names.
NoOrphan == \A i \in Attempts : Finished(i) => (up[i] = "live" => row[i])

\* A session a committed row names is never archived.
NoArchiveNamed == \A i \in Attempts : row[i] => up[i] # "archived"

\* The cleanup inside create_ma_session never archives a session a row names.
WindowNeverArchivesNamed == \A i \in Attempts : by[i] = "create" => ~row[i]

\* The session another turn created and a row names is never archived by this turn.
NoArchiveAdopted == upA = "live"

\* A successful turn keeps a live session.
SuccessNotArchived == outcome = "success" => (up[att] # "archived" /\ upA = "live")

\* Witnesses: each is expected to be violated, which shows the path is reachable.
NoWindowCleanup == \A i \in Attempts : by[i] # "create"
NoCreatedSuccess == ~(outcome = "success" /\ row[att])
====================================================================
