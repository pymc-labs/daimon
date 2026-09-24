---------------------- MODULE OutOfOrder ----------------------
EXTENDS Naturals, TLC

CONSTANTS UseIntentLock, CreditAmount, ClawbackTarget
ASSUME CreditAmount \in Nat /\ CreditAmount > 0
ASSUME ClawbackTarget \in Nat /\ ClawbackTarget <= CreditAmount

Actors == {"checkout", "clawback"}
Stages == {"idle", "observed", "done"}
None == "none"

VARIABLES creditExists, pending, applied, checkoutStage, clawbackStage,
          checkoutSawPending, clawbackSawCredit, lockOwner
vars == <<creditExists, pending, applied, checkoutStage, clawbackStage,
          checkoutSawPending, clawbackSawCredit, lockOwner>>

Init == /\ creditExists = FALSE
        /\ pending = FALSE
        /\ applied = 0
        /\ checkoutStage = "idle"
        /\ clawbackStage = "idle"
        /\ checkoutSawPending = FALSE
        /\ clawbackSawCredit = FALSE
        /\ lockOwner = None

CanStart(actor) == IF UseIntentLock THEN lockOwner = None ELSE TRUE
AcquireCheckout == /\ checkoutStage = "idle"
                   /\ CanStart("checkout")
                   /\ lockOwner' = IF UseIntentLock THEN "checkout" ELSE lockOwner
                   /\ UNCHANGED <<creditExists, pending, applied, checkoutStage,
                                   clawbackStage, checkoutSawPending,
                                   clawbackSawCredit>>
ObservePending == /\ checkoutStage = "idle"
                  /\ (IF UseIntentLock THEN lockOwner = "checkout" ELSE TRUE)
                  /\ checkoutSawPending' = pending
                  /\ checkoutStage' = "observed"
                  /\ UNCHANGED <<creditExists, pending, applied, clawbackStage,
                                  clawbackSawCredit, lockOwner>>
CommitCheckout == /\ checkoutStage = "observed"
                  /\ creditExists' = TRUE
                  /\ pending' = IF checkoutSawPending THEN FALSE ELSE pending
                  /\ applied' = IF checkoutSawPending THEN ClawbackTarget ELSE applied
                  /\ checkoutStage' = "done"
                  /\ lockOwner' = IF UseIntentLock THEN None ELSE lockOwner
                  /\ UNCHANGED <<clawbackStage, checkoutSawPending,
                                  clawbackSawCredit>>

AcquireClawback == /\ clawbackStage = "idle"
                   /\ CanStart("clawback")
                   /\ lockOwner' = IF UseIntentLock THEN "clawback" ELSE lockOwner
                   /\ UNCHANGED <<creditExists, pending, applied, checkoutStage,
                                   clawbackStage, checkoutSawPending,
                                   clawbackSawCredit>>
ObserveCredit == /\ clawbackStage = "idle"
                 /\ (IF UseIntentLock THEN lockOwner = "clawback" ELSE TRUE)
                 /\ clawbackSawCredit' = creditExists
                 /\ clawbackStage' = "observed"
                 /\ UNCHANGED <<creditExists, pending, applied, checkoutStage,
                                 checkoutSawPending, lockOwner>>
CommitClawback == /\ clawbackStage = "observed"
                  /\ pending' = IF clawbackSawCredit THEN pending ELSE TRUE
                  /\ applied' = IF clawbackSawCredit THEN ClawbackTarget ELSE applied
                  /\ clawbackStage' = "done"
                  /\ lockOwner' = IF UseIntentLock THEN None ELSE lockOwner
                  /\ UNCHANGED <<creditExists, checkoutStage, checkoutSawPending,
                                  clawbackSawCredit>>

Next == AcquireCheckout \/ ObservePending \/ CommitCheckout
        \/ AcquireClawback \/ ObserveCredit \/ CommitClawback
Spec == Init /\ [][Next]_vars
FairSpec == Spec
            /\ WF_vars(AcquireCheckout)
            /\ WF_vars(ObservePending)
            /\ WF_vars(CommitCheckout)
            /\ WF_vars(AcquireClawback)
            /\ WF_vars(ObserveCredit)
            /\ WF_vars(CommitClawback)

TypeOK == /\ creditExists \in BOOLEAN
          /\ pending \in BOOLEAN
          /\ applied \in 0..CreditAmount
          /\ checkoutStage \in Stages
          /\ clawbackStage \in Stages
          /\ checkoutSawPending \in BOOLEAN
          /\ clawbackSawCredit \in BOOLEAN
          /\ lockOwner \in Actors \cup {None}
NoOrphanPending == ~(creditExists /\ pending)
NoOverClawback == applied <= CreditAmount
BothComplete == checkoutStage = "done" /\ clawbackStage = "done"
Progress == <>BothComplete

=============================================================================
