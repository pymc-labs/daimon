------------------------------ MODULE VaultSlot ------------------------------
(***************************************************************************)
(* One person's per-(account, agent) MA vault and the credential slot for   *)
(* one external MCP server URL X. MA keeps at most one credential per URL   *)
(* in a vault: a second create at X is a 409, and update/delete of an id    *)
(* that is gone is a 404. Writers:                                          *)
(*   Ensure  - ensure_agent_mcp_vault get-or-create (advisory lock, SYNC-01)*)
(*   Mirror  - mirror_credentials_into_vault: the agent's shared token for X*)
(*             (create_session and RemirrorVaultCredentials; no lock)       *)
(*   OAuth   - put_mcp_oauth_credential after the callback consumed the     *)
(*             flow: list, delete whatever holds X, create mcp_oauth        *)
(* Each MA call is one atomic step; list results are snapshots.             *)
(*                                                                         *)
(* Source: packages/core/daimon/core/mcp_vault.py,                          *)
(* packages/core/daimon/core/agent_mcp_credentials.py,                      *)
(* packages/core/daimon/core/mcp_oauth/vault.py, mcp_oauth/complete.py.     *)
(***************************************************************************)
EXTENDS Naturals, TLC

CONSTANTS
    EnsureActors,       \* concurrent session creates bootstrapping the vault
    Mirrors,            \* concurrent mirrors of the agent's shared token
    WithOAuth,          \* a consumed OAuth callback writes its grant
    InitSlot,           \* "empty" | "static_old" | "static_new" | "oauth"
    VaultLock,          \* 2cbe69e: get-or-create under pg_advisory_xact_lock
    MirrorSkipsGrant,   \* 39450e8: a URL held by an mcp_oauth grant is left alone
    MirrorCatches409,   \* 39450e8: a create that loses to a concurrent writer is fine
    RotateInPlace,      \* dc2d866: a stale stamp is updated in place
    MirrorRetries404,   \* #230 (merged): an update whose id vanished re-reads the slot
    OAuthRetries,       \* #230 (merged): the grant write re-reads and replaces on 404/409
    OAuthLocked,        \* #239 (merged): grant write and mirror under the vault lock
    MaxTries

None == "none"
DbVer == "new"   \* the agent's stored token (agent_mcp_credentials.updated_at)
Slot(k, v, i) == [kind |-> k, ver |-> v, id |-> i]
InitialSlot ==
    CASE InitSlot = "empty" -> Slot("empty", "na", 0)
      [] InitSlot = "static_old" -> Slot("static", "old", 1)
      [] InitSlot = "static_new" -> Slot("static", "new", 1)
      [] InitSlot = "oauth" -> Slot("oauth", "na", 1)
Actors == EnsureActors \cup Mirrors \cup {"oauth"}

VARIABLES
    vaults,    \* vaults carrying this (account, agent)'s display name
    slot,      \* credential at X in the canonical vault
    nextId,
    lock,      \* holder of the per-(account, agent) advisory lock
    pcE, snapE,
    pcM, snapM, doneSlot,
    pcO, snapO, tries

vars == <<vaults, slot, nextId, lock, pcE, snapE, pcM, snapM, doneSlot, pcO, snapO, tries>>

Init ==
    /\ vaults = IF EnsureActors = {} THEN 1 ELSE 0
    /\ slot = InitialSlot
    /\ nextId = 2
    /\ lock = None
    /\ pcE = [e \in EnsureActors |-> "idle"]
    /\ snapE = [e \in EnsureActors |-> 0]
    /\ pcM = [m \in Mirrors |-> "idle"]
    /\ snapM = [m \in Mirrors |-> InitialSlot]
    /\ doneSlot = [m \in Mirrors |-> InitialSlot]
    /\ pcO = IF WithOAuth THEN "idle" ELSE "done"
    /\ snapO = InitialSlot
    /\ tries = 0

Acquire(a) == IF lock = None THEN lock' = a ELSE FALSE
Release(a) == lock' = IF lock = a THEN None ELSE lock

(* ---- Ensure: list vaults by name, create when none ---- *)
EnsureStart(e) ==
    /\ pcE[e] = "idle"
    /\ IF VaultLock THEN Acquire(e) ELSE UNCHANGED lock
    /\ pcE' = [pcE EXCEPT ![e] = "locked"]
    /\ UNCHANGED <<vaults, slot, nextId, snapE, pcM, snapM, doneSlot, pcO, snapO, tries>>

EnsureList(e) ==
    /\ pcE[e] = "locked"
    /\ snapE' = [snapE EXCEPT ![e] = vaults]
    /\ pcE' = [pcE EXCEPT ![e] = "listed"]
    /\ UNCHANGED <<vaults, slot, nextId, lock, pcM, snapM, doneSlot, pcO, snapO, tries>>

EnsureCreate(e) ==
    /\ pcE[e] = "listed"
    /\ vaults' = IF snapE[e] = 0 THEN vaults + 1 ELSE vaults
    /\ pcE' = [pcE EXCEPT ![e] = "done"]
    /\ Release(e)
    /\ UNCHANGED <<slot, nextId, snapE, pcM, snapM, doneSlot, pcO, snapO, tries>>

(* ---- Mirror: list, then create / update / skip per URL ---- *)
MirrorStart(m) ==
    /\ pcM[m] = "idle"
    /\ vaults >= 1
    /\ IF OAuthLocked THEN Acquire(m) ELSE UNCHANGED lock
    /\ snapM' = [snapM EXCEPT ![m] = slot]
    /\ pcM' = [pcM EXCEPT ![m] = "listed"]
    /\ UNCHANGED <<vaults, slot, nextId, pcE, snapE, doneSlot, pcO, snapO, tries>>

\* `observed` is the slot state the mirror's decision rests on: its own write,
\* the winner of a 409, or the listed snapshot when it chose to do nothing.
MirrorFinish(m, result, observed) ==
    /\ pcM' = [pcM EXCEPT ![m] = result]
    /\ doneSlot' = [doneSlot EXCEPT ![m] = observed]
    /\ IF OAuthLocked THEN Release(m) ELSE UNCHANGED lock

MirrorCreate(m) ==
    IF slot.kind = "empty"
       THEN /\ slot' = Slot("static", DbVer, nextId)
            /\ nextId' = nextId + 1
            /\ MirrorFinish(m, "done", Slot("static", DbVer, nextId))
       ELSE /\ UNCHANGED <<slot, nextId>>
            /\ MirrorFinish(m, IF MirrorCatches409 THEN "done" ELSE "failed409", slot)

MirrorAct(m) ==
    LET s == snapM[m] IN
    /\ pcM[m] = "listed"
    /\ CASE s.kind = "oauth" /\ MirrorSkipsGrant ->
                /\ UNCHANGED <<slot, nextId>> /\ MirrorFinish(m, "done", s)
         [] (s.kind = "oauth" /\ ~MirrorSkipsGrant) \/ s.kind = "empty" ->
                MirrorCreate(m)
         [] s.kind = "static" /\ s.ver = DbVer ->
                /\ UNCHANGED <<slot, nextId>> /\ MirrorFinish(m, "done", s)
         [] s.kind = "static" /\ s.ver # DbVer /\ ~RotateInPlace ->
                /\ UNCHANGED <<slot, nextId>> /\ MirrorFinish(m, "done", s)
         [] s.kind = "static" /\ s.ver # DbVer /\ RotateInPlace ->
                IF slot.id = s.id
                   THEN /\ slot' = [slot EXCEPT !.ver = DbVer]
                        /\ UNCHANGED nextId
                        /\ MirrorFinish(m, "done", [slot EXCEPT !.ver = DbVer])
                   ELSE IF MirrorRetries404
                      THEN \* NotFound: re-read the slot and decide again
                           /\ UNCHANGED <<slot, nextId, doneSlot>>
                           /\ pcM' = [pcM EXCEPT ![m] = "relist"]
                           /\ UNCHANGED lock
                      ELSE /\ UNCHANGED <<slot, nextId>>
                           /\ MirrorFinish(m, "failed404", slot)
    /\ UNCHANGED <<vaults, pcE, snapE, pcO, snapO, tries, snapM>>

MirrorRelist(m) ==
    /\ pcM[m] = "relist"
    /\ snapM' = [snapM EXCEPT ![m] = slot]
    /\ pcM' = [pcM EXCEPT ![m] = "listed"]
    /\ UNCHANGED <<vaults, slot, nextId, lock, pcE, snapE, doneSlot, pcO, snapO, tries>>

(* ---- OAuth grant write: list, delete what holds X, create mcp_oauth ---- *)
OAuthList ==
    /\ pcO \in {"idle", "retry"}
    /\ vaults >= 1
    /\ IF OAuthLocked /\ pcO = "idle" THEN Acquire("oauth") ELSE UNCHANGED lock
    /\ snapO' = slot
    /\ pcO' = "listed"
    /\ UNCHANGED <<vaults, slot, nextId, pcE, snapE, pcM, snapM, doneSlot, tries>>

OAuthFail ==
    IF OAuthRetries /\ tries < MaxTries
       THEN /\ pcO' = "retry" /\ tries' = tries + 1 /\ UNCHANGED lock
       ELSE /\ pcO' = "lost" /\ UNCHANGED tries
            /\ IF OAuthLocked THEN Release("oauth") ELSE UNCHANGED lock

OAuthDelete ==
    /\ pcO = "listed"
    /\ IF snapO.kind = "empty"
          THEN /\ pcO' = "deleted" /\ UNCHANGED <<slot, tries, lock>>
          ELSE IF slot.id = snapO.id
             THEN /\ slot' = Slot("empty", "na", 0)
                  /\ pcO' = "deleted"
                  /\ UNCHANGED <<tries, lock>>
             ELSE /\ UNCHANGED slot /\ OAuthFail   \* 404 on delete
    /\ UNCHANGED <<vaults, nextId, pcE, snapE, pcM, snapM, doneSlot, snapO>>

OAuthCreate ==
    /\ pcO = "deleted"
    /\ IF slot.kind = "empty"
          THEN /\ slot' = Slot("oauth", "na", nextId)
               /\ nextId' = nextId + 1
               /\ pcO' = "done"
               /\ UNCHANGED tries
               /\ IF OAuthLocked THEN Release("oauth") ELSE UNCHANGED lock
          ELSE /\ UNCHANGED <<slot, nextId>> /\ OAuthFail   \* 409 on create
    /\ UNCHANGED <<vaults, pcE, snapE, pcM, snapM, doneSlot, snapO>>

Quiescent ==
    /\ \A e \in EnsureActors : pcE[e] = "done"
    /\ \A m \in Mirrors : pcM[m] \in {"done", "failed409", "failed404"}
    /\ pcO \in {"done", "lost"}
    /\ UNCHANGED vars

Next ==
    \/ \E e \in EnsureActors : EnsureStart(e) \/ EnsureList(e) \/ EnsureCreate(e)
    \/ \E m \in Mirrors : MirrorStart(m) \/ MirrorAct(m) \/ MirrorRelist(m)
    \/ OAuthList \/ OAuthDelete \/ OAuthCreate
    \/ Quiescent

Spec == Init /\ [][Next]_vars

SlotType == [kind : {"empty", "static", "oauth"}, ver : {"na", "old", "new"}, id : 0..10]
TypeOK ==
    /\ vaults \in 0..3
    /\ slot \in SlotType
    /\ lock \in Actors \cup {None}
    /\ pcO \in {"idle", "retry", "listed", "deleted", "done", "lost"}
    /\ tries \in 0..MaxTries

\* 2cbe69e: one vault per (account, agent).
NoDuplicateVault == vaults <= 1
\* 39450e8: mirroring the shared token never fails a turn on a URL the grant holds.
MirrorNeverFails409 == \A m \in Mirrors : pcM[m] # "failed409"
\* dc2d866: a finished mirror leaves the grant or the current token at X.
MirrorLeavesCurrent ==
    \A m \in Mirrors : pcM[m] = "done" =>
        (doneSlot[m].kind = "oauth" \/ (doneSlot[m].kind = "static" /\ doneSlot[m].ver = DbVer))
\* T2 reverse: an update never 404s because the grant write replaced the credential.
MirrorNever404 == \A m \in Mirrors : pcM[m] # "failed404"
\* T2: a consumed sign-in is never lost to a concurrent static writer, and the
\* grant it stored is not overwritten afterwards.
SignInNeverLost == pcO # "lost"
GrantSurvives == pcO = "done" => slot.kind = "oauth"
=============================================================================
