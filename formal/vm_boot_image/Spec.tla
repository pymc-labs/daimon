--------------------------- MODULE Spec ------------------------------------
EXTENDS Naturals, FiniteSets

CONSTANTS Images, V1, V2, V3, None, Invalid,
          EarlyWorkerRecord, EarlyNotebookRecord, TrustInvalid
ASSUME Images = {V1, V2, V3}
ASSUME Cardinality(Images) = 3
ASSUME V1 # V2 /\ V1 # V3 /\ V2 # V3
ASSUME None \notin Images /\ Invalid \notin Images /\ None # Invalid
VMs == {"worker", "notebook", "report"}

VARIABLES phase, candidate, environment, worker, notebook,
          key, verified, latest, running, defaultImage,
          lookup, booted, bootFrom, bootKey, bootDefault,
          deploys, successful, externalSteps, rebootCount, midDeployOld
vars == <<phase, candidate, environment, worker, notebook,
          key, verified, latest, running, defaultImage,
          lookup, booted, bootFrom, bootKey, bootDefault,
          deploys, successful, externalSteps, rebootCount, midDeployOld>>

Init ==
  /\ phase = "idle" /\ candidate = None /\ environment = "staging"
  /\ worker = "idle" /\ notebook = "idle" /\ deploys = 0
  /\ externalSteps = 0 /\ rebootCount = 0 /\ midDeployOld = FALSE
  /\ key = [v \in VMs |-> IF v = "report" THEN None ELSE V2]
  /\ verified = [v \in VMs |-> IF v = "report" THEN {V1} ELSE {V1, V2}]
  /\ latest = [v \in VMs |-> IF v = "report" THEN V1 ELSE V2]
  /\ running = [v \in VMs |-> IF v = "report" THEN V1 ELSE V2]
  /\ defaultImage = [v \in VMs |-> V1]
  /\ lookup = [v \in VMs |-> "reachable"]
  /\ booted = [v \in VMs |-> None]
  /\ bootFrom = [v \in VMs |-> "none"]
  /\ bootKey = [v \in VMs |-> None]
  /\ bootDefault = [v \in VMs |-> V1]
  /\ successful = [v \in VMs |-> v # "report"]

Start(i, env) ==
  /\ phase = "idle" /\ deploys < 1 /\ i \in Images
  /\ env \in {"staging", "production"}
  /\ phase' = "active" /\ candidate' = i /\ environment' = env
  /\ worker' = "refresh" /\ notebook' = "refresh"
  /\ deploys' = deploys + 1
  /\ UNCHANGED <<key, verified, latest, running, defaultImage,
                  lookup, booted, bootFrom, bootKey, bootDefault, successful, externalSteps, rebootCount, midDeployOld>>

\* deploy.yml:300-382 starts notebook refresh in background; :520-536 joins it.
NotebookRefresh ==
  /\ phase = "active" /\ notebook = "refresh"
  /\ notebook' = "reportGate"
  /\ UNCHANGED <<phase, candidate, environment, worker, key, verified,
                  latest, running, defaultImage, lookup, booted, bootFrom,
                  bootKey, bootDefault, deploys, successful, externalSteps, rebootCount, midDeployOld>>
NotebookReportGate ==
  /\ phase = "active" /\ notebook = "reportGate"
  /\ notebook' = "wait"
  /\ UNCHANGED <<phase, candidate, environment, worker, key, verified,
                  latest, running, defaultImage, lookup, booted, bootFrom,
                  bootKey, bootDefault, deploys, successful, externalSteps, rebootCount, midDeployOld>>
NotebookWait ==
  /\ phase = "active" /\ notebook = "wait"
  /\ worker \in {"health", "failed"}
  /\ notebook' = "health"
  /\ key' = IF EarlyNotebookRecord THEN [key EXCEPT !["notebook"] = candidate]
             ELSE key
  /\ UNCHANGED <<phase, candidate, environment, worker, verified,
                  latest, running, defaultImage, lookup, booted, bootFrom,
                  bootKey, bootDefault, deploys, successful, externalSteps, rebootCount, midDeployOld>>
\* deploy.yml:556-570: /health passes, then a single metadata call writes both keys.
NotebookHealthy ==
  /\ phase = "active" /\ notebook = "health"
  /\ notebook' = "done"
  /\ key' = [key EXCEPT !["notebook"] = candidate, !["report"] = candidate]
  /\ verified' = [verified EXCEPT
                    !["notebook"] = @ \cup {candidate},
                    !["report"] = @ \cup {candidate}]
  /\ latest' = [latest EXCEPT !["notebook"] = candidate,
                              !["report"] = candidate]
  /\ successful' = [successful EXCEPT !["notebook"] = TRUE,
                                      !["report"] = TRUE]
  /\ UNCHANGED <<phase, candidate, environment, worker, running,
                  defaultImage, lookup, booted, bootFrom, bootKey,
                  bootDefault, deploys, externalSteps, rebootCount, midDeployOld>>
