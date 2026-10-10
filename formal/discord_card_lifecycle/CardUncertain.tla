-------------------------- MODULE CardUncertain --------------------------
EXTENDS Naturals

CONSTANT Mode
VARIABLES phase, card, progress, terminal, accepted, uncertain, ready, dirty, repair
vars == <<phase, card, progress, terminal, accepted, uncertain, ready, dirty, repair>>

Init == /\ phase = "running"
        /\ card = "working"
        /\ progress = FALSE
        /\ terminal = FALSE
        /\ accepted = FALSE
        /\ uncertain = FALSE
        /\ ready = FALSE
        /\ dirty = FALSE
        /\ repair = FALSE

IssueProgress == /\ phase = "running" /\ ~progress
                 /\ progress' = TRUE
                 /\ UNCHANGED <<phase, card, terminal, accepted, uncertain, ready, dirty, repair>>

EndTurn == /\ phase = "running"
           /\ phase' = "ending"
           /\ terminal' = TRUE
           /\ UNCHANGED <<card, progress, accepted, uncertain, ready, dirty, repair>>

Bound == /\ phase = "ending" /\ terminal /\ ~uncertain
         /\ phase' = "ended"
         /\ uncertain' = TRUE
         /\ UNCHANGED <<card, progress, terminal, accepted, ready, dirty, repair>>

\* Discord can accept HTTP 200 before discord.py releases its webhook lock.
AcceptTerminal == /\ terminal /\ ~accepted
                  /\ accepted' = TRUE
                  /\ card' = "answer"
                  /\ UNCHANGED <<phase, progress, terminal, uncertain, ready, dirty, repair>>

FinishTerminal == /\ terminal /\ accepted
                  /\ terminal' = FALSE
                  /\ phase' = "ended"
                  /\ ready' = (Mode = "safe" \/ ~uncertain)
                  /\ repair' = (dirty /\ (Mode = "safe" \/ ~uncertain))
                  /\ UNCHANGED <<card, progress, accepted, uncertain, dirty>>

FinishProgress == /\ progress
                  /\ progress' = FALSE
                  /\ card' = "working"
                  /\ dirty' = (accepted \/ ready)
                  /\ repair' = (repair \/ ready)
                  /\ UNCHANGED <<phase, terminal, accepted, uncertain, ready>>

Repair == /\ repair /\ ~progress /\ ~terminal
          /\ repair' = FALSE
          /\ dirty' = FALSE
          /\ card' = "answer"
          /\ UNCHANGED <<phase, progress, terminal, accepted, uncertain, ready>>

Next == IssueProgress \/ EndTurn \/ Bound \/ AcceptTerminal \/ FinishTerminal
        \/ FinishProgress \/ Repair
Spec == Init /\ [][Next]_vars

\* A settled late progress write must have a same-message repair queued.
RepairQueued == (phase = "ended" /\ ~terminal /\ ~progress /\ card = "working") => repair
=============================================================================
