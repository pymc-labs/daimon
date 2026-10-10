--------------------- MODULE CardTerminalReplacement ---------------------
EXTENDS FiniteSets

CONSTANT Mode
VARIABLES old, deleted, bounded, newer, newDone, cards, messageRef, cardRef
vars == <<old, deleted, bounded, newer, newDone, cards, messageRef, cardRef>>

\* Card 0 is the original. Card 2 is the ceiling render's replacement;
\* card 1 is the retained request's late replacement.
Init == /\ old = "editing"
        /\ deleted = FALSE
        /\ bounded = FALSE
        /\ newer = FALSE
        /\ newDone = FALSE
        /\ cards = {0}
        /\ messageRef = 0
        /\ cardRef = 0

DeleteOriginal == /\ ~deleted
                  /\ deleted' = TRUE
                  /\ cards' = {}
                  /\ UNCHANGED <<old, bounded, newer, newDone, messageRef, cardRef>>

BoundOld == /\ ~bounded /\ old # "done"
            /\ bounded' = TRUE
            /\ UNCHANGED <<old, deleted, newer, newDone, cards, messageRef, cardRef>>

OldEditNotFound == /\ deleted /\ old = "editing"
                   /\ old' = "ready"
                   /\ UNCHANGED <<deleted, bounded, newer, newDone, cards,
                                   messageRef, cardRef>>

IssueNew == /\ deleted /\ bounded /\ ~newer /\ old # "done"
            /\ newer' = TRUE
            /\ UNCHANGED <<old, deleted, bounded, newDone, cards, messageRef, cardRef>>

FinishNew == /\ newer /\ ~newDone
             /\ newDone' = TRUE
             /\ cards' = cards \cup {2}
             /\ messageRef' = 2
             /\ cardRef' = 2
             /\ UNCHANGED <<old, deleted, bounded, newer>>

\* The fixed code checks terminal token ownership before this send.
StartOldSend == /\ old = "ready"
                /\ (Mode = "broken" \/ ~newer)
                /\ old' = "sending"
                /\ UNCHANGED <<deleted, bounded, newer, newDone, cards,
                                messageRef, cardRef>>

DropOldSend == /\ old = "ready" /\ newer /\ Mode = "safe"
               /\ old' = "done"
               /\ UNCHANGED <<deleted, bounded, newer, newDone, cards,
                               messageRef, cardRef>>

\* A send already on the wire creates card 1 before reconciliation can delete it.
OldSendReturns == /\ old = "sending"
                  /\ old' = IF newer /\ Mode = "safe" THEN "reconciling" ELSE "done"
                  /\ cards' = cards \cup {1}
                  /\ messageRef' = IF Mode = "broken" THEN 1 ELSE messageRef
                  /\ cardRef' = IF Mode = "broken" /\ ~newDone THEN 1 ELSE cardRef
                  /\ UNCHANGED <<deleted, bounded, newer, newDone>>

ReconcileOldSend == /\ old = "reconciling"
                    /\ old' = "done"
                    /\ cards' = cards \ {1}
                    /\ UNCHANGED <<deleted, bounded, newer, newDone,
                                    messageRef, cardRef>>

Next == DeleteOriginal \/ BoundOld \/ OldEditNotFound \/ IssueNew \/ FinishNew
        \/ StartOldSend \/ DropOldSend \/ OldSendReturns \/ ReconcileOldSend

NoStaleReplacement == (newDone /\ old = "done") =>
    (Cardinality(cards) = 1 /\ cards = {2} /\ messageRef = 2 /\ cardRef = 2)
=============================================================================
