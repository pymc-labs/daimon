# Eval runs

How to run, score and review the cases of an [eval](evals.md) suite.

## Runs

A run plays each case against each arm, `repeats` times. Each item gets a fresh
session, the fake world and a simulated chat: the case's script plays the people,
pressing buttons, reacting and replying. Where the case lets the simulator answer, it
answers any agent reply that ends in a question mark, so the agent can steer it.

```bash
daimon evals runs estimate draft.json        # low, expected and high cost in USD
daimon evals runs start draft.json --cap 10  # start, spending at most 10 USD
daimon evals runs show RUN_ID
daimon evals runs cancel RUN_ID
daimon evals runs approve RUN_ID --cap 40    # the operator, past the limits below
```

Every run needs a spending cap, and the operator pays for it. A cap above
[`DAIMON_EVALS__ADMIN_APPROVAL_USD`](configuration.md#daimon_evals__admin_approval_usd),
or one that takes a workspace's open runs past
[`DAIMON_EVALS__WORKSPACE_CAP_USD`](configuration.md#daimon_evals__workspace_cap_usd),
waits for the operator. An item starts only while its estimate fits under the cap, so a
run that reaches it stops early as `partial` and keeps the items already done. An item
also stops at its turn and time limits. Arms need a model with a known price. An
agent can start a run only on the person's card in chat.

Eval workers run the queued items. Each item runs a copy of the arm's agent in
its own sandbox. Set the sandbox settings,
[`DAIMON_EVALS__WORLD_URL`](configuration.md#daimon_evals__world_url) and
[`DAIMON_EVALS__ENVIRONMENT_KEY`](configuration.md#daimon_evals__environment_key),
then start one or more:

```bash
daimon evals worker
```

## Scoring

Deterministic checks (call and effect matching in `strict`, `unordered`, `subset` or
`superset` mode, output text checks, limits) score each finished item with no model.
Model judges add rubric, pairwise and reference-guided scores. A pairwise judge sees
both orders; a verdict that flips with position counts as a tie. Judges write critique
first and may answer unknown.

```bash
daimon evals runs judge RUN_ID                # estimate only
daimon evals runs judge RUN_ID --approve 5    # run, spending at most 5 USD
daimon evals runs compare RUN_ID --blind
```

Judging is paid. It never runs without an approved spending ceiling and never spends
past it. A ceiling above
[`DAIMON_EVALS__ADMIN_APPROVAL_USD`](configuration.md#daimon_evals__admin_approval_usd)
needs an admin. Agents over MCP can only ask for the estimate. The run keeps the spend
and who approved it. Running again replaces the same scores.

Name the judge models, or set a default panel in
[`DAIMON_EVALS__JUDGE_MODELS`](configuration.md#daimon_evals__judge_models). A judge
must have a known price and share no model or provider with an arm it judges.

`GET /api/evals/runs/{id}/summary` gives pass rate, rubric score, cost, pass@k and
pass^k per arm. Arms are compared on the same cases with a bootstrap interval that
resamples whole source threads and a non-inferiority check. Fewer than 10 threads give
no verdict. The exact McNemar test runs only when no two cases share a thread.
A recorded baseline has one answer per case, so its variance is unknown. Judges are
checked against human scores with Cohen's kappa; below
[`DAIMON_EVALS__KAPPA_WARN`](configuration.md#daimon_evals__kappa_warn) they are flagged.

## Review

A review queue hands people or agents one task at a time: one item, or the same case
from several arms side by side. A task is reserved for its reviewer for 30 minutes.
A queue can ask for several reviewers per item. Editors adjudicate disagreements with
a score that supersedes the earlier ones. Comments attach to an item, a case or a
passage of a transcript.

Columns are named A, B and so on by place. A blind queue hides model names, model
reactions, usage, times and world origins. It orders the columns randomly per reviewer
and task, and serves only repeats every arm has. The server does this on every surface.
Reviewers always see runs, items and comparisons blind, and get comments with each task.
A task can't be skipped: one left alone comes back when its reservation ends.

```bash
daimon evals review queues
daimon evals review next RUN_ID~QUEUE_ID
daimon evals review submit TASK_ID scores.json
```
