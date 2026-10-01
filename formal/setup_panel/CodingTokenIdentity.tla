----------------------- MODULE CodingTokenIdentity -----------------------
EXTENDS Naturals, TLC

CONSTANTS Agents, Tenants, NoAgent, NoTenant, CallerTenant, CardAgent,
          NameCandidate, SafeMode, Invalidation

VARIABLES cardAgent, duplicatePresent, cardExists, cardArchived,
          cardTenantMatches, tokenAgent, tokenTenant

vars == <<cardAgent, duplicatePresent, cardExists, cardArchived,
          cardTenantMatches, tokenAgent, tokenTenant>>

Init ==
    /\ cardAgent = NoAgent
    /\ duplicatePresent = FALSE
    /\ cardExists = TRUE
    /\ cardArchived = FALSE
    /\ cardTenantMatches = TRUE
    /\ tokenAgent = NoAgent
    /\ tokenTenant = NoTenant

RenderCard ==
    /\ cardAgent = NoAgent
    /\ cardAgent' = CardAgent
    /\ UNCHANGED <<duplicatePresent, cardExists, cardArchived,
                    cardTenantMatches, tokenAgent, tokenTenant>>

ReplaceOrDuplicateName ==
    /\ cardAgent = CardAgent
    /\ ~duplicatePresent
    /\ duplicatePresent' = TRUE
    /\ UNCHANGED <<cardAgent, cardExists, cardArchived,
                    cardTenantMatches, tokenAgent, tokenTenant>>

InvalidateCard ==
    /\ cardAgent = CardAgent
    \* Model only changes observed by the live check; post-mint revocation is out of scope.
    /\ tokenAgent = NoAgent
    /\ Invalidation \in {"archive", "missing", "foreign"}
    /\ cardExists' = IF Invalidation = "missing" THEN FALSE ELSE cardExists
    /\ cardArchived' = IF Invalidation = "archive" THEN TRUE ELSE cardArchived
    /\ cardTenantMatches' =
           IF Invalidation = "foreign" THEN FALSE ELSE cardTenantMatches
    /\ UNCHANGED <<cardAgent, duplicatePresent, tokenAgent, tokenTenant>>

Mint ==
    /\ cardAgent = CardAgent
    /\ tokenAgent = NoAgent
    /\ IF SafeMode
          THEN IF cardExists
                  /\ ~cardArchived
                  /\ cardTenantMatches
               THEN /\ tokenAgent' = cardAgent
                    /\ tokenTenant' = CallerTenant
               ELSE /\ tokenAgent' = NoAgent
                    /\ tokenTenant' = NoTenant
          ELSE /\ tokenAgent' = IF duplicatePresent
                                   THEN NameCandidate
                                   ELSE CardAgent
               /\ tokenTenant' = CallerTenant
    /\ UNCHANGED <<cardAgent, duplicatePresent, cardExists,
                    cardArchived, cardTenantMatches>>

Next == RenderCard \/ ReplaceOrDuplicateName \/ InvalidateCard \/ Mint
Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ cardAgent \in Agents \cup {NoAgent}
    /\ duplicatePresent \in BOOLEAN
    /\ cardExists \in BOOLEAN
    /\ cardArchived \in BOOLEAN
    /\ cardTenantMatches \in BOOLEAN
    /\ tokenAgent \in Agents \cup {NoAgent}
    /\ tokenTenant \in Tenants \cup {NoTenant}

CardIdentityMatches == tokenAgent = NoAgent \/ tokenAgent = CardAgent

TokenTenantBound == tokenTenant = NoTenant \/ tokenTenant = CallerTenant

UnavailableCardNotMinted ==
    (cardExists /\ ~cardArchived /\ cardTenantMatches)
        \/ tokenAgent = NoAgent

=============================================================================
