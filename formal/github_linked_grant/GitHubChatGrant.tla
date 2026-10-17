----------------------------- MODULE GitHubChatGrant -----------------------------
EXTENDS Naturals, FiniteSets, TLC

\* IdentityCheck:      the platform callback verifies the chat identity.
\* CommitRecheck:      the grant transaction rechecks link, generation, coverage
\*                     and the requester's authority under its locks (off = the
\*                     pre-fix commit that trusted the earlier lookup).
\* LateCoverageCheck:  the late snapshot rejects when the installation row exists
\*                     and no longer covers the repo. An early exit; the check
\*                     under the installation lock also decides.
\* LockRepoCheck:      the check under the installation lock also requires the
\*                     repo to be covered (off = the spike, which compared the
\*                     snapshot's name set with the current one and nothing else).
\* Authority at commit comes from two kinds of stored rows, each revoked by its
\* own write: the requester's role (the Account row and the channel-admin rows,
\* read first by read_stored_admin) and the agent's access-policy ownership
\* rule (read by _caller_holds when the role does not already grant). The
\* requester has authority while either one holds.
\* RoleRead, RuleRead: how the transaction reads each source.
\*   "locked"       SELECT ... FOR SHARE. A row changed after the snapshot
\*                  aborts the transaction (serialization failure); a change
\*                  after the lock waits for the transaction to finish.
\*   "snapshot"     plain read: the snapshot's value, no lock.
\*   "lockedStale"  lock held, but the read returns the snapshot's value
\*                  instead of aborting. Only here to show the clean result
\*                  leans on PostgreSQL's abort rule.
CONSTANTS IdentityCheck, CommitRecheck, LateCoverageCheck, LockRepoCheck,
          RoleRead, RuleRead
ASSUME /\ IdentityCheck \in BOOLEAN /\ CommitRecheck \in BOOLEAN
       /\ LateCoverageCheck \in BOOLEAN /\ LockRepoCheck \in BOOLEAN
       /\ RoleRead \in {"locked", "snapshot", "lockedStale"}
       /\ RuleRead \in {"locked", "snapshot", "lockedStale"}

Users == {"manager", "member"}
GitHubUsers == {"gh1", "gh2"}
Agents == {"shared", "own"}
Repos == {"widgets", "other"}
None == "none"

\* Link and callback state.
VARIABLES phase, owner, opener, githubIdentity, proven, link, generation,
          saves, replayed, foreignEver
\* Stored authority: role pairs and policy-rule pairs (person, agent), the
\* installation row's repo_full_names
\* (covered), whether the row exists, GitHub's per-user permission, and the
\* active authorization rows that upsert_github_app rebuilds covered from.
VARIABLES roles, rules, covered, instPresent, auth, permitted
\* One request: its stage, inputs, what the lookup saw, the late snapshot of
\* the installation row, and the role and rule the transaction's snapshot holds
\* (roleChanged, ruleChanged: that row was written after the snapshot;
\* ruleLocked: the rule row is held FOR SHARE).
VARIABLES req, requester, agent, repo, checkedIdentity, checkedGeneration,
          checkedPermission, snapPresent, snapNames, snapRole, roleChanged,
          snapRule, ruleChanged, ruleLocked
\* Ghosts: written by actions, never read by them.
VARIABLES grant, unsafeEver, stalePermissionEver, removedEver, restoredEver,
          staleAuthEver

idVars == <<phase, owner, opener, githubIdentity, proven, link, generation,
            saves, replayed, foreignEver>>
authVars == <<roles, rules, covered, instPresent, auth, permitted>>
reqVars == <<req, requester, agent, repo, checkedIdentity, checkedGeneration,
             checkedPermission, snapPresent, snapNames, snapRole, roleChanged,
          snapRule, ruleChanged, ruleLocked>>
ghostVars == <<grant, unsafeEver, stalePermissionEver, removedEver,
               restoredEver, staleAuthEver>>
vars == <<idVars, authVars, reqVars, ghostVars>>