\* deploy.yml:561-565: notebook unhealthy; one metadata call records report only.
NotebookUnhealthy ==
  /\ phase = "active" /\ notebook = "health"
  /\ notebook' = "failed"
  /\ key' = [key EXCEPT !["report"] = candidate]
  /\ verified' = [verified EXCEPT !["report"] = @ \cup {candidate}]
  /\ latest' = [latest EXCEPT !["report"] = candidate]
  /\ successful' = [successful EXCEPT !["report"] = TRUE]
  /\ UNCHANGED <<phase, candidate, environment, worker, running,
                  defaultImage, lookup, booted, bootFrom, bootKey,
                  bootDefault, deploys, externalSteps, rebootCount, midDeployOld>>
NotebookFail ==
  /\ phase = "active" /\ notebook \in {"refresh", "reportGate", "wait"}
  /\ notebook' = "failed"
  /\ UNCHANGED <<phase, candidate, environment, worker, key, verified,
                  latest, running, defaultImage, lookup, booted, bootFrom,
                  bootKey, bootDefault, deploys, successful, externalSteps, rebootCount, midDeployOld>>

\* deploy.yml:386-518: env, pull, Discord overlap, up, Slack gate, prune.
WorkerRefresh ==
  /\ phase = "active" /\ worker = "refresh"
  /\ worker' = "health"
  /\ key' = IF EarlyWorkerRecord THEN [key EXCEPT !["worker"] = candidate]
             ELSE key
  /\ UNCHANGED <<phase, candidate, environment, notebook, verified,
                  latest, running, defaultImage, lookup, booted, bootFrom,
                  bootKey, bootDefault, deploys, successful, externalSteps, rebootCount, midDeployOld>>
WorkerHealth ==
  /\ phase = "active" /\ worker = "health" /\ notebook = "done"
  /\ worker' = "smoke"
  /\ UNCHANGED <<phase, candidate, environment, notebook, key, verified,
                  latest, running, defaultImage, lookup, booted, bootFrom,
                  bootKey, bootDefault, deploys, successful, externalSteps, rebootCount, midDeployOld>>
\* deploy.yml:594-637: Cloud Run health; staging smoke; settle floor.
WorkerSmoke ==
  /\ phase = "active" /\ worker = "smoke"
  /\ worker' = "settle"
  /\ UNCHANGED <<phase, candidate, environment, notebook, key, verified,
                  latest, running, defaultImage, lookup, booted, bootFrom,
                  bootKey, bootDefault, deploys, successful, externalSteps, rebootCount, midDeployOld>>
WorkerSettle ==
  /\ phase = "active" /\ worker = "settle"
  /\ worker' = "verify1"
  /\ UNCHANGED <<phase, candidate, environment, notebook, key, verified,
                  latest, running, defaultImage, lookup, booted, bootFrom,
                  bootKey, bootDefault, deploys, successful, externalSteps, rebootCount, midDeployOld>>
