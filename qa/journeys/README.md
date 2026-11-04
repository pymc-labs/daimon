# QA journeys

The [journey map](JOURNEYS.md) separates scripted application evidence, actual
browser observations, and real-provider checks. This package is operator/test
code; deployed services do not import it.

## Offline and owned-page checks

Use an isolated, migrated PostgreSQL test database. The regular fixture layer
creates a unique schema per run/worker. Chromium drives the real application
routes with external providers replaced in the test process.

```bash
uv sync --all-extras --all-packages
uv run --with playwright==1.63.0 playwright install chromium
DAIMON_DATABASE__TEST_URL=postgresql+asyncpg://USER:PASSWORD@localhost:PORT/TEST_DB \
  uv run --with playwright==1.63.0 pytest qa/journeys/tests -m 'not contract' -n 2
```

`DAIMON_QA_ARTIFACT_ROOT` selects a private directory outside Git checkouts.
Reports include the actual observation boundaries, checks, elapsed minutes and
billed token counts. Screenshots, DOM snapshots and backend receipts stay with
the run. Missing native-client evidence stays PENDING.

The scripted cases cover Discord and Slack turn delivery, usage/debit records,
and setup roster visibility for members and admins. Chromium covers GitHub
selection, save, cancellation, and reopening a used invitation at desktop and
mobile widths. Cancellation leaves no grants; reopening adds no duplicate grant.
The report-host journey renders a local PDF and compares its title, revision,
reader and budget with stored state. It also opens the mobile chat panel. This
browser check allows the viewer's two pinned PDF.js CDN assets; model requests
are disabled. Sidebar answers still need separate provider evidence.

`compare_screenshots` requires an explicitly accepted baseline. It writes
side-by-side comparisons for changed steps and never updates the baseline.
The regression tests deliberately alter an image to verify change detection.

## Dedicated Discord login

```bash
uv run python -m qa.journeys.profile prepare
uv run --with playwright==1.63.0 python -m qa.journeys.profile login
```

The profile lives under `~/.local/state/daimon-qa-journeys/` with private
permissions. Open the login command on a private desktop, log in manually with
the dedicated staging QA account, and close the browser. Use this account only
for staging QA. The launcher does not export cookies or tokens. Never open two
browsers against the profile at once.

Account login alone is not native journey evidence. Until a real staging card
is clicked and its screenshots, DOM state and backend receipt are recorded,
the Discord journey remains PENDING.

## Haiku provider canary

The canary checks a value carried across two turns using `qa/live`'s bot
transport, model probe, usage collection and shared ledger. It makes no native
browser claim. It requires Haiku 5.5 for both pre-trigger and post-turn model
evidence. Production and the live runner's older staging fallback are refused.

Prepare a plan without contacting a provider:

```bash
uv run python -m qa.journeys.canary --budget-usd 0.25
```

Before each paid run, state the budget and inspect the plan. Configure the
existing live runner's staging target, dedicated QA agent, read-only model and
deployment probes, bot driver and usage database. Keep that configuration
private. The per-turn reservation must fit the stated two-turn allowance.

```bash
uv run python -m qa.journeys.canary --budget-usd 0.25 \
  --config /private/staging.json --ledger /private/shared-ledger.jsonl --go
```

This invokes one scenario once, with no automatic repeat or external alerts.
It deletes only its own current-run channels and does not sweep older runs.
Before the second turn, it requires the first turn's database usage receipt and
enough remaining allowance for the next estimated turn. Unknown usage stops it.
Token/runtime allocations are estimates, not provider-enforced billing caps;
unknown usage retains the existing ledger's conservative reservation.
The wrapper adds no new lock or reservation protocol.