Init ==
  /\ phase = "unminted"
  /\ owner = None /\ opener = None /\ githubIdentity = None /\ proven = FALSE
  /\ link = [u \in Users |-> None]
  /\ generation = [u \in Users |-> 0]
  /\ saves = 0 /\ replayed = FALSE /\ foreignEver = FALSE
  /\ roles = {<<"manager", "shared">>, <<"manager", "own">>}
  /\ rules = {<<"manager", "own">>, <<"member", "own">>}
  /\ instPresent \in BOOLEAN
  /\ covered = IF instPresent THEN Repos ELSE {}
  /\ auth = covered
  /\ permitted = Users \X Repos
  /\ req = "idle" /\ requester = None /\ agent = None /\ repo = None
  /\ checkedIdentity = None /\ checkedGeneration = 0
  /\ checkedPermission = FALSE
  /\ snapPresent = FALSE /\ snapNames = {} /\ snapRole = FALSE
  /\ snapRule = FALSE /\ ruleChanged = FALSE /\ ruleLocked = FALSE
  /\ roleChanged = FALSE
  /\ grant = FALSE /\ unsafeEver = FALSE /\ stalePermissionEver = FALSE
  /\ removedEver = FALSE /\ restoredEver = FALSE /\ staleAuthEver = FALSE

-------------------------------------------------------------------------------
\* Link and callback.

Mint(u) ==
  /\ phase = "unminted" /\ u \in Users
  /\ phase' = "new" /\ owner' = u
  /\ UNCHANGED <<opener, githubIdentity, proven, link, generation, saves,
                  replayed, foreignEver, authVars, reqVars, ghostVars>>

Open(u) ==
  /\ phase = "new" /\ u \in Users
  /\ phase' = "platform" /\ opener' = u
  /\ UNCHANGED <<owner, githubIdentity, proven, link, generation, saves,
                  replayed, foreignEver, authVars, reqVars, ghostVars>>

PlatformCallback ==
  /\ phase = "platform"
  /\ (IdentityCheck => opener = owner)
  /\ phase' = "github" /\ proven' = (opener = owner)
  /\ UNCHANGED <<owner, opener, githubIdentity, link, generation, saves,
                  replayed, foreignEver, authVars, reqVars, ghostVars>>

RejectForeign ==
  /\ phase = "platform" /\ IdentityCheck /\ opener # owner
  /\ phase' = "rejected"
  /\ UNCHANGED <<owner, opener, githubIdentity, proven, link, generation,
                  saves, replayed, foreignEver, authVars, reqVars, ghostVars>>

GitHubCallback(g) ==
  /\ phase = "github" /\ g \in GitHubUsers
  /\ phase' = "used" /\ githubIdentity' = g
  /\ link' = [link EXCEPT ![owner] = g]
  /\ generation' = [generation EXCEPT ![owner] = @ + 1]
  /\ saves' = saves + 1
  /\ foreignEver' = (foreignEver \/ ~proven)
  /\ UNCHANGED <<owner, opener, proven, replayed, authVars, reqVars,
                  ghostVars>>

ReplayCallback ==
  /\ phase = "used" /\ ~replayed
  /\ replayed' = TRUE
  /\ UNCHANGED <<phase, owner, opener, githubIdentity, proven, link,
                  generation, saves, foreignEver, authVars, reqVars,
                  ghostVars>>

-------------------------------------------------------------------------------
\* The grant request. Stages: checking -> discovered -> looked -> checked ->
\* begun -> (roleRead ->) authRead -> done, or denied from any stage after the
\* first. roleRead: the role did not grant, the policy rule is read next.
\* authRead: authority decided.

Request(u, a, r) ==
  /\ req = "idle" /\ u \in Users /\ a \in Agents /\ r \in Repos
  /\ req' = "checking" /\ requester' = u /\ agent' = a /\ repo' = r
  /\ UNCHANGED <<idVars, authVars, checkedIdentity, checkedGeneration,
                  checkedPermission, snapPresent, snapNames, snapRole,
                  roleChanged, snapRule, ruleChanged, ruleLocked, ghostVars>>

\* linked_grant: read the link generation and token, list the user's
\* installations; GitHub says the repo is covered. The repo GET is now
\* outstanding.
Discover ==
  /\ req = "checking"
  /\ req' = "discovered"
  /\ checkedIdentity' = link[requester]
  /\ checkedGeneration' = generation[requester]
  /\ UNCHANGED <<idVars, authVars, requester, agent, repo, checkedPermission,
                  snapPresent, snapNames, snapRole, roleChanged, snapRule,
                  ruleChanged, ruleLocked, ghostVars>>

\* The repo GET answers with the user's permission.
CheckPermission ==
  /\ req = "discovered"
  /\ req' = "looked"
  /\ checkedPermission' = (<<requester, repo>> \in permitted)
  /\ UNCHANGED <<idVars, authVars, requester, agent, repo, checkedIdentity,
                  checkedGeneration, snapPresent, snapNames, snapRole,
                  roleChanged, snapRule, ruleChanged, ruleLocked, ghostVars>>