\* deploy.yml:638-680: defaults verify has one retry after 60 seconds.
WorkerRetry ==
  /\ phase = "active" /\ worker = "verify1"
  /\ worker' = "verify2"
  /\ UNCHANGED <<phase, candidate, environment, notebook, key, verified,
                  latest, running, defaultImage, lookup, booted, bootFrom,
                  bootKey, bootDefault, deploys, successful, externalSteps, rebootCount, midDeployOld>>
WorkerVerified ==
  /\ phase = "active" /\ worker \in {"verify1", "verify2"}
  /\ worker' = "record"
  /\ UNCHANGED <<phase, candidate, environment, notebook, key, verified,
                  latest, running, defaultImage, lookup, booted, bootFrom,
                  bootKey, bootDefault, deploys, successful, externalSteps, rebootCount, midDeployOld>>
\* deploy.yml:682-694: one atomic add-metadata call after every worker gate.
WorkerRecord ==
  /\ phase = "active" /\ worker = "record"
  /\ worker' = "done"
  /\ key' = [key EXCEPT !["worker"] = candidate]
  /\ verified' = [verified EXCEPT !["worker"] = @ \cup {candidate}]
  /\ latest' = [latest EXCEPT !["worker"] = candidate]
  /\ successful' = [successful EXCEPT !["worker"] = TRUE]
  /\ UNCHANGED <<phase, candidate, environment, notebook, running,
                  defaultImage, lookup, booted, bootFrom, bootKey,
                  bootDefault, deploys, externalSteps, rebootCount, midDeployOld>>
WorkerFail ==
  /\ phase = "active"
  /\ worker \in {"refresh", "health", "smoke", "settle", "verify1", "verify2", "record"}
  /\ worker' = "failed"
  /\ UNCHANGED <<phase, candidate, environment, notebook, key, verified,
                  latest, running, defaultImage, lookup, booted, bootFrom,
                  bootKey, bootDefault, deploys, successful, externalSteps, rebootCount, midDeployOld>>
Finish ==
  /\ phase = "active" /\ worker \in {"done", "failed"}
  /\ notebook \in {"done", "failed"}
  /\ phase' = "idle"
  /\ UNCHANGED <<candidate, environment, worker, notebook, key, verified,
                  latest, running, defaultImage, lookup, booted, bootFrom,
                  bootKey, bootDefault, deploys, successful, externalSteps, rebootCount, midDeployOld>>

\* startup.sh.tftpl worker:32-68, notebook:45-85: metadata lookup/validation.
SetLookup(v, status) ==
  /\ v \in VMs /\ status \in {"reachable", "absent", "unreachable", "invalid"}
  /\ externalSteps < 2
  /\ externalSteps' = externalSteps + 1
  /\ lookup' = [lookup EXCEPT ![v] = status]
  /\ UNCHANGED <<phase, candidate, environment, worker, notebook, key,
                  verified, latest, running, defaultImage, booted,
                  bootFrom, bootKey, bootDefault, deploys, successful, rebootCount, midDeployOld>>
\* Operator startup-script rewrite changes the rendered default independently.
RewriteDefault(v, i) ==
  /\ v \in VMs /\ i \in Images
  /\ externalSteps < 2
  /\ externalSteps' = externalSteps + 1
  /\ defaultImage' = [defaultImage EXCEPT ![v] = i]
  /\ UNCHANGED <<phase, candidate, environment, worker, notebook, key,
                  verified, latest, running, lookup, booted,
                  bootFrom, bootKey, bootDefault, deploys, successful, rebootCount, midDeployOld>>
