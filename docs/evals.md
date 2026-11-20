# Evals

Evals check how well an agent handles real work. A suite holds cases: a
conversation, the world the agent can reach, and what a good answer does.
Suites and their results travel between deployments as bundles.

## Turn it on

Evals are off by default. Turn on the `evals` feature for the deployment or a
workspace, as in [Features](features.md). Off hides the eval tools and refuses
every eval request.

To offer the evals section of the [web app](web.md), add `evals` to
[`DAIMON_WEB__MODULES`](configuration.md#daimon_web__modules).

## Suites and cases

A suite has an id, a name and the cases it pins. Each change to a suite saves
a new version, so results always name the version they ran.

A case is one JSON document in the bundle format. Saving a case again adds a
revision and a new suite version. Its hash covers every field, so any edit shows.

Check a case before you save it:

```bash
daimon evals cases validate case.json
```

## Who can do what

| Person | Can |
| --- | --- |
| Workspace owner or admin | Everything, on every suite |
| Suite owner | Everything on their suite, including sharing it |
| Editor | Change cases and export scrubbed bundles |
| Reviewer | See the suite and review results |
| Viewer | See the suite |
| Visitor | Nothing |

Members create suites and own what they create. Owners share a suite with people
or groups. Group membership is checked live. When the platform cannot say, access is denied.

A suite that is not scrubbed cannot be shared.

## Bundles

A bundle is a zip with a `bundle.json` manifest, suites, runs and blobs. The
manifest lists every file with its sha256. Edits outside Daimon break validation.

Validate a bundle folder, zip or encrypted file. This works offline, with no
database:

```bash
daimon evals bundle validate path/to/bundle.zip
```

It prints a JSON report and exits with 1 on any problem. Set
`DAIMON_EVAL_PASSPHRASE` or pass `--passphrase` for an encrypted bundle.

Export and import from the command line:

```bash
daimon evals bundle export --suite support-basics --out support.zip
daimon evals bundle import support.zip
```

Export replaces people with neutral labels. Only an owner or an admin can keep
identities or export content that is not scrubbed. Import refuses a bundle that is
not scrubbed unless an owner or an admin accepts it with `--accept-unscrubbed`.

### Signatures and encryption

Set [`DAIMON_EVALS__SIGNING_KEY`](configuration.md#daimon_evals__signing_key) to sign
exported bundles. Import checks signatures from the keys in
[`DAIMON_EVALS__TRUSTED_KEYS`](configuration.md#daimon_evals__trusted_keys). A bad
signature from a trusted key is refused.

A passphrase encrypts the bundle with AES-256-GCM and a scrypt key. Share the
passphrase another way than the file.

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

Judging is paid. It never runs without an approved spending ceiling, stops when the
ceiling is reached, and refuses a model with no known price. Agents over MCP can only
ask for the estimate. Running again replaces the same scores. The panel comes from
[`DAIMON_EVALS__JUDGE_MODELS`](configuration.md#daimon_evals__judge_models).

`GET /api/evals/runs/{id}/summary` gives pass rate, rubric score, cost, pass@k and
pass^k per arm. Arms are compared on the same cases with a bootstrap interval that
resamples whole source threads, an exact McNemar test and a non-inferiority check.
A recorded baseline has one answer per case, so its variance is unknown. Judges are
checked against human scores with Cohen's kappa; below
[`DAIMON_EVALS__KAPPA_WARN`](configuration.md#daimon_evals__kappa_warn) they are flagged.

## Review

A review queue hands people or agents one task at a time: one item, or the same case
from several arms side by side. A task is reserved for its reviewer for 30 minutes.
A queue can ask for several reviewers per item. Editors adjudicate disagreements with
a score that supersedes the earlier ones. Comments attach to an item, a case or a
passage of a transcript.

A blind queue shows arms as letters, hides model names, model reactions and usage,
and orders the columns randomly per reviewer and task. The server does this on every
surface, and a reviewer who only reviews sees every run they can open this way while a
blind queue is open.

```bash
daimon evals review next RUN_ID~QUEUE_ID
daimon evals review submit TASK_ID scores.json
```

## Where to use it

The same rules apply on every surface:

- The web app, under `/api/evals`.
- MCP tools whose names start with `eval_`, listed in [MCP tools](mcp-tools.md).
- The `daimon evals` commands.