LateRejects == LateCoverageCheck /\ instPresent /\ repo \notin covered

\* Late snapshot of the installation row, read after the repo GET.
LateSnapshot ==
  /\ req = "looked" /\ ~LateRejects
  /\ req' = "checked"
  /\ snapPresent' = instPresent /\ snapNames' = covered
  /\ UNCHANGED <<idVars, authVars, requester, agent, repo, checkedIdentity,
                  checkedGeneration, checkedPermission, snapRole, roleChanged, snapRule,
                  ruleChanged, ruleLocked,
                  ghostVars>>

LateReject ==
  /\ req = "looked" /\ LateRejects
  /\ req' = "denied"
  /\ UNCHANGED <<idVars, authVars, requester, agent, repo, checkedIdentity,
                  checkedGeneration, checkedPermission, snapPresent, snapNames,
                  snapRole, roleChanged, snapRule,
                  ruleChanged, ruleLocked, ghostVars>>

\* Under the installation lock: an existing row must be the snapshot's row
\* with the same names (and, unless the spike's bug is on, must cover the
\* repo); a missing row must still be missing.
LockCoverageOk ==
  IF instPresent
    THEN snapPresent /\ covered = snapNames
         /\ (LockRepoCheck => repo \in covered)
    ELSE ~snapPresent

\* First statements of the SERIALIZABLE transaction: the link, user and
\* installation rows are locked FOR UPDATE and checked.
BeginOk ==
  /\ checkedIdentity # None /\ checkedPermission
  /\ CommitRecheck =>
       /\ link[requester] = checkedIdentity
       /\ generation[requester] = checkedGeneration
       /\ LockCoverageOk

\* Authority is "has a role or a policy rule" for the (person, agent) pair.
Authority(u, a) == <<u, a>> \in roles \/ <<u, a>> \in rules

\* The transaction's snapshot is taken at its first SELECT (the link lock) and
\* holds the requester's role and the agent's policy rule as of now.
Begin ==
  /\ req = "checked" /\ BeginOk
  /\ req' = "begun"
  /\ snapRole' = (<<requester, agent>> \in roles)
  /\ snapRule' = (<<requester, agent>> \in rules)
  /\ roleChanged' = FALSE /\ ruleChanged' = FALSE /\ ruleLocked' = FALSE
  /\ UNCHANGED <<idVars, authVars, requester, agent, repo, checkedIdentity,
                  checkedGeneration, checkedPermission, snapPresent, snapNames,
                  ghostVars>>

BeginReject ==
  /\ req = "checked" /\ ~BeginOk
  /\ req' = "denied"
  /\ UNCHANGED <<idVars, authVars, requester, agent, repo, checkedIdentity,
                  checkedGeneration, checkedPermission, snapPresent, snapNames,
                  snapRole, roleChanged, snapRule,
                  ruleChanged, ruleLocked, ghostVars>>

\* manages_now, first source: get_account(..., for_share=True) and the stored
\* channel-admin rows (read_stored_admin). A role that grants ends the check
\* (stored.is_admin returns True); otherwise the policy rule is read next. A
\* locked read of a row written after the snapshot aborts (RoleDeny).
RoleAborts ==
  CommitRecheck /\ RoleRead = "locked" /\ snapRole /\ roleChanged

ReadRole ==
  /\ req = "begun" /\ ~RoleAborts
  /\ IF ~CommitRecheck \/ snapRole THEN req' = "authRead" ELSE req' = "roleRead"
  /\ staleAuthEver' = (staleAuthEver \/
       (CommitRecheck /\ snapRole /\ ~Authority(requester, agent)))
  /\ UNCHANGED <<idVars, authVars, requester, agent, repo, checkedIdentity,
                  checkedGeneration, checkedPermission, snapPresent, snapNames,
                  snapRole, roleChanged, snapRule, ruleChanged, ruleLocked,
                  grant, unsafeEver, stalePermissionEver, removedEver,
                  restoredEver>>

RoleDeny ==
  /\ req = "begun" /\ RoleAborts
  /\ req' = "denied"
  /\ UNCHANGED <<idVars, authVars, requester, agent, repo, checkedIdentity,
                  checkedGeneration, checkedPermission, snapPresent, snapNames,
                  snapRole, roleChanged, snapRule, ruleChanged, ruleLocked,
                  ghostVars>>

\* manages_now, second source: _caller_holds loads the access policy and looks
\* for the agent's ownership rule. Locked: a rule written after the snapshot
\* aborts. A missing rule denies.
RuleAborts == RuleRead = "locked" /\ snapRule /\ ruleChanged
RuleGrants == snapRule /\ ~RuleAborts

