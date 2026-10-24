------------------------------- MODULE GrantCap -------------------------------
EXTENDS Naturals, FiniteSets
CONSTANT Unsafe
Installations == {"A", "B", "C"}
Cap == 2
VARIABLES granted, attempted
vars == <<granted, attempted>>

Init == /\ granted = {}
        /\ attempted = {}

\* The per-agent advisory lock in github_access.py:stage_grant encloses the
\* distinct-installation count and insert. This small model gives one
\* attempted grant per installation; the database test covers extra repos.
Stage(i) == /\ i \in Installations \ attempted
            /\ attempted' = attempted \cup {i}
            /\ granted' = IF i \in granted \/ Cardinality(granted) < Cap \/ Unsafe
                          THEN granted \cup {i} ELSE granted

Next == \E i \in Installations: Stage(i)
Spec == Init /\ [][Next]_vars
TypeOK == /\ granted \subseteq Installations
          /\ attempted \subseteq Installations
WithinCap == Cardinality(granted) <= Cap
=============================================================================
