---------------------------- MODULE GrantAdmission ----------------------------
EXTENDS Naturals, FiniteSets
CONSTANT Start, Fault
Repos == {"a1", "a2", "a3", "b"}
Home(r) == IF r = "b" THEN "B" ELSE "A"
ReadOnly == IF Start = "read" THEN {"a1"} ELSE {}
Candidate == IF Start = "read" THEN "b" ELSE "a3"
Cap == 5
VARIABLES granted, attempted
vars == <<granted, attempted>>

Init == /\ granted = IF Start = "read" THEN {"a1"} ELSE {"a1", "a2", "b"}
        /\ attempted = FALSE

\* Two repos per token chunk stand in for GitHub's 500. The budget start
\* has A's first chunk full and B's one chunk; another A repo adds a chunk.
ChunkCount(s) == LET a == Cardinality({r \in s: Home(r) = "A"})
                     b == Cardinality({r \in s: Home(r) = "B"})
                 IN (IF a = 0 THEN 0 ELSE IF a <= 2 THEN 1 ELSE 2)
                    + (IF b = 0 THEN 0 ELSE 1)
Multi(s) == (\E a, b \in s: Home(a) # Home(b))
VaultCount(s) == IF s = {} THEN 1
                 ELSE IF Multi(s) THEN ChunkCount(s) + 3
                 ELSE 3
\* github_access.py:stage_grant checks both constraints while holding the
\* per-agent grant lock and before inserting the grant.
Stage == /\ ~attempted
         /\ attempted' = TRUE
         /\ granted' = IF (VaultCount(granted \cup {Candidate}) <= Cap \/ Fault = "budget")
                          /\ (~Multi(granted \cup {Candidate})
                               \/ (granted \cup {Candidate}) \cap ReadOnly = {}
                               \/ Fault = "read")
                        THEN granted \cup {Candidate} ELSE granted
Next == Stage
Spec == Init /\ [][Next]_vars
TypeOK == /\ granted \subseteq Repos /\ attempted \in BOOLEAN
WithinBudget == VaultCount(granted) <= Cap
MultiWrite == ~Multi(granted) \/ granted \cap ReadOnly = {}
=============================================================================
