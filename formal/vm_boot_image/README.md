# VM boot image after a gated deploy

Question: can a reboot start an unverified image, or an image older than the last verified deploy? The model separates the image currently running, the recorded metadata key, and the startup script's rendered default for the worker, notebook, and report services. The notebook and report share one VM but have separate image keys. Both can happen through the rendered default. If metadata is absent or unreachable, a stale default can boot an older image. An operator can also rewrite the default to an image that has not passed this VM's gates. With reachable, valid metadata, a reboot uses the last recorded image.

## Actions and source boundaries

The code references are on `fleet/deploy-records-images` in this repo and `fleet/boot-reads-images` in the companion `daimon-private` checkout. Each model action is atomic at the cited boundary; the two VM refresh paths can interleave.

| Model action | Code boundary |
| --- | --- |
| `Start` | `.github/workflows/deploy.yml:66-68` serializes deploys by environment, without canceling a running one; `:18-38` receives an SHA and environment. |
| `NotebookRefresh`, `NotebookReportGate`, `NotebookFail` | `.github/workflows/deploy.yml:300-382` starts the background refresh, fetches env/assets, pulls and starts containers, tests report-host health, and stores its exit code. |
| `NotebookWait` | `.github/workflows/deploy.yml:520-536` joins the background result after worker refresh and before recording. The unsafe switch writes the notebook key here, before its health gate. |
| `NotebookHealthy` | `.github/workflows/deploy.yml:538-570` validates digest refs, retries notebook `/health`, then writes notebook and report keys in one `add-metadata` call. |
| `NotebookUnhealthy` | `.github/workflows/deploy.yml:556-565` writes only the report key when notebook health fails, then exits failed. |
| `WorkerRefresh`, `WorkerFail` | `.github/workflows/deploy.yml:386-518` refreshes env and compose assets, pulls, overlaps Discord when supported, runs `up -d`, checks Slack liveness and prunes. The unsafe switch writes the worker key at this early point. |
| `WorkerHealth`, `WorkerSmoke`, `WorkerSettle` | `.github/workflows/deploy.yml:538-637` checks Cloud Run health after notebook recording, runs smoke only in staging, and waits for adapter reconcile. In production the smoke action represents the skipped step. |
| `WorkerRetry`, `WorkerVerified` | `.github/workflows/deploy.yml:638-680` verifies shipped defaults with one retry after 60 seconds. Both attempts can fail. |
| `WorkerRecord` | `.github/workflows/deploy.yml:682-694` validates the digest and writes `daimon-image` after all worker gates. |
| `Finish` | `.github/workflows/deploy.yml:520-570,594-694` gives each path a terminal success or failure after its steps. |
| `SetLookup`, `Reboot` | Companion `infra/terraform/modules/worker_vm/startup.sh.tftpl:32-72` and `notebook_host_vm/startup.sh.tftpl:45-92`: metadata lookup, digest validation, default fallback on absent/unreachable lookup, refusal on invalid value, then compose startup. |
| `RewriteDefault` | The same templates interpolate `${daimon_image}`, `${notebook_image}`, and `${report_image}` at `worker_vm/startup.sh.tftpl:68` and `notebook_host_vm/startup.sh.tftpl:81-85`. An operator rewrite is an explicit environmental action, not a deploy gate. |

## Bounds and assumptions

| Input | Model bound / meaning | Source or status |
| --- | --- | --- |
| Images | `v1 < v2 < v3`; one modeled deploy, chosen from any of the three. Initial worker and notebook keys are `v2`; their `v1` and `v2` passed earlier gates. The report key is absent, with rendered default `v1`. This permits a fresh `v3` deploy and a verified rollback to `v1`. | Finite scaling assumption; manual older-SHA redeploy is supported by `.github/workflows/deploy.yml:15-28` and `.github/workflows/promote.yml:90-93`. |
| VMs | Worker, notebook, report keys; notebook and report share one VM. | `.github/workflows/deploy.yml:538-570`; companion startup templates cited above. |
| Worker gates | Refresh, Cloud Run health, staging smoke, settle, defaults verify with one retry, record. Every gate may fail. | `.github/workflows/deploy.yml:386-518,594-694`; smoke staging-only at `:607-609`. |
| Notebook gates | Refresh/report health, wait, notebook health with 12 tries (5 seconds between failures), then paired record or report-only record. | `.github/workflows/deploy.yml:300-382,520-570`. |
| External actions | At most two lookup/default rewrites and one reboot, interleaved anywhere. A lookup may be reachable, absent, unreachable, or invalid. | Bounded exploration; response classification follows companion startup templates. |
| Metadata writes | Each `add-metadata` call is atomic. The paired notebook/report call changes both keys together. Calls succeed when modeled as a record action. | Unsupported service assumption; `deploy.yml:563-570,693-694` shows call boundaries, but this model has no GCE contract test. |
| Initial history | For worker and notebook, `v1` and `v2` are previously verified, current key `v2`, default `v1`. Report starts without a key and with default `v1`. | Unsupported setup assumption to expose stale fallback and rollback in one deploy. |
| Scheduling | `WF_vars(Progress)` makes an enabled deploy step eventually advance; reboot and operator actions need no fairness. | Fairness assumption for `ActiveTerminates`, not a GitHub Actions service guarantee. |