Reboot(v) ==
  /\ v \in VMs
  /\ rebootCount < 1
  /\ rebootCount' = rebootCount + 1
  /\ lookup[v] # "invalid" \/ TrustInvalid
  /\ LET useKey == lookup[v] \in {"reachable", "invalid"} /\ key[v] # None
         selected == IF lookup[v] = "invalid" THEN Invalid
                     ELSE IF useKey THEN key[v] ELSE defaultImage[v]
     IN /\ midDeployOld' = (midDeployOld \/
           (phase = "active" /\ v = "worker" /\ worker = "record"
            /\ candidate # latest[v] /\ selected = latest[v]))
        /\ running' = [running EXCEPT ![v] = selected]
        /\ booted' = [booted EXCEPT ![v] = selected]
        /\ bootFrom' = [bootFrom EXCEPT ![v] = IF useKey THEN "key" ELSE "default"]
        /\ bootKey' = [bootKey EXCEPT ![v] = key[v]]
        /\ bootDefault' = [bootDefault EXCEPT ![v] = defaultImage[v]]
  /\ UNCHANGED <<phase, candidate, environment, worker, notebook, key,
                  verified, latest, defaultImage, lookup, deploys, successful, externalSteps>>

Progress == (\E i \in Images, env \in {"staging", "production"}: Start(i, env))
  \/ NotebookRefresh \/ NotebookReportGate \/ NotebookWait
  \/ NotebookHealthy \/ NotebookUnhealthy \/ NotebookFail
  \/ WorkerRefresh \/ WorkerHealth \/ WorkerSmoke \/ WorkerSettle
  \/ WorkerRetry \/ WorkerVerified \/ WorkerRecord \/ WorkerFail \/ Finish
Next == Progress
  \/ (\E v \in VMs, s \in {"reachable", "absent", "unreachable", "invalid"}: SetLookup(v, s))
  \/ (\E v \in VMs, i \in Images: RewriteDefault(v, i))
  \/ (\E v \in VMs: Reboot(v))
Spec == Init /\ [][Next]_vars /\ WF_vars(Progress)

TypeOK ==
  /\ phase \in {"idle", "active"} /\ candidate \in Images \cup {None}
  /\ environment \in {"staging", "production"}
  /\ worker \in {"idle", "refresh", "health", "smoke", "settle", "verify1", "verify2", "record", "done", "failed"}
  /\ notebook \in {"idle", "refresh", "reportGate", "wait", "health", "done", "failed"}
  /\ \A v \in VMs: key[v] \in Images \cup {None} /\ verified[v] \subseteq Images
       /\ latest[v] \in Images /\ defaultImage[v] \in Images
       /\ running[v] \in Images \cup {Invalid}
       /\ booted[v] \in Images \cup {None, Invalid}
       /\ lookup[v] \in {"reachable", "absent", "unreachable", "invalid"}
       /\ bootFrom[v] \in {"none", "key", "default"}
       /\ bootKey[v] \in Images \cup {None}
       /\ bootDefault[v] \in Images /\ successful[v] \in BOOLEAN
  /\ deploys \in 0..1 /\ externalSteps \in 0..2 /\ rebootCount \in 0..1
  /\ midDeployOld \in BOOLEAN
BootAuthorized == \A v \in VMs:
  booted[v] = None \/
    ((bootFrom[v] = "key" /\ booted[v] = bootKey[v]) \/
     (bootFrom[v] = "default" /\ booted[v] = bootDefault[v]))
KeyVerified == \A v \in VMs: key[v] = None \/ key[v] \in verified[v]
LatestWhenReachable == \A v \in VMs:
  ~successful[v] \/ booted[v] = None \/ bootFrom[v] # "key"
  \/ booted[v] = latest[v] \/ bootKey[v] # latest[v]
NoMidDeployOld == ~midDeployOld
NoStaleBoot == \A v \in VMs:
  ~successful[v] \/ booted[v] = None \/ booted[v] = latest[v]
NoUnverifiedBoot == \A v \in VMs:
  booted[v] = None \/ booted[v] \in verified[v]
ActiveTerminates == (phase = "active") ~> (phase = "idle")
=============================================================================
