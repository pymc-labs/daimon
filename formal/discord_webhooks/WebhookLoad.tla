---------------------- MODULE WebhookLoad ----------------------
EXTENDS Integers, FiniteSets, TLC

\* One turn is one answer post. A window is two seconds. Discord.py's
\* retry_after handling is represented by retryAt, not by an HTTP loop.
CONSTANTS Turns, Channels, Place, HookOf, IdentityOn, CanManage,
          MaxHooks, ChannelCap, HookLimit, BotLimit, OpsPerTurn, DelayBound,
          DropOn429, IgnoreRetryAfter, DropFallback, ReplayAnswer
ASSUME /\ Turns # {} /\ Channels # {}
       /\ Place \in [Turns -> Channels]
       /\ HookOf \in [Turns -> 1..MaxHooks]
       /\ MaxHooks <= ChannelCap
       /\ HookLimit > 0 /\ BotLimit > 0 /\ OpsPerTurn > 0
       /\ DelayBound > 0

SingleChannel == [t \in Turns |-> CHOOSE c \in Channels: TRUE]
SingleHook == [t \in Turns |-> 1]
TwoTurns == 1..2
ThreeTurns == 1..3
IdentityHook == [t \in Turns |-> t]
TwoChannel ==
    LET firstTurn == CHOOSE t \in Turns: TRUE
        firstChannel == CHOOSE c \in Channels: TRUE
        secondChannel == CHOOSE c \in Channels \ {firstChannel}: TRUE
    IN [t \in Turns |-> IF t = firstTurn THEN firstChannel ELSE secondChannel]

VARIABLES window, pool, hookUsed, botUsed, route, status,
          retryAt, postedAt, attempts, rejections, deliveries, opsDone,
          forcedBot
vars == <<window, pool, hookUsed, botUsed, route, status,
          retryAt, postedAt, attempts, rejections, deliveries, opsDone,
          forcedBot>>

Init ==
    /\ window = 0
    /\ pool = [c \in Channels |-> 0]
    /\ hookUsed = [c \in Channels |-> [h \in 1..MaxHooks |-> 0]]
    /\ botUsed = 0
    /\ route = [t \in Turns |-> "none"]
    /\ status = [t \in Turns |-> "queued"]
    /\ retryAt = [t \in Turns |-> 0]
    /\ postedAt = [t \in Turns |-> -1]
    /\ attempts = [t \in Turns |-> 0]
    /\ rejections = [t \in Turns |-> 0]
    /\ deliveries = [t \in Turns |-> 0]
    /\ opsDone = [t \in Turns |-> 0]
    /\ forcedBot = [t \in Turns |-> FALSE]

\* The adapter's asyncio lock makes each channel lookup/create atomic within
\* one process. This does not represent two processes sharing the same guild.
CreateHook(c) ==
    /\ c \in Channels
    /\ IdentityOn /\ CanManage
    /\ pool[c] < MaxHooks /\ pool[c] < ChannelCap
    /\ \E t \in Turns: status[t] = "queued" /\ Place[t] = c
                        /\ HookOf[t] > pool[c]
    /\ pool' = [pool EXCEPT ![c] = @ + 1]
    /\ UNCHANGED <<window, hookUsed, botUsed, route, status,
                   retryAt, postedAt, attempts, rejections, deliveries, opsDone,
                   forcedBot>>

Choose(t) ==
    /\ t \in Turns /\ status[t] = "queued" /\ route[t] = "none"
    /\ IF IdentityOn /\ CanManage
          THEN pool[Place[t]] >= HookOf[t]
          ELSE TRUE
    /\ route' = [route EXCEPT ![t] =
                    IF IdentityOn /\ CanManage THEN "hook"
                    ELSE IF DropFallback THEN "none" ELSE "bot"]
    /\ forcedBot' = [forcedBot EXCEPT ![t] = ~(IdentityOn /\ CanManage)]
    /\ UNCHANGED <<window, pool, hookUsed, botUsed, status,
                   retryAt, postedAt, attempts, rejections, deliveries, opsDone>>

PostHook(t) ==
    /\ t \in Turns /\ status[t] = "queued" /\ route[t] = "hook"
    /\ window >= retryAt[t]
    /\ hookUsed[Place[t]][HookOf[t]] < HookLimit
    /\ hookUsed' = [hookUsed EXCEPT ![Place[t]][HookOf[t]] = @ + 1]
    /\ opsDone' = [opsDone EXCEPT ![t] = @ + 1]
    /\ status' = [status EXCEPT ![t] =
                     IF opsDone[t] + 1 = OpsPerTurn THEN "posted" ELSE @]
    /\ postedAt' = [postedAt EXCEPT ![t] =
                       IF opsDone[t] + 1 = OpsPerTurn THEN window ELSE @]
    /\ attempts' = [attempts EXCEPT ![t] = @ + 1]
    /\ deliveries' = [deliveries EXCEPT ![t] =
                         IF opsDone[t] + 1 = OpsPerTurn THEN @ + 1 ELSE @]
    /\ UNCHANGED <<window, pool, botUsed, route, retryAt, rejections,
                   forcedBot>>

