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
