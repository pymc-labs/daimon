---------------- MODULE TurnProgress ----------------
EXTENDS Naturals, TLC

CONSTANT DataBudget
ASSUME DataBudget \in Nat

VARIABLES remainingData, terminalDelivered, cancelAvailable, renderCancelled,
          renderDirty, renderSucceeded, lastRenderFailed, finalized,
          finalRenderAttempted

pvars == <<remainingData, terminalDelivered, cancelAvailable, renderCancelled,
          renderDirty, renderSucceeded, lastRenderFailed, finalized,
          finalRenderAttempted>>

Init ==
  /\ remainingData = DataBudget
  /\ terminalDelivered = FALSE
  /\ cancelAvailable = TRUE
  /\ renderCancelled = FALSE
  /\ renderDirty = FALSE
  /\ renderSucceeded = FALSE
  /\ lastRenderFailed = FALSE
  /\ finalized = FALSE
  /\ finalRenderAttempted = FALSE

DeliverData ==
  /\ remainingData > 0
  /\ ~terminalDelivered
  /\ remainingData' = remainingData - 1
  /\ renderDirty' = TRUE
  /\ UNCHANGED <<terminalDelivered, cancelAvailable, renderCancelled,
                 renderSucceeded, lastRenderFailed, finalized,
                 finalRenderAttempted>>

DeliverTerminal ==
  /\ remainingData = 0
  /\ ~terminalDelivered
  /\ terminalDelivered' = TRUE
  /\ renderDirty' = TRUE
  /\ UNCHANGED <<remainingData, cancelAvailable, renderCancelled,
                 renderSucceeded, lastRenderFailed, finalized,
                 finalRenderAttempted>>

RequestCancel ==
  /\ cancelAvailable
  /\ cancelAvailable' = FALSE
  /\ renderCancelled' = TRUE
  /\ UNCHANGED <<remainingData, terminalDelivered, renderDirty,
                 renderSucceeded, lastRenderFailed, finalized,
                 finalRenderAttempted>>

CloseCancelWindow ==
  /\ cancelAvailable
  /\ cancelAvailable' = FALSE
  /\ UNCHANGED <<remainingData, terminalDelivered, renderCancelled,
                 renderDirty, renderSucceeded, lastRenderFailed, finalized,
                 finalRenderAttempted>>

CancelOrClose == RequestCancel \/ CloseCancelWindow

RenderSuccess ==
  /\ renderDirty
  /\ ~renderCancelled
  /\ renderDirty' = FALSE
  /\ renderSucceeded' = TRUE
  /\ lastRenderFailed' = FALSE
  /\ UNCHANGED <<remainingData, terminalDelivered, cancelAvailable,
                 renderCancelled, finalized, finalRenderAttempted>>

RenderFailure ==
  /\ renderDirty
  /\ ~renderCancelled
  /\ lastRenderFailed' = TRUE
  /\ UNCHANGED <<remainingData, terminalDelivered, cancelAvailable,
                 renderCancelled, renderDirty, renderSucceeded, finalized,
                 finalRenderAttempted>>

Finalize ==
  /\ terminalDelivered
  /\ ~finalized
  /\ finalized' = TRUE
  /\ finalRenderAttempted' = TRUE
  /\ UNCHANGED <<remainingData, terminalDelivered, cancelAvailable,
                 renderCancelled, renderDirty, renderSucceeded,
                 lastRenderFailed>>

Next ==
  \/ DeliverData
  \/ DeliverTerminal
  \/ CancelOrClose
  \/ RenderSuccess
  \/ RenderFailure
  \/ Finalize

TypeOK ==
  /\ remainingData \in 0..DataBudget
  /\ terminalDelivered \in BOOLEAN
  /\ cancelAvailable \in BOOLEAN
  /\ renderCancelled \in BOOLEAN
  /\ renderDirty \in BOOLEAN
  /\ renderSucceeded \in BOOLEAN
  /\ lastRenderFailed \in BOOLEAN
  /\ finalized \in BOOLEAN
  /\ finalRenderAttempted \in BOOLEAN

FinalizationConsistent == finalized => (terminalDelivered /\ finalRenderAttempted)
TurnProgress == <>finalized
RenderResolved == <> (renderSucceeded \/ renderCancelled)

Spec ==
  /\ Init
  /\ [][Next]_pvars
  /\ WF_pvars(DeliverData)
  /\ WF_pvars(DeliverTerminal)
  /\ WF_pvars(CancelOrClose)
  /\ WF_pvars(RenderSuccess)
  /\ WF_pvars(Finalize)

==============================================================
