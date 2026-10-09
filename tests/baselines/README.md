# Content-free baselines

The lead runs `telemetry.sql` read-only against existing production telemetry;
N9 never connects to production. The SQL uses a read-only transaction, a 30 s
statement timeout and UTC. It emits one aggregate JSON object for the last
14 days, with no tenant, caller, channel, session, event or message identifiers.
No content tables are queried.

```sh
# Lead only; use the already-authorized read-only connection.
psql -X -qAt -v ON_ERROR_STOP=1 -f tests/baselines/telemetry.sql > telemetry.json
uv run python tests/baselines/convert.py telemetry.json \
  --sdk-pin anthropic==0.117.0 --date 2026-10-09 --output baseline.json
```

Supply the actual deployed SDK version and UTC export date, not the example
values. The converter verifies the 14-day window, date, known schema and
percentile ordering, then records the SDK pin, SQL/export hashes and date.
Baseline JSON is generated only from an export; no fake production baseline
is checked in.

Tokens come from `usage_events`, which covers metered tenant calls, excluding
exempt DMs. Its legacy input/cache-read/cache-write buckets are disjoint. The
converter computes input mix without double counting; no observations stay
null, and a measured zero has null ratios. Whole-turn p50/p95 duration comes
from SYS-066 `turn_outcomes`, counted once per distinct model in a turn,
including multi-model turns. It is not individual model-request latency.
Historical outcomes without model IDs cannot enter a model cohort. Usage-only
cohorts retain null latency; latency-only cohorts retain null token totals.

First-token timestamps do not exist in the current schema. Their p50/p95
are null with an explicit unavailable reason, never total latency or zero.
Future measurement needs N4/N8 instrumentation and a reviewed query change.

# M0 replay overhead

`replay.py` returns pending until N4 supplies a real legacy/mux bridge adapter:

```sh
uv run python tests/baselines/replay.py --output replay.json
# Once N4 lands an offline adapter:
uv run python tests/baselines/replay.py --adapter module:factory --output replay.json
```

The adapter must select both `DAIMON_TURN__PATH=legacy|mux` values when building
settings/bridge, restore the flag afterwards, reset state each turn, and use
only the scripted transport. It returns the path actually selected, normalized
effect digest, fake-call count and observed time to first normalized event. The harness rejects wrong paths, mismatched
effects/work and external I/O. It alternates path order, discards 20 warmup
pairs and measures 200 pairs using a monotonic clock. Added overhead is the
paired mux-minus-legacy duration: p50 ≤5 ms and p95 ≤20 ms. Pending has no
measurements and never passes the gate. First-event p50/p95 and its paired added
latency are also reported for both paths under the fake transport; this requires
no new runtime instrumentation. Fake unit-test timings are not M0 evidence.

Validate offline against N9's isolated local database:

```sh
DAIMON_DATABASE__TEST_URL=postgresql+asyncpg://daimon:daimon@localhost:5432/daimon_test_nc_n9 \
  uv run pytest tests/baselines -q
```