Hit429(t) ==
    /\ t \in Turns /\ status[t] = "queued" /\ route[t] = "hook"
    /\ window >= retryAt[t]
    /\ hookUsed[Place[t]][HookOf[t]] = HookLimit
    /\ retryAt' = [retryAt EXCEPT ![t] = IF IgnoreRetryAfter THEN window ELSE window + 1]
    /\ attempts' = [attempts EXCEPT ![t] = @ + 1]
    /\ rejections' = [rejections EXCEPT ![t] = @ + 1]
    /\ status' = IF DropOn429 THEN [status EXCEPT ![t] = "lost"] ELSE status
    /\ UNCHANGED <<window, pool, hookUsed, botUsed, route, postedAt, deliveries,
                   opsDone, forcedBot>>

\* 10015 Unknown Webhook: the adapter evicts the cached hook and sends by bot.
UnknownWebhook(t) ==
    /\ t \in Turns /\ status[t] = "queued" /\ route[t] = "hook"
    /\ route' = [route EXCEPT ![t] =
                    IF DropFallback THEN "none" ELSE "bot"]
    /\ forcedBot' = [forcedBot EXCEPT ![t] = TRUE]
    /\ UNCHANGED <<window, pool, hookUsed, botUsed, status,
                   retryAt, postedAt, attempts, rejections, deliveries, opsDone>>

PostBot(t) ==
    /\ t \in Turns /\ status[t] = "queued" /\ route[t] = "bot"
    /\ botUsed < BotLimit
    /\ botUsed' = botUsed + 1
    /\ opsDone' = [opsDone EXCEPT ![t] = @ + 1]
    /\ status' = [status EXCEPT ![t] =
                     IF opsDone[t] + 1 = OpsPerTurn THEN "posted" ELSE @]
    /\ postedAt' = [postedAt EXCEPT ![t] =
                       IF opsDone[t] + 1 = OpsPerTurn THEN window ELSE @]
    /\ attempts' = [attempts EXCEPT ![t] = @ + 1]
    /\ deliveries' = [deliveries EXCEPT ![t] =
                         IF opsDone[t] + 1 = OpsPerTurn THEN @ + 1 ELSE @]
    /\ UNCHANGED <<window, pool, hookUsed, route, retryAt, rejections,
                   forcedBot>>

\* Mutation: an accepted final answer is sent again after its response was
\* already recorded. Production never retries a posted turn this way.
ReplayPostedAnswer(t) ==
    /\ ReplayAnswer /\ t \in Turns /\ status[t] = "posted"
    /\ deliveries[t] = 1
    /\ deliveries' = [deliveries EXCEPT ![t] = 2]
    /\ UNCHANGED <<window, pool, hookUsed, botUsed, route, status,
                   retryAt, postedAt, attempts, rejections, opsDone,
                   forcedBot>>

Eligible(t) ==
    status[t] = "queued" /\
    CASE route[t] = "none" -> FALSE
      [] route[t] = "bot" -> botUsed < BotLimit
      [] route[t] = "hook" ->
           window >= retryAt[t] /\ hookUsed[Place[t]][HookOf[t]] < HookLimit
      [] OTHER -> FALSE

\* Work-conserving scheduling: advance only when every turn has a route and
\* no eligible post remains. A 429 can set the next-window retry deadline.
NextWindow ==
    /\ \E t \in Turns: status[t] = "queued"
    /\ \A t \in Turns: status[t] # "queued" \/ route[t] # "none"
    /\ \A t \in Turns: ~Eligible(t)
    /\ window' = window + 1
    /\ hookUsed' = [c \in Channels |-> [h \in 1..MaxHooks |-> 0]]
    /\ botUsed' = 0
    /\ rejections' = [t \in Turns |-> 0]
    /\ UNCHANGED <<pool, route, status, retryAt, postedAt, attempts, deliveries,
                   opsDone, forcedBot>>

