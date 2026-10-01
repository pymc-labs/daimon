------------------- MODULE InitialCardReconcile -------------------
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS OldIntent, NewIntent, OldCard, OldDuplicate, NewCard
Intents == {OldIntent, NewIntent}
Cards == {OldCard, OldDuplicate, NewCard}
Owner(c) == CASE c = OldCard -> OldIntent
              [] c = OldDuplicate -> OldIntent
              [] c = NewCard -> NewIntent

VARIABLES process, status, generation, card, recorded, snapshot,
          found, misses, editedTerminal
vars == <<process, status, generation, card, recorded, snapshot,
          found, misses, editedTerminal>>

Init ==
    /\ process = "old"
    /\ status = [i \in Intents |-> "absent"]
    /\ generation = [i \in Intents |-> "none"]
    /\ card = [c \in Cards |-> "absent"]
    /\ recorded = [i \in Intents |-> FALSE]
    /\ snapshot = {}
    /\ found = [i \in Intents |-> FALSE]
    /\ misses = [i \in Intents |-> 0]
    /\ editedTerminal = FALSE

PrepareOld ==
    /\ process = "old"
    /\ status[OldIntent] = "absent"
    /\ status' = [status EXCEPT ![OldIntent] = "prepared"]
    /\ generation' = [generation EXCEPT ![OldIntent] = "old"]
    /\ UNCHANGED <<process, card, recorded, snapshot, found, misses, editedTerminal>>

PostOld(c) ==
    /\ process = "old"
    /\ c \in {OldCard, OldDuplicate}
    /\ status[OldIntent] = "prepared"
    /\ card[c] = "absent"
    /\ c = OldCard \/ card[OldCard] = "live"
    /\ card' = [card EXCEPT ![c] = "live"]
    /\ UNCHANGED <<process, status, generation, recorded, snapshot,
                   found, misses, editedTerminal>>

RecordOld ==
    /\ process = "old"
    /\ status[OldIntent] = "prepared"
    /\ card[OldCard] \in {"live", "terminal"}
    /\ status' = [status EXCEPT ![OldIntent] = "posted"]
    /\ recorded' = [recorded EXCEPT ![OldIntent] = TRUE]
    /\ UNCHANGED <<process, generation, card, snapshot, found, misses, editedTerminal>>

RenderTerminal ==
    /\ process = "old"
    /\ card[OldCard] = "live"
    /\ card' = [card EXCEPT ![OldCard] = "terminal"]
    /\ UNCHANGED <<process, status, generation, recorded, snapshot,
                   found, misses, editedTerminal>>

ProcessDies ==
    /\ process = "old"
    /\ process' = "down"
    /\ UNCHANGED <<status, generation, card, recorded, snapshot,
                   found, misses, editedTerminal>>

BootSnapshot ==
    /\ process = "down"
    /\ process' = "new"
    /\ snapshot' = {i \in Intents : status[i] \in {"prepared", "posted"}}
    /\ UNCHANGED <<status, generation, card, recorded, found, misses, editedTerminal>>

PrepareNew ==
    /\ process = "new"
    /\ status[NewIntent] = "absent"
    /\ status' = [status EXCEPT ![NewIntent] = "prepared"]
    /\ generation' = [generation EXCEPT ![NewIntent] = "new"]
    /\ UNCHANGED <<process, card, recorded, snapshot, found, misses, editedTerminal>>

PostNew ==
    /\ process = "new"
    /\ status[NewIntent] = "prepared"
    /\ card[NewCard] = "absent"
    /\ card' = [card EXCEPT ![NewCard] = "live"]
    /\ UNCHANGED <<process, status, generation, recorded, snapshot,
                   found, misses, editedTerminal>>

LookupFound(i) ==
    /\ process = "new"
    /\ i \in snapshot
    /\ status[i] \in {"prepared", "posted"}
    /\ \E c \in Cards : Owner(c) = i /\ card[c] = "live"
    /\ found' = [found EXCEPT ![i] = TRUE]
    /\ UNCHANGED <<process, status, generation, card, recorded, snapshot,
                   misses, editedTerminal>>

