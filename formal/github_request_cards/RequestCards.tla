--------------------------- MODULE RequestCards ---------------------------
EXTENDS Naturals, TLC

CONSTANTS UseLock, InitialMentions

Actors == 1..4
Requests == {1, 2}
Req(a) == IF a <= 2 THEN 1 ELSE 2

VARIABLES phase, lock, card, posts, mentions, wantsNew, wantsMention
vars == <<phase, lock, card, posts, mentions, wantsNew, wantsMention>>

Init ==
  /\ phase = [a \in Actors |-> "idle"]
  /\ lock = 0
  /\ card = [r \in Requests |-> FALSE]
  /\ posts = [r \in Requests |-> 0]
  /\ mentions = InitialMentions
  /\ wantsNew = [a \in Actors |-> FALSE]
  /\ wantsMention = [a \in Actors |-> FALSE]

Start(a) ==
  /\ phase[a] = "idle"
  /\ phase' = [phase EXCEPT ![a] = "waiting"]
  /\ UNCHANGED <<lock, card, posts, mentions, wantsNew, wantsMention>>

Acquire(a) ==
  /\ phase[a] = "waiting"
  /\ (IF UseLock THEN lock = 0 ELSE TRUE)
  /\ lock' = IF UseLock THEN a ELSE lock
  /\ phase' = [phase EXCEPT ![a] = "held"]
  /\ UNCHANGED <<card, posts, mentions, wantsNew, wantsMention>>

Check(a) ==
  /\ phase[a] = "held"
  /\ wantsNew' = [wantsNew EXCEPT ![a] = ~card[Req(a)]]
  /\ wantsMention' = [wantsMention EXCEPT ![a] = ~card[Req(a)] /\ mentions < 3]
  /\ phase' = [phase EXCEPT ![a] = "checked"]
  /\ UNCHANGED <<lock, card, posts, mentions>>

Post(a) ==
  /\ phase[a] = "checked"
  /\ posts' = IF wantsNew[a] THEN [posts EXCEPT ![Req(a)] = @ + 1] ELSE posts
  /\ mentions' = IF wantsMention[a] THEN mentions + 1 ELSE mentions
  /\ phase' = [phase EXCEPT ![a] = "posted"]
  /\ UNCHANGED <<lock, card, wantsNew, wantsMention>>

Commit(a) ==
  /\ phase[a] = "posted"
  /\ card' = IF wantsNew[a] THEN [card EXCEPT ![Req(a)] = TRUE] ELSE card
  /\ lock' = IF UseLock THEN 0 ELSE lock
  /\ phase' = [phase EXCEPT ![a] = "done"]
  /\ UNCHANGED <<posts, mentions, wantsNew, wantsMention>>

Next == \E a \in Actors : Start(a) \/ Acquire(a) \/ Check(a) \/ Post(a) \/ Commit(a)
Spec == Init /\ [][Next]_vars

NoDuplicate == \A r \in Requests : posts[r] <= 1
MentionBound == mentions <= 3
=============================================================================
