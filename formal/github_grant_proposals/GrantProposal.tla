---------------------------- MODULE GrantProposal ----------------------------
EXTENDS Integers

CONSTANT UnsafeModelConfirm
Actors == {"requester", "other"}
TTL == 2
VARIABLES pending, owner, proposalTurn, proposalTime, approvedTurn,
          turn, actor, time, canGrant, granted, grantTurn, grantActor,
          grantTime, grantPermission
vars == <<pending, owner, proposalTurn, proposalTime, approvedTurn,
          turn, actor, time, canGrant, granted, grantTurn, grantActor,
          grantTime, grantPermission>>

Init == /\ pending = FALSE /\ owner = "requester" /\ proposalTurn = 0
        /\ proposalTime = 0 /\ approvedTurn = -1 /\ turn = 0
        /\ actor = "requester" /\ time = 0 /\ canGrant = TRUE
        /\ granted = FALSE /\ grantTurn = -1 /\ grantActor = "other"
        /\ grantTime = -1 /\ grantPermission = FALSE

Propose == /\ ~pending /\ ~granted
           /\ pending' = TRUE /\ owner' = actor
           /\ proposalTurn' = turn /\ proposalTime' = time
           /\ approvedTurn' = -1
           /\ UNCHANGED <<turn, actor, time, canGrant, granted,
                           grantTurn, grantActor, grantTime, grantPermission>>

NextHuman(a) == /\ a \in Actors /\ turn < 2
                /\ turn' = turn + 1 /\ actor' = a /\ time' = time + 1
                /\ UNCHANGED <<pending, owner, proposalTurn, proposalTime,
                                approvedTurn, canGrant, granted, grantTurn,
                                grantActor, grantTime, grantPermission>>

HumanYes == /\ pending /\ actor = owner /\ turn > proposalTurn
            /\ time <= proposalTime + TTL /\ approvedTurn = -1
            /\ approvedTurn' = turn
            /\ UNCHANGED <<pending, owner, proposalTurn, proposalTime,
                            turn, actor, time, canGrant, granted, grantTurn,
                            grantActor, grantTime, grantPermission>>

Revoke == /\ canGrant /\ canGrant' = FALSE
          /\ UNCHANGED <<pending, owner, proposalTurn, proposalTime,
                          approvedTurn, turn, actor, time, granted, grantTurn,
                          grantActor, grantTime, grantPermission>>

Confirm == /\ pending /\ ~granted
           /\ IF UnsafeModelConfirm
                 THEN TRUE
                 ELSE /\ actor = owner /\ turn > proposalTurn
                      /\ approvedTurn = turn
                      /\ time <= proposalTime + TTL /\ canGrant
           /\ pending' = FALSE /\ granted' = TRUE
           /\ grantTurn' = turn /\ grantActor' = actor
           /\ grantTime' = time /\ grantPermission' = canGrant
           /\ UNCHANGED <<owner, proposalTurn, proposalTime,
                           approvedTurn, turn, actor, time, canGrant>>

Next == Propose \/ (\E a \in Actors: NextHuman(a)) \/ HumanYes \/ Revoke \/ Confirm
Spec == Init /\ [][Next]_vars
ValidGrant == granted =>
    /\ grantActor = owner /\ grantTurn > proposalTurn
    /\ approvedTurn = grantTurn /\ grantTime <= proposalTime + TTL
    /\ grantPermission
=============================================================================
