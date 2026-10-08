------------------------------ MODULE Approvals ------------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS K, Batched, PreFix, ReAsk, DropConfirm, QuietStatusEarly, EarlyCardRender, QuietRetryBound, QueueSettlesWithinBound, RetireUnsentApproved
ASSUME K \in 1..3 /\ QuietRetryBound \in 0..2
Ids == 1..K
Cards == IF Batched THEN {0} ELSE Ids
Calls(c) == IF Batched THEN Ids ELSE {c}
VARIABLES phase, answer, card, clicks, refusals, sent, accepted, pending,
          queue, runs, alive, outcome, idle, duplicated, reasked, quiet, ungated, lateRun, quietRetries
vars == <<phase, answer, card, clicks, refusals, sent, accepted, pending,
          queue, runs, alive, outcome, idle, duplicated, reasked, quiet, ungated, lateRun, quietRetries>>
Init ==
  /\ phase = "start" /\ answer = [c \in Cards |-> "none"]
  /\ card = [c \in Cards |-> "pending"]
  /\ clicks = [c \in Cards |-> 0] /\ refusals = 0
  /\ sent = [i \in Ids |-> "none"] /\ accepted = {} /\ pending = Ids
  /\ queue = {} /\ runs = [i \in Ids |-> 0]
  /\ alive = TRUE /\ outcome = "none" /\ idle = "none"
  /\ duplicated = FALSE /\ reasked = FALSE /\ quiet = FALSE /\ ungated = FALSE
  /\ lateRun = FALSE /\ quietRetries = 0
EmitPause ==
  /\ phase = "start" /\ phase' = "pause" /\ idle' = "requires"
  /\ UNCHANGED <<answer, card, clicks, refusals, sent, accepted, pending,
                 queue, runs, alive, outcome, duplicated, reasked, quiet, ungated, lateRun, quietRetries>>
OpenCards ==
  /\ phase = "pause" /\ phase' = "cards" /\ idle' = "none"
  /\ UNCHANGED <<answer, card, clicks, refusals, sent, accepted, pending,
                 queue, runs, alive, outcome, duplicated, reasked, quiet, ungated, lateRun, quietRetries>>
Click(c,a) ==
  /\ phase = "cards" /\ c \in Cards /\ a \in {"approved","denied"}
  /\ answer[c] = "none" /\ answer' = [answer EXCEPT ![c] = a]
  /\ clicks' = [clicks EXCEPT ![c] = @ + 1]
  /\ UNCHANGED <<phase, card, refusals, sent, accepted, pending, queue,
                 runs, alive, outcome, idle, duplicated, reasked, quiet, ungated, lateRun, quietRetries>>
Expire(c) ==
  /\ phase = "cards" /\ c \in Cards /\ answer[c] = "none"
  /\ answer' = [answer EXCEPT ![c] = "expired"]
  /\ UNCHANGED <<phase, card, clicks, refusals, sent, accepted, pending,
                 queue, runs, alive, outcome, idle, duplicated, reasked, quiet, ungated, lateRun, quietRetries>>
RefusedClick(c) ==
  /\ c \in Cards /\ refusals < 2 /\ phase \in {"cards","sent","ended"}
  /\ refusals' = refusals + 1
  /\ UNCHANGED <<phase, answer, card, clicks, sent, accepted, pending,
                 queue, runs, alive, outcome, idle, duplicated, reasked, quiet, ungated, lateRun, quietRetries>>
Cancel ==
  /\ phase = "cards"
  /\ answer' = [c \in Cards |-> IF answer[c] = "none" THEN "stopped" ELSE answer[c]]
  /\ card' = [c \in Cards |->
                IF answer[c] = "none" \/ (RetireUnsentApproved /\ answer[c] = "approved")
                THEN "stopped"
                ELSE IF card[c] = "approved" THEN "approved" ELSE "denied"]
  /\ sent' = [i \in Ids |-> "deny"]
  /\ phase' = "ended" /\ alive' = FALSE /\ outcome' = "stopped"
  /\ UNCHANGED <<clicks, refusals, accepted, pending, queue, runs, idle,
                 duplicated, reasked, quiet, ungated, lateRun, quietRetries>>
RenderAnswer(c) ==
  /\ EarlyCardRender /\ phase = "cards" /\ c \in Cards
  /\ answer[c] # "none" /\ card[c] = "pending"
  /\ card' = [card EXCEPT ![c] = answer[c]]
  /\ UNCHANGED <<phase, answer, clicks, refusals, sent, accepted, pending,
                 queue, runs, alive, outcome, idle, duplicated, reasked,
                 quiet, ungated, lateRun, quietRetries>>
SendBatch ==
  /\ phase = "cards" /\ \A c \in Cards : answer[c] # "none"
  /\ card' = [c \in Cards |-> answer[c]]
  /\ sent' = [i \in Ids |-> IF answer[IF Batched THEN 0 ELSE i] = "approved"
                         THEN "allow" ELSE "deny"]
  /\ queue' = Ids /\ phase' = "sent"
  /\ UNCHANGED <<answer, clicks, refusals, accepted, pending, runs,
                 alive, outcome, idle, duplicated, reasked, quiet, ungated, lateRun, quietRetries>>
UngatedTool ==
  /\ phase = "sent" /\ ~ungated /\ pending # {} /\ queue # {}
  /\ ungated' = TRUE /\ idle' = "requires"
  /\ UNCHANGED <<phase, answer, card, clicks, refusals, sent, accepted,
                 pending, queue, runs, alive, outcome, duplicated, reasked, quiet, lateRun, quietRetries>>
