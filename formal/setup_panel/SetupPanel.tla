----------------------------- MODULE SetupPanel -----------------------------
EXTENDS Naturals, TLC

CONSTANTS Agents, SafeMode, NoAgent

VARIABLES clicked, loaded, latest, selected, displayedDetails, displayedTarget,
          setupDetails, setupTarget, codingDetails, codingTarget, renderSeq,
          clickRenderSeq

vars == <<clicked, loaded, latest, selected, displayedDetails, displayedTarget,
         setupDetails, setupTarget, codingDetails, codingTarget, renderSeq,
         clickRenderSeq>>

Init ==
    /\ clicked = {}
    /\ loaded = {}
    /\ latest = NoAgent
    /\ selected = NoAgent
    /\ displayedDetails = NoAgent
    /\ displayedTarget = NoAgent
    /\ setupDetails = NoAgent
    /\ setupTarget = NoAgent
    /\ codingDetails = NoAgent
    /\ codingTarget = NoAgent
    /\ renderSeq = 0
    /\ clickRenderSeq = [agent \in Agents |-> 0]

Click(agent) ==
    /\ agent \in Agents \ clicked
    /\ renderSeq = 0
    /\ clicked' = clicked \cup {agent}
    /\ latest' = agent
    /\ selected' = IF SafeMode THEN selected ELSE agent
    /\ clickRenderSeq' = [clickRenderSeq EXCEPT ![agent] = renderSeq]
    /\ UNCHANGED <<loaded, displayedDetails, displayedTarget, setupDetails,
                    setupTarget, codingDetails, codingTarget, renderSeq>>

AdvancePanel ==
    /\ renderSeq = 0
    /\ renderSeq' = renderSeq + 1
    /\ displayedDetails' = NoAgent
    /\ displayedTarget' = NoAgent
    /\ UNCHANGED <<clicked, loaded, latest, selected, setupDetails, setupTarget,
                    codingDetails, codingTarget, clickRenderSeq>>

CompleteDetailsRead(agent) ==
    /\ agent \in clicked \ loaded
    /\ loaded' = loaded \cup {agent}
    /\ IF SafeMode
          THEN IF /\ agent = latest
                  /\ clickRenderSeq[agent] = renderSeq
                  THEN /\ displayedDetails' = agent
                       /\ displayedTarget' = agent
                       /\ selected' = agent
                       /\ renderSeq' = renderSeq + 1
                  ELSE /\ displayedDetails' = displayedDetails
                       /\ displayedTarget' = displayedTarget
                       /\ selected' = selected
                       /\ renderSeq' = renderSeq
          ELSE /\ displayedDetails' = agent
               /\ displayedTarget' = selected
               /\ selected' = selected
               /\ renderSeq' = renderSeq + 1
    /\ UNCHANGED <<clicked, latest, setupDetails, setupTarget, codingDetails,
                    codingTarget, clickRenderSeq>>

OpenSetup ==
    /\ displayedDetails \in Agents
    /\ setupDetails' = displayedDetails
    /\ setupTarget' = IF SafeMode THEN displayedTarget ELSE selected
    /\ UNCHANGED <<clicked, loaded, latest, selected, displayedDetails,
                    displayedTarget, codingDetails, codingTarget, renderSeq,
                    clickRenderSeq>>

MintCodingTools ==
    /\ displayedDetails \in Agents
    /\ codingDetails' = displayedDetails
    /\ codingTarget' = IF SafeMode THEN displayedTarget ELSE selected
    /\ UNCHANGED <<clicked, loaded, latest, selected, displayedDetails,
                    displayedTarget, setupDetails, setupTarget, renderSeq,
                    clickRenderSeq>>

Next ==
    \/ \E agent \in Agents : Click(agent)
    \/ \E agent \in Agents : CompleteDetailsRead(agent)
    \/ AdvancePanel
    \/ OpenSetup
    \/ MintCodingTools

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ clicked \subseteq Agents
    /\ loaded \subseteq Agents
    /\ latest \in Agents \cup {NoAgent}
    /\ selected \in Agents \cup {NoAgent}
    /\ displayedDetails \in Agents \cup {NoAgent}
    /\ displayedTarget \in Agents \cup {NoAgent}
    /\ setupDetails \in Agents \cup {NoAgent}
    /\ setupTarget \in Agents \cup {NoAgent}
    /\ codingDetails \in Agents \cup {NoAgent}
    /\ codingTarget \in Agents \cup {NoAgent}
    /\ renderSeq \in Nat
    /\ clickRenderSeq \in [Agents -> Nat]

SetupTargetMatchesCard ==
    setupDetails = NoAgent \/ setupTarget = setupDetails

CodingTargetMatchesCard ==
    codingDetails = NoAgent \/ codingTarget = codingDetails

=============================================================================
