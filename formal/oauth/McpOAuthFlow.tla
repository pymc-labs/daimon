---------------------------- MODULE McpOAuthFlow ----------------------------
(***************************************************************************)
(* One mcp_oauth_flows row, from the click that mints it to the callback   *)
(* that spends it. Two opens of the same start link race discovery,        *)
(* client registration and save_flow_client; each browser that reaches the *)
(* provider may send its callback, and a callback may be delivered twice.  *)
(* The consuming callback exchanges the code with the client stored on the *)
(* row and, on success, stores one grant in the requester's vault and      *)
(* stamps completed_at.                                                    *)
(*                                                                         *)
(* Source: packages/core/daimon/core/stores/mcp_oauth_flows.py             *)
(* (save_flow_client, consume_flow, mark_flow_completed),                  *)
(* packages/core/daimon/core/mcp_oauth/handshake.py (prepare_authorization)*)
(* packages/core/daimon/core/mcp_oauth/complete.py,                        *)
(* packages/adapters/mcp/daimon/adapters/mcp/oauth_mcp.py.                 *)
(***************************************************************************)
EXTENDS Naturals, TLC

CONSTANTS
    ClientCAS,             \* 7a5a74b: client written only while client_id IS NULL; a loser re-reads and reuses
    ConnectedByCompletion, \* 41234e9: "connected" is completed_at, not used_at
    ConsumeCAS,            \* consume_flow matches only used_at IS NULL (mutation check)
    ReplayCallbacks        \* a callback URL may be delivered a second time

None == "none"
Opens == {"o1", "o2"}
ClientOf == [o \in Opens |-> IF o = "o1" THEN "c1" ELSE "c2"]
Clients == {"c1", "c2"}
Deliveries == Opens \X {1, 2}
CbStates == {"none", "approve", "decline", "consumedApprove", "consumedDecline",
             "rejected", "done", "refused", "mismatch", "declined"}

VARIABLES
    rowClient,  \* client_id on the flow row
    used,       \* used_at IS NOT NULL
    completed,  \* completed_at IS NOT NULL
    openPc,     \* per open: idle | read | redirected | expired
    openSnap,   \* client_id the open read from the row
    issued,     \* per open: the client the browser authorised as (the code's audience)
    cb,         \* per delivery: callback progress
    cbClient,   \* per delivery: client_id RETURNING'd by the consume
    grants,     \* grants stored in the requester's vault
    consumes    \* successful consume_flow calls

vars == <<rowClient, used, completed, openPc, openSnap, issued, cb, cbClient, grants, consumes>>

Init ==
    /\ rowClient = None
    /\ used = FALSE
    /\ completed = FALSE
    /\ openPc = [o \in Opens |-> "idle"]
    /\ openSnap = [o \in Opens |-> None]
    /\ issued = [o \in Opens |-> None]
    /\ cb = [d \in Deliveries |-> "none"]
    /\ cbClient = [d \in Deliveries |-> None]
    /\ grants = 0
    /\ consumes = 0

\* start_handler: get_flow; a spent row shows the expired page.
OpenRead(o) ==
    /\ openPc[o] = "idle"
    /\ IF used
          THEN /\ openPc' = [openPc EXCEPT ![o] = "expired"]
               /\ UNCHANGED openSnap
          ELSE /\ openPc' = [openPc EXCEPT ![o] = "read"]
               /\ openSnap' = [openSnap EXCEPT ![o] = rowClient]
    /\ UNCHANGED <<rowClient, used, completed, issued, cb, cbClient, grants, consumes>>

\* prepare_authorization: reuse a registered client, or register one and save it.
OpenPrepare(o) ==
    /\ openPc[o] = "read"
    /\ \/ /\ ClientCAS /\ openSnap[o] # None
          /\ issued' = [issued EXCEPT ![o] = openSnap[o]]
          /\ openPc' = [openPc EXCEPT ![o] = "redirected"]
          /\ UNCHANGED rowClient
       \/ /\ ~(ClientCAS /\ openSnap[o] # None)
          /\ IF used
                THEN /\ openPc' = [openPc EXCEPT ![o] = "expired"]
                     /\ UNCHANGED <<rowClient, issued>>
                ELSE IF ~ClientCAS \/ rowClient = None
                   THEN \* save_flow_client took the write
                        /\ rowClient' = ClientOf[o]
                        /\ issued' = [issued EXCEPT ![o] = ClientOf[o]]
                        /\ openPc' = [openPc EXCEPT ![o] = "redirected"]
                   ELSE \* lost the CAS: re-read and reuse what landed first
                        /\ issued' = [issued EXCEPT ![o] = rowClient]
                        /\ openPc' = [openPc EXCEPT ![o] = "redirected"]
                        /\ UNCHANGED rowClient
    /\ UNCHANGED <<used, completed, openSnap, cb, cbClient, grants, consumes>>

\* The provider redirects back; the person approved or declined.
Arrive(d) ==
    LET o == d[1] k == d[2] IN
    /\ issued[o] # None
    /\ cb[d] = "none"
    /\ k = 2 => (ReplayCallbacks /\ cb[<<o, 1>>] # "none")
    /\ \E choice \in {"approve", "decline"} : cb' = [cb EXCEPT ![d] = choice]
    /\ UNCHANGED <<rowClient, used, completed, openPc, openSnap, issued, cbClient, grants, consumes>>

\* callback_handler: consume_flow before anything else (the replay gate).
Consume(d) ==
    /\ cb[d] \in {"approve", "decline"}
    /\ IF ConsumeCAS => ~used
          THEN /\ used' = TRUE
               /\ consumes' = consumes + 1
               /\ cbClient' = [cbClient EXCEPT ![d] = rowClient]
               /\ cb' = [cb EXCEPT ![d] = IF cb[d] = "approve" THEN "consumedApprove" ELSE "consumedDecline"]
          ELSE /\ cb' = [cb EXCEPT ![d] = "rejected"]
               /\ UNCHANGED <<used, consumes, cbClient>>
    /\ UNCHANGED <<rowClient, completed, openPc, openSnap, issued, grants>>

Decline(d) ==
    /\ cb[d] = "consumedDecline"
    /\ cb' = [cb EXCEPT ![d] = "declined"]
    /\ UNCHANGED <<rowClient, used, completed, openPc, openSnap, issued, cbClient, grants, consumes>>

\* complete_mcp_oauth_flow: exchange with the row's client, store, stamp completed_at.
Exchange(d) ==
    LET o == d[1] IN
    /\ cb[d] = "consumedApprove"
    /\ \/ /\ cbClient[d] # issued[o]
          \* the code was issued to a different client: the provider refuses it
          /\ cb' = [cb EXCEPT ![d] = "mismatch"]
          /\ UNCHANGED <<grants, completed>>
       \/ /\ cbClient[d] = issued[o]
          /\ cb' = [cb EXCEPT ![d] = "done"]
          /\ grants' = grants + 1
          /\ completed' = TRUE
       \/ /\ cb' = [cb EXCEPT ![d] = "refused"]   \* provider or vault failure
          /\ UNCHANGED <<grants, completed>>
    /\ UNCHANGED <<rowClient, used, openPc, openSnap, issued, cbClient, consumes>>

Done ==
    /\ \A o \in Opens : openPc[o] \in {"redirected", "expired"}
    /\ \A d \in Deliveries : cb[d] \notin {"approve", "decline", "consumedApprove", "consumedDecline"}
    /\ UNCHANGED vars

Next ==
    \/ \E o \in Opens : OpenRead(o) \/ OpenPrepare(o)
    \/ \E d \in Deliveries : Arrive(d) \/ Consume(d) \/ Decline(d) \/ Exchange(d)
    \/ Done

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ rowClient \in Clients \cup {None}
    /\ used \in BOOLEAN /\ completed \in BOOLEAN
    /\ openPc \in [Opens -> {"idle", "read", "redirected", "expired"}]
    /\ openSnap \in [Opens -> Clients \cup {None}]
    /\ issued \in [Opens -> Clients \cup {None}]
    /\ cb \in [Deliveries -> CbStates]
    /\ cbClient \in [Deliveries -> Clients \cup {None}]
    /\ grants \in 0..4 /\ consumes \in 0..4

Connected == IF ConnectedByCompletion THEN completed ELSE used

\* A flow is spent at most once.
AtMostOneConsume == consumes <= 1
\* At most one grant results from one flow.
AtMostOneGrant == grants <= 1
\* The code is always exchanged as the client it was issued to.
ExchangeMatchesIssuer == \A d \in Deliveries : cb[d] # "mismatch"
\* Nobody is treated as connected without a stored grant.
ConnectedImpliesGrant == Connected => grants >= 1
=============================================================================