ReadRule ==
  /\ req = "roleRead" /\ RuleGrants
  /\ req' = "authRead"
  /\ ruleLocked' = (RuleRead # "snapshot")
  /\ staleAuthEver' = (staleAuthEver \/ ~Authority(requester, agent))
  /\ UNCHANGED <<idVars, authVars, requester, agent, repo, checkedIdentity,
                  checkedGeneration, checkedPermission, snapPresent, snapNames,
                  snapRole, roleChanged, snapRule, ruleChanged, grant,
                  unsafeEver, stalePermissionEver, removedEver, restoredEver>>

RuleDeny ==
  /\ req = "roleRead" /\ ~RuleGrants
  /\ req' = "denied"
  /\ UNCHANGED <<idVars, authVars, requester, agent, repo, checkedIdentity,
                  checkedGeneration, checkedPermission, snapPresent, snapNames,
                  snapRole, roleChanged, snapRule, ruleChanged, ruleLocked,
                  ghostVars>>

\* The authorization upsert, upsert_github_app (which rebuilds
\* repo_full_names from the active authorization rows) and the commit.
Commit ==
  /\ req = "authRead"
  /\ req' = "done" /\ grant' = TRUE
  /\ auth' = auth \cup {repo}
  /\ covered' = auth \cup {repo}
  /\ instPresent' = TRUE
  /\ unsafeEver' = (unsafeEver \/
       ~(Authority(requester, agent)
         /\ link[requester] = checkedIdentity
         /\ generation[requester] = checkedGeneration
         /\ (instPresent => repo \in covered)))
  /\ stalePermissionEver' = (stalePermissionEver \/
       (<<requester, repo>> \notin permitted))
  /\ restoredEver' = (restoredEver \/ (instPresent /\ repo \notin covered))
  /\ UNCHANGED <<idVars, roles, rules, permitted, requester, agent, repo,
                  checkedIdentity, checkedGeneration, checkedPermission,
                  snapPresent, snapNames, snapRole, roleChanged, snapRule,
                  ruleChanged, ruleLocked, removedEver,
                  staleAuthEver>>

-------------------------------------------------------------------------------
\* The world changes. Link, user and installation rows are locked from Begin
\* on, so their writers wait there; the role row is locked only after ReadRole
\* and only when the role read takes the lock.

Lookup == req \in {"discovered", "looked", "checked"}

\* A role write (demotion; the Account row or a channel-admin row). The role
\* rows are locked from ReadRole on when the role read takes the lock, so the
\* writer waits there; before it, a commit after the snapshot is what the
\* locked read detects.
RevokeRole(u, a) ==
  /\ req \in {"checked", "begun", "roleRead", "authRead"}
  /\ (req \in {"roleRead", "authRead"} => RoleRead = "snapshot")
  /\ u = requester /\ a = agent /\ <<u, a>> \in roles
  /\ roles' = roles \ {<<u, a>>}
  /\ roleChanged' = (roleChanged \/ req = "begun")
  /\ UNCHANGED <<idVars, rules, covered, instPresent, auth, permitted, req,
                  requester, agent, repo, checkedIdentity, checkedGeneration,
                  checkedPermission, snapPresent, snapNames, snapRole,
                  snapRule, ruleChanged, ruleLocked, ghostVars>>

\* An operator removes the agent's ownership rule from the access policy. The
\* policy row is locked only once ReadRule has read it with a lock; a role that
\* granted ends the check before the rule is read, so the rule stays unlocked.
RevokeRule(u, a) ==
  /\ req \in {"checked", "begun", "roleRead", "authRead"}
  /\ (req = "authRead" => ~ruleLocked)
  /\ u = requester /\ a = agent /\ <<u, a>> \in rules
  /\ rules' = rules \ {<<u, a>>}
  /\ ruleChanged' = (ruleChanged \/ req \in {"begun", "roleRead"})
  /\ UNCHANGED <<idVars, roles, covered, instPresent, auth, permitted, req,
                  requester, agent, repo, checkedIdentity, checkedGeneration,
                  checkedPermission, snapPresent, snapNames, snapRole,
                  snapRule, roleChanged, ruleLocked, ghostVars>>

Unlink(u) ==
  /\ Lookup /\ u = requester /\ link[u] # None
  /\ link' = [link EXCEPT ![u] = None]
  /\ generation' = [generation EXCEPT ![u] = @ + 1]
  /\ UNCHANGED <<phase, owner, opener, githubIdentity, proven, saves,
                  replayed, foreignEver, authVars, reqVars, ghostVars>>

