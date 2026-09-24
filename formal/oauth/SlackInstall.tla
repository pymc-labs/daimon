----------------------------- MODULE SlackInstall -----------------------------
(***************************************************************************)
(* One Slack workspace installed, uninstalled and reinstalled. Uninstalling *)
(* makes Slack send app_uninstalled and tokens_revoked; each becomes a      *)
(* teardown (archive the tenant, delete the bot token). The reinstall's     *)
(* OAuth callback provisions the tenant and upserts the fresh token. A      *)
(* teardown may run after the reinstall started when its delivery was       *)
(* delayed or retried (LateDelivery).                                       *)
(*                                                                         *)
(* Source: packages/adapters/mcp/daimon/adapters/mcp/oauth_slack.py         *)
(* (callback_handler), packages/core/daimon/core/defaults/provisioning.py   *)
(* (provision_tenant ON CONFLICT DO NOTHING, teardown_slack_install),       *)
(* packages/adapters/slack/daimon/adapters/slack/app.py (_handle_teardown). *)
(***************************************************************************)
EXTENDS Naturals, TLC

CONSTANTS
    ClearArchive,       \* the reinstall clears archived_at (Discord's rejoin does)
    GuardedTeardown,    \* teardown deletes and archives in one txn, only for a token
                        \* installed before the event
    UpsertBeforeClear,  \* the reinstall stores its token before clearing the archive
    LateDelivery        \* a teardown may run after the reinstall began

Deliveries == {"appUninstalled", "tokensRevoked"}

VARIABLES
    phase,   \* Slack side: installed | uninstalled | reinstalled
    tenant,  \* live | archived
    token,   \* none | t1 (original install) | t2 (reinstall)
    rpc,     \* reinstall callback progress
    tpc      \* per teardown delivery: none | pending | archived | done

vars == <<phase, tenant, token, rpc, tpc>>

Init ==
    /\ phase = "installed"
    /\ tenant = "live"
    /\ token = "t1"
    /\ rpc = "idle"
    /\ tpc = [d \in Deliveries |-> "none"]

Uninstall ==
    /\ phase = "installed"
    /\ phase' = "uninstalled"
    /\ tpc' = [d \in Deliveries |-> "pending"]
    /\ UNCHANGED <<tenant, token, rpc>>

\* Current teardown_slack_install: archive_tenant, then a separate txn deleting the token.
TeardownArchive(d) ==
    /\ ~GuardedTeardown
    /\ tpc[d] = "pending"
    /\ tenant' = "archived"
    /\ tpc' = [tpc EXCEPT ![d] = "archived"]
    /\ UNCHANGED <<phase, token, rpc>>

TeardownDelete(d) ==
    /\ ~GuardedTeardown
    /\ tpc[d] = "archived"
    /\ token' = "none"
    /\ tpc' = [tpc EXCEPT ![d] = "done"]
    /\ UNCHANGED <<phase, tenant, rpc>>

\* Proposed: one txn; a token stored after the event (t2) means the event is stale.
TeardownGuarded(d) ==
    /\ GuardedTeardown
    /\ tpc[d] = "pending"
    /\ IF token = "t2"
          THEN UNCHANGED <<tenant, token>>
          ELSE /\ tenant' = "archived"
               /\ token' = "none"
    /\ tpc' = [tpc EXCEPT ![d] = "done"]
    /\ UNCHANGED <<phase, rpc>>

TeardownsDone == \A d \in Deliveries : tpc[d] \in {"none", "done"}

\* callback_handler. provision_tenant is ON CONFLICT DO NOTHING on an existing
\* tenant, so only an explicit clear touches archived_at.
ReinstallStep ==
    /\ phase = "uninstalled"
    /\ rpc = "idle" => (LateDelivery \/ TeardownsDone)
    /\ CASE rpc = "idle" ->
              /\ rpc' = "provisioned"
              /\ tenant' = IF ClearArchive /\ ~UpsertBeforeClear THEN "live" ELSE tenant
              /\ UNCHANGED <<token, phase>>
         [] rpc = "provisioned" ->
              /\ token' = "t2"
              /\ IF UpsertBeforeClear
                    THEN /\ rpc' = "upserted" /\ UNCHANGED phase
                    ELSE /\ rpc' = "done" /\ phase' = "reinstalled"
              /\ UNCHANGED tenant
         [] rpc = "upserted" ->
              /\ tenant' = IF ClearArchive THEN "live" ELSE tenant
              /\ rpc' = "done"
              /\ phase' = "reinstalled"
              /\ UNCHANGED token
    /\ UNCHANGED tpc

Quiescent ==
    /\ rpc = "done" /\ TeardownsDone
    /\ UNCHANGED vars

Next ==
    \/ Uninstall
    \/ \E d \in Deliveries : TeardownArchive(d) \/ TeardownDelete(d) \/ TeardownGuarded(d)
    \/ ReinstallStep
    \/ Quiescent

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ phase \in {"installed", "uninstalled", "reinstalled"}
    /\ tenant \in {"live", "archived"}
    /\ token \in {"none", "t1", "t2"}
    /\ rpc \in {"idle", "provisioned", "upserted", "done"}
    /\ tpc \in [Deliveries -> {"none", "pending", "archived", "done"}]

\* Once the reinstall and every teardown have finished, the workspace is a live
\* tenant holding the reinstall's token.
ReinstallLeavesLiveTenant ==
    (rpc = "done" /\ TeardownsDone) => (tenant = "live" /\ token = "t2")
=============================================================================
