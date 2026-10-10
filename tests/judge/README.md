# Recorded transcript judge

This is a manual quality check, reported separately from C01–C18 conformance.
`tasks.json` pins 12 task prompts, expected goals and critical dimensions;
`rubric.json` pins five 0–2 score dimensions. Grades must cite existing recorded
event IDs. Full task success and all critical dimensions must score 2 to pass.
A core task blocks only if every configured replay repetition fails (default 3).
Incomplete task coverage is not a backend certificate: results are explicitly
scoped to the supplied recordings. CLI/auth/schema errors stop the run without
turning invalid output into a task grade.

Use a Codex ChatGPT subscription login (`auth_mode=chatgpt`); API-key auth is
refused. The script strips API-key environment variables and provider endpoint
overrides, ignores Codex user config, pins `gpt-6.1-sol`, uses a temporary empty
working directory with a read-only sandbox, and requests schema-constrained JSON.
The task is never replayed through a provider API. Transcript payloads are
untrusted evidence, not judge instructions. Never put credentials into recordings.

The installed CLI's `codex exec --help` verifies `--output-schema`,
`--output-last-message`, `--ephemeral`, `--ignore-user-config`, and `--sandbox`.

Run explicitly:

```sh
uv run python tests/judge/harness.py recordings.json --output grades.json --reps 3
DAIMON_JUDGE_RECORDINGS=recordings.json DAIMON_JUDGE_OUTPUT=grades.json \
  uv run pytest tests/judge/test_manual.py -m judge
```

`judge` is excluded by the root default marker expression. Existing default
collection paths are unchanged. Test the harness without a real judge:
`uv run pytest tests/judge/test_harness.py`. All subprocesses there are fake.

Input is a JSON array, one transcript per backend/profile/task:

```json
[{"task_id":"two_turn_files","backend":"anthropic",
  "profile":"anthropic.managed_agents","recorded_at":"2026-10-09T00:00:00Z",
  "events":[{"id":"event-1","turn":1,"speaker":"tool",
  "type":"file.checksum","payload":{"filename":"probe.bin","sha256":"recorded digest"}}]}]
```

This minimal example is insufficient evidence for task success; real recordings
need both turns, tool effects, checksums and terminal observations. Outputs retain
backend/profile, each repetition's scores, pass counts, rubric/task/transcript
hashes, the pinned judge model and grading date. Same-family judge bias for OpenAI
recordings remains subject to the lead's ~20% Opus spot check (QUESTIONS Q30).

`catalog_runner.py` maps an external QA catalog to headless invocation plans,
without executing its commands, HTTP requests, test jobs or provider turns:

```sh
uv run python tests/judge/catalog_runner.py /path/to/catalog \
  --integration-sha <40-character-integration-head> --run-id qa-fixture \
  --output plans.json
```

The catalog supplies `scenarios/*.yaml`, `SCHEMA.md`, `PROPOSED-KINDS.md` and
`TARGET-53.txt`. Exactly 53 distinct frozen targets produce the scored denominator
of 159 across Anthropic, OpenAI and Gemini. The CLI and replay validator enforce
the frozen TARGET-53 SHA256 `fc14eab684b6aa257ca5c01ab113ef154c9847938d263f2739480299c43cc2f4`;
replay also verifies that the ordered target IDs reproduce that digest. Other scenarios are retained as
unscored extras. Missing targets refuse the matrix; an unknown kind or malformed
scenario affects only that scenario. Source, schema, target and attached fixture
hashes pin the inputs for replay. Setup, steps, assertions, human instructions
and teardown remain in the source snapshot. The Discord set-A global assertions
are included separately for every catalog turn.

Each scenario has a portability classification and explicit gaps. Text, terminal
timing, host lifecycle finalization, artifact and trace assertions map to host
observations; platform layout, reactions, UI and manual checklists require an
adapter surface. CLI/SQL/HTTP jobs, administrative fixtures and unknown extensions
require additional bindings. A classification does not certify execution: every
plan has `evidence_status="pending"` until host fixtures and an outcome oracle
are bound. Nothing earns a PASS in this planning slice.

Channel revisions explicitly name the provider/profile. The requested agent
models are Haiku 5.5, Luna and Gemini 3.8 Flash. Anthropic retains `model=None` in
its channel revision because the agent supplies its model; the host fixture must
verify the requested agent model. Non-Anthropic channel admission remains an
explicit integration gap until its host wiring lands, with no provider fallback.
Turn numbering includes context-only messages and every burst text. Mentioned
follow-ups reuse their channel's planned thread; unmentioned replies, burst
concurrency and reply-to-chunk routing retain adapter gaps. Placeholders expand
only from explicitly supplied values, once; no environment variable is read.

Focused verification: `uv run pytest tests/judge/test_catalog_runner.py`.

`headless_executor.py` executes the frozen scored matrix offline, starting with
Anthropic. It always rebuilds plans from catalog files with the frozen target
pin, checks each authored tape's source hash and exact expanded request text,
and passes every original/global assertion to `daimon.testing.outcome_oracle`.

```sh
uv run python tests/judge/headless_executor.py /path/to/catalog \
  --replays tests/judge/fixtures/catalog_anthropic.json \
  --integration-sha <40-character-integration-head> --run-id qa-replay \
  --output results.json
```

The actual core `turn.driver.run_turn(path="mux")` uses the real SDK with a
closed scripted HTTP transport, per-channel backend admission, explicit
`TurnBackendRequest`, and a fenced `TurnPersistence` journal in `MemoryStateStore`.
The fixture's prepared session is fetched and its frozen agent model checked.
Terminals come from journal records belonging to the acknowledged submitted
input, with host lifecycle closure checked separately. Timing includes setup and
closure, conservatively bounding root completion. Every scripted HTTP reply and
SSE record must be consumed; corruption refuses the run, including under Python
`-O`. A mentioned follow-up selects its persisted thread binding and reuses the
session. This executor owns all binding selections in its capture window;
production admission, replacements/recovery, billing and platform delivery need
separate bindings.

Authored tapes prove host protocol replay, not model instruction following or
remote tool/file effects. The initial tapes cover five catalog scenarios and six
turns, including a tool-only completion and a mentioned follow-up. Commands in
provider tool records are never executed locally. Administrative setup, context
fetch, attachments, burst concurrency, waits and special reply routing remain
explicit execution gaps. Original setup/teardown commands are never run, and
no workspace cleanup occurs. Preview/reconnect tapes need a later capture slice.
OpenAI and Gemini dispatch zero requests and remain typed PENDING until G1/G2
host wiring is available. The matrix planner still tracks the 30 extras separately;
the executor report scores only the frozen 159 cells.

Results distinguish full-cell status from `catalog_outcome` (all original/global
checks) and `host_outcome` (supplemental completion/session checks). A passing
supplemental check never replaces a missing catalog assertion. With oracle #675,
the initial frozen run has **1 original check PASS and 13 supplemental checks
PASS**, while **all 159 full cells remain PENDING** for uncaptured/unsupported
predicates. Text/card/platform capture will bind to N3's subsequent oracle slices.
All provider calls here use SDK mock transports; no key, database, live provider
or judge is contacted.

Focused verification: `uv run pytest tests/judge/test_headless_executor.py -q -n 2`.
