-------------------- MODULE CancelRegistration --------------------
EXTENDS Naturals, TLC

(***************************************************************************)
(* Model of Slack's first status-card post and Cancel action.              *)
(* Sources: slack/lifecycle.py post_initial/_maybe_flush, app.py           *)
(* _handle_block_action, and blockkit.py to_blocks.                       *)
(***************************************************************************)

CONSTANT SafeOrdering

VARIABLES visible, responseReturned, registered, clicked, cancelled
vars == <<visible, responseReturned, registered, clicked, cancelled>>

Init ==
    /\ visible = FALSE
    /\ responseReturned = FALSE
    /\ registered = FALSE
    /\ clicked = FALSE
    /\ cancelled = FALSE

Register ==
    /\ ~registered
    /\ (SafeOrdering \/ responseReturned)
    /\ registered' = TRUE
    /\ UNCHANGED <<visible, responseReturned, clicked, cancelled>>

PublishCard ==
    /\ ~visible
    /\ (~SafeOrdering \/ registered)
    /\ visible' = TRUE
    /\ UNCHANGED <<responseReturned, registered, clicked, cancelled>>

ReturnPostResponse ==
    /\ visible
    /\ ~responseReturned
    /\ responseReturned' = TRUE
    /\ UNCHANGED <<visible, registered, clicked, cancelled>>

AuthorClicksVisibleCard ==
    /\ visible
    /\ ~clicked
    /\ clicked' = TRUE
    /\ cancelled' = registered
    /\ UNCHANGED <<visible, responseReturned, registered>>

Next == Register \/ PublishCard \/ ReturnPostResponse \/ AuthorClicksVisibleCard
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ visible \in BOOLEAN
    /\ responseReturned \in BOOLEAN
    /\ registered \in BOOLEAN
    /\ clicked \in BOOLEAN
    /\ cancelled \in BOOLEAN

VisibleAuthorClickCancels == clicked => cancelled

=============================================================================
