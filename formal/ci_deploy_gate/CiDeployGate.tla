-------------------------- MODULE CiDeployGate --------------------------
EXTENDS TLC

CONSTANT EarlyTag
VARIABLES tests, candidate, deploy, canonical
vars == <<tests, candidate, deploy, canonical>>

Init == /\ tests = "pending"
        /\ candidate = FALSE
        /\ deploy = "idle"
        /\ canonical = FALSE

PublishCandidate ==
    /\ ~candidate
    /\ candidate' = TRUE
    /\ UNCHANGED <<tests, deploy, canonical>>

PassTests ==
    /\ tests = "pending"
    /\ tests' = "passed"
    /\ UNCHANGED <<candidate, deploy, canonical>>

FailTests ==
    /\ tests = "pending"
    /\ tests' = "failed"
    /\ UNCHANGED <<candidate, deploy, canonical>>

StartDeploy ==
    /\ deploy = "idle"
    /\ candidate
    /\ tests = "passed"
    /\ deploy' = "running"
    /\ UNCHANGED <<tests, candidate, canonical>>

FinishDeploy ==
    /\ deploy = "running"
    /\ deploy' = "passed"
    /\ UNCHANGED <<tests, candidate, canonical>>

FailDeploy ==
    /\ deploy = "running"
    /\ deploy' = "failed"
    /\ UNCHANGED <<tests, candidate, canonical>>

TagCanonical ==
    /\ ~canonical
    /\ IF EarlyTag THEN candidate ELSE deploy = "passed"
    /\ canonical' = TRUE
    /\ UNCHANGED <<tests, candidate, deploy>>

Next == PublishCandidate \/ PassTests \/ FailTests \/ StartDeploy
        \/ FinishDeploy \/ FailDeploy \/ TagCanonical
Spec == Init /\ [][Next]_vars

TypeOK == /\ tests \in {"pending", "passed", "failed"}
          /\ candidate \in BOOLEAN
          /\ deploy \in {"idle", "running", "passed", "failed"}
          /\ canonical \in BOOLEAN

PromotableOnlyAfterStagingPass == canonical => (tests = "passed" /\ deploy = "passed")
NoDeployAfterTestFailure == tests = "failed" => deploy = "idle"

=========================================================================
