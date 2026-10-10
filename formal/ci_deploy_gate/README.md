# CI deploy gate

Question: can a failed or still-running main build acquire the canonical
seven-character SHA tag that `promote.yml` accepts?

| Model action | Workflow boundary |
| --- | --- |
| `PublishCandidate` | `ci.yml` `docker-publish-candidate` finishes all three pushes, independently of tests. |
| `PassTests` / `FailTests` | All jobs in `ci.yml` `deploy-gcp.needs` resolve; any failure or skip makes the verdict fail. |
| `StartDeploy` | GitHub Actions starts `deploy-gcp` only if all `needs` succeeded. |
| `FinishDeploy` / `FailDeploy` | `deploy.yml` migrations, VM refresh, health, smoke, settle and defaults gates finish or fail. |
| `TagCanonical` | Final `deploy.yml` step writes the three short-SHA tags, daimon last. `promote.yml` recognizes the daimon tag. |

The invariant requires a promotable tag only after successful tests and staging
gates.

| Config | Verdict | Distinct states | Meaning |
| --- | --- | ---: | --- |
| `CiDeployGateSafe` | clean | 10 | Candidate may finish first; canonical tag waits for all gates. |
| `CiDeployGateUnsafe` | violates `PromotableOnlyAfterStagingPass` | 7 | Candidate finish can expose the SHA before tests. |

The unsafe config models assigning the SHA tag when the candidate build
finishes, before test results. Its shortest counterexample is: candidate build
finishes; canonical tag is assigned while tests are still pending. The safe
config allows the candidate to finish first, but waits for both test and deploy
verdicts before tagging. Failed and skipped test jobs both map to `failed`;
GitHub Actions' [documented `needs` semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idneeds)
skip a dependent job after a failed or skipped prerequisite unless its `if`
overrides that status. This is checked structurally by
`tests/test_ci_workflow_contract.py` and ultimately by the first real CI run.
Artifact Registry [accepts a digest as the source of `tags add`](https://docs.cloud.google.com/sdk/gcloud/reference/artifacts/docker/tags/add).

The model has one test verdict, one candidate bundle, and one deploy verdict.
It does not model per-job timing, registry consistency, or a prior successful
run of the same SHA. An old canonical tag from a previous successful run remains
valid even if a rerun fails. The model is provisional until a staging run and a
failed-test run replay its transitions.