Next ==
    \/ \E c \in Channels: CreateHook(c)
    \/ \E t \in Turns: Choose(t) \/ PostHook(t) \/ Hit429(t)
                       \/ UnknownWebhook(t) \/ PostBot(t)
                       \/ ReplayPostedAnswer(t)
    \/ NextWindow
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ window \in Nat
    /\ pool \in [Channels -> 0..MaxHooks]
    /\ hookUsed \in [Channels -> [1..MaxHooks -> 0..HookLimit]]
    /\ botUsed \in 0..BotLimit
    /\ route \in [Turns -> {"none", "hook", "bot"}]
    /\ status \in [Turns -> {"queued", "posted", "lost"}]
    /\ retryAt \in [Turns -> Nat]
    /\ postedAt \in [Turns -> {-1} \cup Nat]
    /\ attempts \in [Turns -> Nat]
    /\ rejections \in [Turns -> Nat]
    /\ deliveries \in [Turns -> 0..2]
    /\ opsDone \in [Turns -> 0..OpsPerTurn]
    /\ forcedBot \in [Turns -> BOOLEAN]
NoAnswerLost == \A t \in Turns: status[t] # "lost"
NoDuplicateAnswer == \A t \in Turns: deliveries[t] <= 1
NoBusyRetry == \A t \in Turns: rejections[t] <= 1
FallbackRoute == \A t \in Turns: forcedBot[t] => route[t] = "bot"
PoolWithinChannelCap == \A c \in Channels: pool[c] <= ChannelCap
BoundedPosting == \A t \in Turns: status[t] = "posted" => postedAt[t] < DelayBound

\* Creation uses a separate channel bucket. A 429 occupies it and gives a
\* 33-window retry (66 seconds). These operators use the existing state and
\* leave the posting-bucket checks above unchanged for the older configs.
CreateHit429(c) ==
    /\ c \in Channels /\ window = 0 /\ pool[c] = 0
    /\ hookUsed[c][1] = 0
    /\ hookUsed' = [hookUsed EXCEPT ![c][1] = 1]
    /\ retryAt' = [t \in Turns |-> IF Place[t] = c THEN 33 ELSE retryAt[t]]
    /\ rejections' = [t \in Turns |-> IF Place[t] = c THEN 1 ELSE rejections[t]]
    /\ UNCHANGED <<window, pool, botUsed, route, status, postedAt,
                   attempts, deliveries, opsDone, forcedBot>>

CreateAfterRetry(c) ==
    /\ c \in Channels /\ pool[c] = 0
    /\ \E t \in Turns: Place[t] = c /\ retryAt[t] > 0
                         /\ window >= retryAt[t]
    /\ hookUsed[c][1] = 0
    /\ pool' = [pool EXCEPT ![c] = 1]
    /\ hookUsed' = [hookUsed EXCEPT ![c][1] = 1]
    /\ UNCHANGED <<window, botUsed, route, status, retryAt, postedAt,
                   attempts, rejections, deliveries, opsDone, forcedBot>>

ChooseAfterCreate(t, block) ==
    /\ t \in Turns /\ status[t] = "queued" /\ route[t] = "none"
    /\ retryAt[t] > 0
    /\ IF block THEN pool[Place[t]] > 0 ELSE TRUE
    /\ route' = [route EXCEPT ![t] = IF block THEN "hook" ELSE "bot"]
    /\ forcedBot' = [forcedBot EXCEPT ![t] = ~block]
    /\ UNCHANGED <<window, pool, hookUsed, botUsed, status,
                   retryAt, postedAt, attempts, rejections, deliveries, opsDone>>

CreationNextWindow(block) ==
    /\ \E t \in Turns: status[t] = "queued"
    /\ \A t \in Turns: retryAt[t] > 0
    /\ \A t \in Turns: window < retryAt[t] \/ pool[Place[t]] > 0
    /\ IF block THEN TRUE ELSE \A t \in Turns: route[t] # "none"
    /\ IF block THEN \A t \in Turns: pool[Place[t]] = 0 \/ route[t] # "none" ELSE TRUE
    /\ \A t \in Turns: ~Eligible(t)
    /\ window' = window + 1
    /\ hookUsed' = [c \in Channels |-> [h \in 1..MaxHooks |-> 0]]
    /\ botUsed' = 0
    /\ UNCHANGED <<pool, route, status, retryAt, postedAt, attempts,
                   rejections, deliveries, opsDone, forcedBot>>

CreationNext(block) ==
    \/ \E c \in Channels: CreateHit429(c) \/ CreateAfterRetry(c)
    \/ \E t \in Turns: ChooseAfterCreate(t, block) \/ PostBot(t) \/ PostHook(t)
    \/ CreationNextWindow(block)
CreationSafeSpec == Init /\ [][CreationNext(FALSE)]_vars
CreationUnsafeSpec == Init /\ [][CreationNext(TRUE)]_vars
=================================================================