DuplicatePause ==
  /\ phase = "sent" /\ ~duplicated /\ pending # {} /\ idle = "none"
  /\ duplicated' = TRUE /\ idle' = "requires"
  /\ UNCHANGED <<phase, answer, card, clicks, refusals, sent, accepted,
                 pending, queue, runs, alive, outcome, reasked, quiet, ungated, lateRun, quietRetries>>
ReadIdle ==
  /\ phase = "sent" /\ idle = "requires" /\ ~QuietStatusEarly
  /\ IF PreFix \/ pending \subseteq accepted
        THEN /\ phase' = "ended" /\ alive' = FALSE
             /\ outcome' = "not_accepted" /\ idle' = "none"
        ELSE /\ phase' = phase /\ alive' = alive
             /\ outcome' = outcome /\ idle' = "none"
  /\ UNCHANGED <<answer, card, clicks, refusals, sent, accepted, pending,
                 queue, runs, duplicated, reasked, quiet, ungated, lateRun, quietRetries>>
Take(i) ==
  /\ i \in queue /\ ~quiet /\ phase \in {"sent","ended"}
  /\ accepted' = accepted \cup {i} /\ queue' = queue \ {i}
  /\ pending' = pending \ {i}
  /\ runs' = IF sent[i] = "allow" THEN [runs EXCEPT ![i] = @ + 1] ELSE runs
  /\ lateRun' = (lateRun \/ (sent[i] = "allow" /\ ~alive))
  /\ idle' = IF pending' = {} THEN "end_turn" ELSE "requires"
  /\ UNCHANGED <<phase, answer, card, clicks, refusals, sent, alive,
                 outcome, duplicated, reasked, quiet, ungated, quietRetries>>
GenuineReask(i) ==
  /\ ReAsk /\ phase = "sent" /\ ~reasked /\ i \in accepted
  /\ queue = {} /\ pending = {} /\ idle = "end_turn"
  /\ reasked' = TRUE /\ pending' = {i} /\ idle' = "requires"
  /\ UNCHANGED <<phase, answer, card, clicks, refusals, sent, accepted,
                 queue, runs, alive, outcome, duplicated, quiet, ungated, lateRun, quietRetries>>
DropQueued ==
  /\ (DropConfirm \/ (QueueSettlesWithinBound /\ QuietStatusEarly
                      /\ quietRetries = QuietRetryBound))
  /\ phase = "sent" /\ queue # {} /\ ~quiet
  /\ queue' = {} /\ quiet' = TRUE /\ idle' = "none"
  /\ UNCHANGED <<phase, answer, card, clicks, refusals, sent, accepted,
                 pending, runs, alive, outcome, duplicated, reasked, ungated, lateRun, quietRetries>>
RetryQuiet ==
  /\ QuietStatusEarly /\ phase = "sent" /\ queue # {} /\ idle = "requires"
  /\ quietRetries < QuietRetryBound
  /\ quietRetries' = quietRetries + 1
  /\ UNCHANGED <<answer, card, clicks, refusals, sent, accepted, pending,
                 queue, runs, phase, alive, outcome, idle, duplicated, reasked,
                 quiet, ungated, lateRun>>
EarlyQuietExit ==
  /\ QuietStatusEarly /\ ~QueueSettlesWithinBound
  /\ quietRetries = QuietRetryBound
  /\ phase = "sent" /\ queue # {} /\ idle = "requires"
  /\ phase' = "ended" /\ alive' = FALSE /\ outcome' = "not_accepted"
  /\ UNCHANGED <<answer, card, clicks, refusals, sent, accepted, pending,
                 queue, runs, idle, duplicated, reasked, quiet, ungated, lateRun, quietRetries>>
StatusTimeout ==
  /\ phase = "sent" /\ quiet
  /\ phase' = "ended" /\ alive' = FALSE /\ outcome' = "couldnt_confirm"
  /\ UNCHANGED <<answer, card, clicks, refusals, sent, accepted, pending,
                 queue, runs, idle, duplicated, reasked, quiet, ungated, lateRun, quietRetries>>
Finish ==
  /\ phase = "sent" /\ idle = "end_turn" /\ ~ReAsk
  /\ phase' = "ended" /\ alive' = FALSE /\ outcome' = "success"
  /\ UNCHANGED <<answer, card, clicks, refusals, sent, accepted, pending,
                 queue, runs, idle, duplicated, reasked, quiet, ungated, lateRun, quietRetries>>
Next ==
  \/ EmitPause \/ OpenCards \/ Cancel \/ SendBatch \/ UngatedTool
  \/ DuplicatePause \/ ReadIdle \/ DropQueued \/ RetryQuiet \/ EarlyQuietExit \/ StatusTimeout \/ Finish
  \/ \E c \in Cards : (Expire(c) \/ RenderAnswer(c) \/ RefusedClick(c)
                       \/ \E a \in {"approved","denied"} : Click(c,a))
  \/ \E i \in Ids : (Take(i) \/ GenuineReask(i))
Spec == Init /\ [][Next]_vars
FairSpec == Spec /\ WF_vars(Next)
AtMostOnce == \A i \in Ids : runs[i] <= 1
NoFalseNotAccepted == outcome = "not_accepted" => reasked
ApprovedRunsInTurn == ~lateRun
NoAllowAfterStop == \A i \in Ids :
  card[IF Batched THEN 0 ELSE i] \in {"denied","expired","stopped"} => sent[i] # "allow"
CardTruth == \A c \in Cards :
  (card[c] = "approved" => \A i \in Calls(c) : sent[i] = "allow")
  /\ (card[c] \in {"denied","expired","stopped"} =>
        \A i \in Calls(c) : sent[i] = "deny")
EventualCardTruth == <>[] (phase = "ended" => CardTruth)
OneAnswerPerCard == \A c \in Cards : clicks[c] <= 1
Terminates == <> (phase = "ended")
=============================================================================