The image labels represent chronology. A rollback to `v1` becomes the latest verified deployment for that VM once its gates pass. `verified` is a history set; `latest` tracks the most recent successful record, including rollback. A failed deploy can still leave a new report key when report health passed but notebook health failed, as the workflow explicitly does.

## Checked properties

- `BootAuthorized`: each boot image equals the recorded key or rendered default observed at that reboot. Safe invalid metadata has no boot transition.
- `KeyVerified`: every recorded image has passed that VM's modeled gates in some deploy.
- `LatestWhenReachable`: a reboot through a reachable metadata key uses the latest successfully recorded image at that time. A later successful record may update `latest` after the reboot.
- `ActiveTerminates`: every started deploy eventually reaches terminal `idle`, under weak fairness of the deploy progress action.
- `NoStaleBoot`, `NoUnverifiedBoot`, and `NoMidDeployOld` are intentionally false witness properties.

| Config | TLC verdict | Distinct states | Meaning |
| --- | --- | ---: | --- |
| `Safe` | clean | 141590 | Correct recording and boot validation; safety and fair termination. |
| `EarlyWorker` | violates `KeyVerified` | 75 | Worker metadata written before health, smoke, and defaults gates. |
| `EarlyNotebook` | violates `KeyVerified` | 15509 | Notebook metadata written after report gate but before notebook health. |
| `TrustInvalid` | violates `BootAuthorized` | 181 | Boot accepts an invalid metadata value. |
| `StaleDefault` | violates `NoStaleBoot` | 233 | Missing metadata falls back to old rendered `v1` after verified `v2`. |
| `MidDeploy` | violates `NoMidDeployOld` | 102405 | Reboot after worker gates but before record uses previous verified image. |
| `UnverifiedDefault` | violates `NoUnverifiedBoot` | 306 | Rewritten default boots an image that report gates have not verified. |

Counterexamples in plain words:

1. Early worker record: Start a new `v3` deploy, finish the VM refresh, then write `v3` as the boot key before the downstream gates. The key now names an image that has not passed them.
2. Early notebook record: Start `v3`, pass report health and join the refresh, then write the notebook key before `/health` passes. A notebook failure would leave an unverified boot key.
3. Trust invalid: A metadata lookup returns a malformed digest. A boot implementation that accepts it attempts to start that value; the actual script refuses boot.
4. Stale fallback: The last verified key is `v2`, but the rendered default is `v1`. If metadata is absent or unreachable, reboot starts `v1`.
5. Mid deploy: Start a new deploy, pass worker gates, then reboot before `Record verified worker image`. The key remains `v2`, so the reboot uses that previous verified image.
6. Unverified default: The report key is absent. An operator changes its rendered default from `v1` to `v2`, then reboots. Report gates have not verified `v2`, yet the startup script selects it as the default.

The check covers one bounded deploy, two external changes, and one reboot. It does not cover every deployment history or production digest. It omits Docker pull/cache failures after the startup selection, VM power state, GCE propagation delay, metadata read/write races within a call, build provenance, artifact retention, and external writers of image metadata. An invalid lookup is a boot-time response abstraction and does not change the stored key. No staging trace, GCE contract test, or executable replay of these counterexamples has been run, so correspondence to the external service remains provisional. Workflow and startup-script branches were checked against the cited source; the existing workflow contract tests provide only structural coverage.
