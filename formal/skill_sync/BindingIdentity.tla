-------------------- MODULE BindingIdentity --------------------
EXTENDS Naturals, TLC

CONSTANTS AgentA, AgentB, NoAgent, SafeMode

Phases == {"bridge", "resolved", "fetching", "fetched", "checked",
           "uploaded", "attached", "refused"}

VARIABLES phase, bridgeAgent, targetAgent, attachedAgent, duplicatePresent,
          registryWritten, ambiguousTitleWrite, earlyRefusal
vars == <<phase, bridgeAgent, targetAgent, attachedAgent, duplicatePresent,
          registryWritten, ambiguousTitleWrite, earlyRefusal>>

Init ==
    /\ phase = "bridge"
    /\ bridgeAgent = NoAgent
    /\ targetAgent = NoAgent
    /\ attachedAgent = NoAgent
    /\ duplicatePresent = FALSE
    /\ registryWritten = FALSE
    /\ ambiguousTitleWrite = FALSE
    /\ earlyRefusal = FALSE

BridgeResolve ==
    /\ phase = "bridge"
    /\ phase' = "resolved"
    /\ bridgeAgent' = AgentA
    /\ UNCHANGED <<targetAgent, attachedAgent, duplicatePresent,
                    registryWritten, ambiguousTitleWrite, earlyRefusal>>

AddSameNameAgent ==
    /\ phase \in {"resolved", "fetching", "fetched", "checked", "uploaded"}
    /\ ~duplicatePresent
    /\ duplicatePresent' = TRUE
    /\ UNCHANGED <<phase, bridgeAgent, targetAgent, attachedAgent,
                    registryWritten, ambiguousTitleWrite, earlyRefusal>>

TargetPreflight ==
    /\ phase = "resolved"
    /\ IF SafeMode /\ duplicatePresent
          THEN /\ phase' = "refused"
               /\ earlyRefusal' = TRUE
               /\ UNCHANGED <<targetAgent, registryWritten>>
          ELSE /\ phase' = "fetching"
               /\ targetAgent' =
                      IF SafeMode THEN AgentA
                      ELSE IF duplicatePresent THEN AgentB ELSE AgentA
               /\ UNCHANGED <<registryWritten, earlyRefusal>>
    /\ UNCHANGED <<bridgeAgent, attachedAgent, duplicatePresent,
                    ambiguousTitleWrite>>

FetchDone ==
    /\ phase = "fetching"
    /\ phase' = "fetched"
    /\ UNCHANGED <<bridgeAgent, targetAgent, attachedAgent, duplicatePresent,
                    registryWritten, ambiguousTitleWrite, earlyRefusal>>

PostFetchCheck ==
    /\ phase = "fetched"
    /\ IF SafeMode /\ duplicatePresent
          THEN /\ phase' = "refused"
               /\ earlyRefusal' = TRUE
               /\ UNCHANGED registryWritten
          ELSE /\ phase' = "checked"
               /\ UNCHANGED <<registryWritten, earlyRefusal>>
    /\ UNCHANGED <<bridgeAgent, targetAgent, attachedAgent, duplicatePresent,
                    ambiguousTitleWrite>>

WriteTitle ==
    /\ phase = "checked"
    /\ phase' = "uploaded"
    /\ registryWritten' = TRUE
    /\ ambiguousTitleWrite' = duplicatePresent
    /\ UNCHANGED <<bridgeAgent, targetAgent, attachedAgent, duplicatePresent,
                    earlyRefusal>>

Attach ==
    /\ phase = "uploaded"
    /\ IF SafeMode /\ duplicatePresent
          THEN /\ phase' = "refused"
               /\ UNCHANGED attachedAgent
          ELSE /\ phase' = "attached"
               /\ attachedAgent' =
                      IF SafeMode THEN targetAgent
                      ELSE IF duplicatePresent THEN AgentB ELSE targetAgent
    /\ UNCHANGED <<bridgeAgent, targetAgent, duplicatePresent, registryWritten,
                    ambiguousTitleWrite, earlyRefusal>>

Next == BridgeResolve \/ AddSameNameAgent \/ TargetPreflight \/ FetchDone
        \/ PostFetchCheck \/ WriteTitle \/ Attach
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ phase \in Phases
    /\ bridgeAgent \in {NoAgent, AgentA}
    /\ targetAgent \in {NoAgent, AgentA, AgentB}
    /\ attachedAgent \in {NoAgent, AgentA, AgentB}
    /\ duplicatePresent \in BOOLEAN
    /\ registryWritten \in BOOLEAN
    /\ ambiguousTitleWrite \in BOOLEAN
    /\ earlyRefusal \in BOOLEAN

NoWrongAgentAttach == attachedAgent = NoAgent \/ attachedAgent = AgentA
SafePreflightUsesExactAgent == SafeMode => targetAgent \in {NoAgent, AgentA}
EarlyAmbiguityWritesNothing == earlyRefusal => ~registryWritten
LateAmbiguousTitleWriteRefusesAttach ==
    ambiguousTitleWrite => phase # "attached" /\ attachedAgent = NoAgent
NoAmbiguousTitleWrite == ~ambiguousTitleWrite

=============================================================================
