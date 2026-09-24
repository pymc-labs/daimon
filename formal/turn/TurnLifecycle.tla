---------------- MODULE TurnLifecycle ----------------
EXTENDS Naturals, FiniteSets, Sequences, TLC

\* Finite abstraction of packages/core/daimon/core/turn/{reducers,render,driver}.py.
\* Event payloads are collapsed to event kind + IDs; text is a count of
\* appended chunks. Delivery order is unconstrained unless a cfg constraint
\* is selected (none is selected by default).

CONSTANTS AllowStaleReplay, AllowConflictingTerminalEvents

EventIds == {"m1", "m2", "tu", "r", "idle", "term", "err"}
EventKinds == {"message", "toolUse", "toolResult", "idle", "terminated", "error"}
NoEvent == <<>>
Statuses == {"pending", "complete", "failed"}
Anchors == 0..7

VARIABLES seen, messages, tool, stopReason, turnError, revision,
          anchor, delivered, renderFailure, renderCancelled, finished

vars == <<seen, messages, tool, stopReason, turnError, revision,
         anchor, delivered, renderFailure, renderCancelled, finished>>

Init ==
  /\ seen = {}
  /\ messages = 0
  /\ tool = NoEvent
  /\ stopReason = FALSE
  /\ turnError = FALSE
  /\ revision = 0
  /\ anchor = 0
  /\ delivered = 0
  /\ renderFailure = FALSE
  /\ renderCancelled = FALSE
  /\ finished = FALSE

FoldMessage(id) ==
  /\ id \in {"m1", "m2"}
  /\ id \notin seen
  /\ seen' = seen \cup {id}
  /\ messages' = messages + 1
  /\ revision' = revision + 1
  /\ UNCHANGED <<tool, stopReason, turnError, anchor, delivered,
                 renderFailure, renderCancelled, finished>>

FoldToolUse ==
  /\ "tu" \notin seen
  /\ seen' = seen \cup {"tu"}
  /\ tool' = <<"tu", "pending">>
  /\ revision' = revision + 1
  /\ UNCHANGED <<messages, stopReason, turnError, anchor, delivered,
                 renderFailure, renderCancelled, finished>>

FoldToolResult ==
  /\ "r" \notin seen
  /\ seen' = seen \cup {"r"}
  /\ tool' = IF tool # NoEvent THEN <<"tu", "complete">> ELSE NoEvent
  /\ revision' = IF tool # NoEvent THEN revision + 1 ELSE revision
  /\ UNCHANGED <<messages, stopReason, turnError, anchor, delivered,
                 renderFailure, renderCancelled, finished>>

FoldIdle ==
  /\ "idle" \notin seen
  /\ AllowConflictingTerminalEvents \/ ~turnError
  /\ seen' = seen \cup {"idle"}
  /\ stopReason' = TRUE
  /\ revision' = revision + 1
  /\ UNCHANGED <<messages, tool, turnError, anchor, delivered,
                 renderFailure, renderCancelled, finished>>

FoldTerminated ==
  /\ "term" \notin seen
  /\ AllowConflictingTerminalEvents \/ ~stopReason
  /\ seen' = seen \cup {"term"}
  /\ turnError' = TRUE
  /\ revision' = revision + 1
  /\ finished' = TRUE
  /\ UNCHANGED <<messages, tool, stopReason, anchor, delivered,
                 renderFailure, renderCancelled>>

FoldError ==
  /\ "err" \notin seen
  /\ AllowConflictingTerminalEvents \/ ~stopReason
  /\ seen' = seen \cup {"err"}
  /\ turnError' = TRUE
  /\ revision' = revision + 1
  /\ finished' = TRUE
  /\ UNCHANGED <<messages, tool, stopReason, anchor, delivered,
                 renderFailure, renderCancelled>>

Duplicate(id) ==
  /\ id \in seen
  /\ UNCHANGED vars

RenderTick ==
  /\ ~renderCancelled
  /\ anchor < revision
  /\ delivered' = delivered + 1
  /\ anchor' = revision
  /\ renderFailure' = FALSE
  /\ UNCHANGED <<seen, messages, tool, stopReason, turnError, revision,
                 renderCancelled, finished>>

RenderFail ==
  /\ ~renderCancelled
  /\ anchor < revision
  /\ renderFailure' = TRUE
  /\ UNCHANGED <<seen, messages, tool, stopReason, turnError, revision, anchor,
                 delivered, renderCancelled, finished>>

CancelRender ==
  /\ ~renderCancelled
  /\ renderCancelled' = TRUE
  /\ UNCHANGED <<seen, messages, tool, stopReason, turnError, revision, anchor,
                 delivered, renderFailure, finished>>

\* Reconnect replay reconstructs TurnState from a fresh fold and replaces
\* state_cell (driver.py around replay_events). A truncated/stale replay is
\* an environment possibility under this abstraction; source correctness
\* relies on MA replay returning the full current-turn history.
ReplayStalePrefix ==
  /\ AllowStaleReplay
  /\ revision > 0
  /\ messages' = 0
  /\ tool' = NoEvent
  /\ stopReason' = FALSE
  /\ turnError' = FALSE
  /\ seen' = {}
  /\ revision' = 0
  /\ UNCHANGED <<anchor, delivered, renderFailure, renderCancelled, finished>>

Finalize ==
  /\ finished
  /\ renderCancelled
  /\ finished' = FALSE
  /\ UNCHANGED <<seen, messages, tool, stopReason, turnError, revision, anchor,
                 delivered, renderFailure, renderCancelled>>

Next ==
  \/ \E id \in {"m1", "m2"}: FoldMessage(id)
  \/ FoldToolUse
  \/ FoldToolResult
  \/ FoldIdle
  \/ FoldTerminated
  \/ FoldError
  \/ \E id \in EventIds: Duplicate(id)
  \/ RenderTick
  \/ RenderFail
  \/ CancelRender
  \/ ReplayStalePrefix
  \/ Finalize

TypeOK ==
  /\ seen \subseteq EventIds
  /\ messages \in 0..2
  /\ revision \in 0..7
  /\ tool \in {NoEvent, <<"tu", "pending">>, <<"tu", "complete">>}
  /\ stopReason \in BOOLEAN
  /\ turnError \in BOOLEAN
  /\ anchor \in Anchors
  /\ delivered \in Nat
  /\ renderFailure \in BOOLEAN
  /\ renderCancelled \in BOOLEAN
  /\ finished \in BOOLEAN

AppendOnly == anchor <= revision
TerminalExclusive == ~(stopReason /\ turnError)
RenderAnchorBound == anchor <= revision

Spec == Init /\ [][Next]_vars

==============================================================