Rotate(u) ==
  /\ Lookup /\ u = requester /\ link[u] # None
  /\ generation[u] < 2
  /\ generation' = [generation EXCEPT ![u] = @ + 1]
  /\ UNCHANGED <<phase, owner, opener, githubIdentity, proven, link, saves,
                  replayed, foreignEver, authVars, reqVars, ghostVars>>

\* Reconciliation drops the repo from repo_full_names (a repository removal,
\* suspension or uninstall all look like this). The authorization rows stay.
RemoveCoverage(r) ==
  /\ Lookup /\ r = repo /\ r \in covered
  /\ covered' = covered \ {r}
  /\ removedEver' = TRUE
  /\ UNCHANGED <<idVars, roles, rules, instPresent, auth, permitted, reqVars,
                  grant, unsafeEver, stalePermissionEver, restoredEver,
                  staleAuthEver>>

\* Reconciliation creates an installation row this agent's lookup had not seen.
ReconcileInsert ==
  /\ Lookup /\ ~instPresent
  /\ instPresent' = TRUE /\ covered' = Repos
  /\ UNCHANGED <<idVars, roles, rules, auth, permitted, reqVars, ghostVars>>

RevokePermission(u, r) ==
  /\ req \in {"looked", "checked", "begun", "roleRead", "authRead"}
  /\ u = requester /\ r = repo /\ <<u, r>> \in permitted
  /\ permitted' = permitted \ {<<u, r>>}
  /\ UNCHANGED <<idVars, roles, rules, covered, instPresent, auth, reqVars,
                  ghostVars>>

Quiescent ==
  /\ req \in {"done", "denied"} /\ phase \in {"used", "rejected"}
  /\ UNCHANGED vars

Next ==
  \/ \E u \in Users: Mint(u) \/ Open(u) \/ Unlink(u) \/ Rotate(u)
  \/ PlatformCallback \/ RejectForeign
  \/ \E g \in GitHubUsers: GitHubCallback(g)
  \/ ReplayCallback
  \/ \E u \in Users, a \in Agents, r \in Repos: Request(u, a, r)
  \/ Discover \/ CheckPermission \/ LateSnapshot \/ LateReject
  \/ Begin \/ BeginReject \/ ReadRole \/ RoleDeny \/ ReadRule \/ RuleDeny
  \/ Commit
  \/ \E u \in Users, a \in Agents: RevokeRole(u, a) \/ RevokeRule(u, a)
  \/ \E r \in Repos: RemoveCoverage(r)
  \/ ReconcileInsert
  \/ \E u \in Users, r \in Repos: RevokePermission(u, r)
  \/ Quiescent

Spec == Init /\ [][Next]_vars

NoForeignLink == ~foreignEver
\* The grant commits only while the requester still has authority (a role or a
\* policy rule), the link is
\* the one the lookup used, and the repo is still covered. Stricter than
\* serializability: a demotion that committed before the grant counts, even
\* though "grant, then demotion" is a valid serial order.
GrantRestsOnCurrentAuthority == ~unsafeEver
SingleUseLink == saves <= 1
NoStalePermission == ~stalePermissionEver
\* Coverage removed before the commit never ends in a grant, and the grant
\* never puts a removed repo back into repo_full_names.
NoGrantAfterCoverageRemoval == ~(grant /\ removedEver)
CoverageNotRestoredByGrant == ~restoredEver
\* When the authority check passes, the requester has authority now: no role
\* or rule revocation committed after the snapshot and before the read that
\* decided is missed.
NoStaleAuthority == ~staleAuthEver

\* Witnesses, violated on purpose: the locked reads can abort, for each source.
\* A denial after the role was written means the role read aborted (a locked
\* role that changed always has snapRole). A denial after the rule was written
\* while the role did not grant means the rule read aborted.
RoleAbortNeverHappens == ~(req = "denied" /\ roleChanged)
RuleAbortNeverHappens == ~(req = "denied" /\ ruleChanged /\ ~snapRole)

ProgressStep ==
  Discover \/ CheckPermission \/ LateSnapshot \/ LateReject \/ Begin
  \/ BeginReject \/ ReadRole \/ RoleDeny \/ ReadRule \/ RuleDeny \/ Commit
EveryRequestResolves ==
  (req \notin {"idle", "done", "denied"}) ~> (req \in {"done", "denied"})
LiveSpec == Spec /\ WF_vars(ProgressStep)
================================================================================