RecordRecovered(i) ==
    /\ process = "new"
    /\ i \in snapshot
    /\ status[i] = "prepared"
    /\ found[i]
    /\ status' = [status EXCEPT ![i] = "posted"]
    /\ recorded' = [recorded EXCEPT ![i] = TRUE]
    /\ UNCHANGED <<process, generation, card, snapshot, found, misses, editedTerminal>>

EditLiveCard(c) ==
    /\ process = "new"
    /\ c \in Cards
    /\ Owner(c) \in snapshot
    /\ found[Owner(c)]
    /\ status[Owner(c)] = "posted" \* response ID committed before edits
    /\ card[c] = "live" \* live means this card still carries its intent key
    /\ card' = [card EXCEPT ![c] = "terminal"]
    /\ UNCHANGED <<process, status, generation, recorded, snapshot,
                   found, misses, editedTerminal>>

RetireResolved(i) ==
    /\ process = "new"
    /\ i \in snapshot
    /\ status[i] \in {"prepared", "posted"}
    /\ \A c \in Cards : Owner(c) = i => card[c] # "live"
    /\ status' = [status EXCEPT ![i] = "retired"]
    /\ UNCHANGED <<process, generation, card, recorded, snapshot,
                   found, misses, editedTerminal>>

CompleteNoMatch(i) ==
    /\ process = "new"
    /\ i \in snapshot
    /\ status[i] = "prepared" \* NULL response ID only
    /\ ~found[i]
    /\ misses[i] < 2
    /\ misses' = [misses EXCEPT ![i] = @ + 1]
    /\ UNCHANGED <<process, status, generation, card, recorded, snapshot,
                   found, editedTerminal>>

RetireNoMatch(i) ==
    /\ process = "new"
    /\ i \in snapshot
    /\ status[i] = "prepared"
    /\ misses[i] = 2
    /\ ~found[i]
    /\ status' = [status EXCEPT ![i] = "retired"]
    /\ UNCHANGED <<process, generation, card, recorded, snapshot,
                   found, misses, editedTerminal>>

Next == PrepareOld \/ (\E c \in {OldCard, OldDuplicate} : PostOld(c))
     \/ RecordOld \/ RenderTerminal \/ ProcessDies \/ BootSnapshot
     \/ PrepareNew \/ PostNew
     \/ (\E i \in Intents : LookupFound(i) \/ RecordRecovered(i)
                            \/ RetireResolved(i) \/ CompleteNoMatch(i)
                            \/ RetireNoMatch(i))
     \/ (\E c \in Cards : EditLiveCard(c))

\* Mutations calibrate the two safety checks against missing guards.
EditWithoutKey ==
    /\ process = "new"
    /\ OldIntent \in snapshot
    /\ found[OldIntent]
    /\ card[OldCard] = "terminal"
    /\ editedTerminal' = TRUE
    /\ UNCHANGED <<process, status, generation, card, recorded, snapshot,
                   found, misses>>

RefreshSnapshotAll ==
    /\ process = "new"
    /\ status[NewIntent] = "prepared"
    /\ NewIntent \notin snapshot
    /\ snapshot' = {i \in Intents : status[i] \in {"prepared", "posted"}}
    /\ UNCHANGED <<process, status, generation, card, recorded,
                   found, misses, editedTerminal>>

TypeOK ==
    /\ process \in {"old", "down", "new"}
    /\ status \in [Intents -> {"absent", "prepared", "posted", "retired"}]
    /\ generation \in [Intents -> {"none", "old", "new"}]
    /\ card \in [Cards -> {"absent", "live", "terminal"}]
    /\ recorded \in [Intents -> BOOLEAN]
    /\ snapshot \subseteq Intents
    /\ found \in [Intents -> BOOLEAN]
    /\ misses \in [Intents -> 0..2]
    /\ editedTerminal \in BOOLEAN

PostedHasResponseId == \A i \in Intents : status[i] = "posted" => recorded[i]
SnapshotExcludesNewIntent == \A i \in snapshot : generation[i] = "old"
NoTerminalCardEdited == ~editedTerminal
NoStrandedLiveCard == ~\E c \in Cards : card[c] = "live" /\ status[Owner(c)] = "retired"

Spec == Init /\ [][Next]_vars
UnsafeEditSpec == Init /\ [][Next \/ EditWithoutKey]_vars
UnsafeSnapshotSpec == Init /\ [][Next \/ RefreshSnapshotAll]_vars
==================================================================
