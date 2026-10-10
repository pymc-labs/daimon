------------------------- MODULE SchemaCleanup -------------------------
EXTENDS TLC

CONSTANT AwaitRollback
VARIABLES face, cleanup
vars == <<face, cleanup>>

Init == /\ face = "queued"
        /\ cleanup = "idle"

StartFace ==
    /\ face = "queued"
    /\ face' = "transaction"
    /\ UNCHANGED cleanup

BeginCleanup ==
    /\ cleanup = "idle"
    /\ cleanup' = "requested"
    /\ UNCHANGED face

CancelFace ==
    /\ cleanup = "requested"
    /\ face = "transaction"
    /\ face' = "cancelling"
    /\ UNCHANGED cleanup

FaceRollback ==
    /\ face = "cancelling"
    /\ face' = "done"
    /\ UNCHANGED cleanup

FaceCompletes ==
    /\ face = "transaction"
    /\ face' = "done"
    /\ UNCHANGED cleanup

BeginDrop ==
    /\ IF AwaitRollback
          THEN cleanup = "requested" /\ face = "done"
          ELSE cleanup = "idle" /\ face = "transaction"
    /\ cleanup' = "dropping"
    /\ UNCHANGED face

FinishDrop ==
    /\ cleanup = "dropping"
    /\ face = "done"
    /\ cleanup' = "done"
    /\ UNCHANGED face

Next == StartFace \/ BeginCleanup \/ CancelFace \/ FaceRollback
        \/ FaceCompletes \/ BeginDrop \/ FinishDrop
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ face \in {"queued", "transaction", "cancelling", "done"}
    /\ cleanup \in {"idle", "requested", "dropping", "done"}

NoDropDuringFaceTransaction == cleanup = "dropping" => face = "done"

=======================================================================
