-------------------------- MODULE WizardLifecycle --------------------------
EXTENDS FiniteSets, Naturals, TLC

CONSTANTS Open, Submitted, Abandoned, V0, V1, NoVersion,
          GuardSubmitVersion, GuardSweepUpdate

Statuses == {Open, Submitted, Abandoned}
Versions == {V0, V1}

VARIABLES status, expired, answersVersion, submitSnapshot,
          submitBeforeExpiry, submitInFlight, sweepSelected,
          secondSubmitWaiting, secondSubmitLost, everSubmitted,
          submittedVersion, turnsStarted
vars == <<status, expired, answersVersion, submitSnapshot,
          submitBeforeExpiry, submitInFlight, sweepSelected,
          secondSubmitWaiting, secondSubmitLost, everSubmitted,
          submittedVersion, turnsStarted>>

Init ==
    /\ status = Open
    /\ expired = FALSE
    /\ answersVersion = V0
    /\ submitSnapshot = NoVersion
    /\ submitBeforeExpiry = FALSE
    /\ submitInFlight = FALSE
    /\ sweepSelected = FALSE
    /\ secondSubmitWaiting = FALSE
    /\ secondSubmitLost = FALSE
    /\ everSubmitted = FALSE
    /\ submittedVersion = NoVersion
    /\ turnsStarted = 0

ReadSubmit ==
    /\ status = Open
    /\ ~expired
    /\ submitSnapshot' = answersVersion
    /\ submitBeforeExpiry' = FALSE
    /\ UNCHANGED <<status, expired, answersVersion, submitInFlight,
                   sweepSelected, secondSubmitWaiting, secondSubmitLost,
                   everSubmitted, submittedVersion, turnsStarted>>

CaptureSubmitTime ==
    /\ status = Open
    /\ ~expired
    /\ submitSnapshot \in Versions
    /\ submitBeforeExpiry' = TRUE
    /\ UNCHANGED <<status, expired, answersVersion, submitSnapshot,
                   submitInFlight, sweepSelected, everSubmitted,
                   secondSubmitWaiting, secondSubmitLost,
                   submittedVersion, turnsStarted>>

EditAnswers ==
    /\ status = Open
    /\ ~expired
    /\ ~submitInFlight
    /\ answersVersion = V0
    /\ answersVersion' = V1
    /\ UNCHANGED <<status, expired, submitSnapshot, submitBeforeExpiry,
                   submitInFlight, sweepSelected, everSubmitted,
                   secondSubmitWaiting, secondSubmitLost,
                   submittedVersion, turnsStarted>>

Expire ==
    /\ ~expired
    /\ expired' = TRUE
    /\ UNCHANGED <<status, answersVersion, submitSnapshot, submitBeforeExpiry,
                   submitInFlight, sweepSelected, everSubmitted,
                   secondSubmitWaiting, secondSubmitLost,
                   submittedVersion, turnsStarted>>

BeginSubmit ==
    /\ status = Open
    /\ submitBeforeExpiry
    /\ ~submitInFlight
    /\ (~GuardSubmitVersion \/ submitSnapshot = answersVersion)
    /\ submitInFlight' = TRUE
    /\ UNCHANGED <<status, expired, answersVersion, submitSnapshot,
                   submitBeforeExpiry, sweepSelected, everSubmitted,
                   secondSubmitWaiting, secondSubmitLost,
                   submittedVersion, turnsStarted>>

WaitSecondSubmit ==
    /\ status = Open
    /\ submitInFlight
    /\ ~secondSubmitWaiting
    /\ secondSubmitWaiting' = TRUE
    /\ UNCHANGED <<status, expired, answersVersion, submitSnapshot,
                   submitBeforeExpiry, submitInFlight, sweepSelected,
                   secondSubmitLost, everSubmitted, submittedVersion,
                   turnsStarted>>

CommitSubmit ==
    /\ status = Open
    /\ submitInFlight
    /\ status' = Submitted
    /\ submitInFlight' = FALSE
    /\ everSubmitted' = TRUE
    /\ submittedVersion' = submitSnapshot
    /\ turnsStarted' = turnsStarted + 1
    /\ UNCHANGED <<expired, answersVersion, submitSnapshot,
                   submitBeforeExpiry, sweepSelected, secondSubmitWaiting,
                   secondSubmitLost>>

RejectSecondSubmit ==
    /\ status = Submitted
    /\ secondSubmitWaiting
    /\ secondSubmitWaiting' = FALSE
    /\ secondSubmitLost' = TRUE
    /\ UNCHANGED <<status, expired, answersVersion, submitSnapshot,
                   submitBeforeExpiry, submitInFlight, sweepSelected,
                   everSubmitted, submittedVersion, turnsStarted>>

SelectExpired ==
    /\ status = Open
    /\ expired
    /\ ~sweepSelected
    /\ sweepSelected' = TRUE
    /\ UNCHANGED <<status, expired, answersVersion, submitSnapshot,
                   submitBeforeExpiry, submitInFlight, everSubmitted,
                   secondSubmitWaiting, secondSubmitLost,
                   submittedVersion, turnsStarted>>

MarkAbandoned ==
    /\ sweepSelected
    /\ ~submitInFlight
    /\ (~GuardSweepUpdate \/ (status = Open /\ expired))
    /\ status' = Abandoned
    /\ sweepSelected' = FALSE
    /\ UNCHANGED <<expired, answersVersion, submitSnapshot,
                   submitBeforeExpiry, submitInFlight, everSubmitted,
                   secondSubmitWaiting, secondSubmitLost,
                   submittedVersion, turnsStarted>>

Next ==
    \/ ReadSubmit
    \/ CaptureSubmitTime
    \/ EditAnswers
    \/ Expire
    \/ BeginSubmit
    \/ WaitSecondSubmit
    \/ CommitSubmit
    \/ RejectSecondSubmit
    \/ SelectExpired
    \/ MarkAbandoned
    \/ UNCHANGED vars

TypeOK ==
    /\ status \in Statuses
    /\ expired \in BOOLEAN
    /\ answersVersion \in Versions
    /\ submitSnapshot \in Versions \cup {NoVersion}
    /\ submitBeforeExpiry \in BOOLEAN
    /\ submitInFlight \in BOOLEAN
    /\ sweepSelected \in BOOLEAN
    /\ secondSubmitWaiting \in BOOLEAN
    /\ secondSubmitLost \in BOOLEAN
    /\ everSubmitted \in BOOLEAN
    /\ submittedVersion \in Versions \cup {NoVersion}
    /\ turnsStarted \in 0..1

SubmittedUsesCurrentAnswers ==
    status = Submitted => submittedVersion = answersVersion

SubmittedNeverDowngrades == everSubmitted => status = Submitted

AtMostOneTurnStarts == turnsStarted <= 1

Spec == Init /\ [][Next]_vars
ProgressSpec ==
    /\ Spec
    /\ WF_vars(CommitSubmit)
    /\ WF_vars(RejectSecondSubmit)
    /\ WF_vars(SelectExpired)
    /\ WF_vars(MarkAbandoned)

SubmitEventuallySettles == submitInFlight ~> status = Submitted
SecondSubmitEventuallyLoses == secondSubmitWaiting ~> secondSubmitLost
ExpiredOpenEventuallySettles ==
    (status = Open /\ expired) ~> (status = Submitted \/ status = Abandoned)

THEOREM Spec => []TypeOK
THEOREM Spec => []AtMostOneTurnStarts
=============================================================================
