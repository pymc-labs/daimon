# `daimon` — agentic CLI reference

Subprocess-driven turn surface. One process per turn. Stdout is NDJSON; stderr is structlog.

## Commands

### `daimon sessions create [--agent NAME] [--environment NAME] [--json]`
Creates an MA session + local row. `--json` emits `{"session_id","agent","environment"}`.

### `daimon run --session ID [message | --message -]`
Single turn. Streams NDJSON on stdout.

### `daimon sessions get ID --json`
Dump MA session transcript + metadata.

### `daimon help agents`
This page.

## NDJSON kinds (stdout)

Every line carries `kind`, `session_id`, `turn_id`. Kinds:

- `sse` — Anthropic SSE event verbatim (`event.model_dump(mode="json")`).
- `reconnect` — driver reconnected. `reason`: `"connection_dropped"`.
- `rate_limited` — driver hit 429 and is about to sleep. `until`: ISO 8601 or null.
- `interrupt_sent` — driver posted `user.interrupt`. `source`: `"sigint" | "cancel_event"`.
- `terminal` — turn ended. `status`: `"end_turn" | "max_turns" | "failed" | "cancelled"`. Always has `state`. `failed` adds `error`.

### Example — successful turn
```jsonl
{"kind":"sse","session_id":"sess_1","turn_id":"turn_1","event":{"type":"message_start"}}
{"kind":"sse","session_id":"sess_1","turn_id":"turn_1","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello"}}}
{"kind":"terminal","session_id":"sess_1","turn_id":"turn_1","status":"end_turn","state":{}}
```

### Example — failed
```jsonl
{"kind":"terminal","session_id":"sess_1","turn_id":"turn_1","status":"failed","error":{"kind":"upstream","message":"502 bad gateway"},"state":{}}
```

## Exit codes
| Code | Meaning |
|------|---------|
| 0    | terminal with no error (typically `end_turn` or `max_turns`) |
| 1    | bootstrap error, upstream error, bad args |

## Bootstrap errors
- `db_not_migrated` — run `uv run alembic upgrade head`.
- `defaults_missing` — run `daimon defaults apply`.
- `no_default_agent` / `no_default_environment` — pass `--agent NAME` / `--environment NAME` or run `daimon defaults apply`.
- `agent_not_found` / `environment_not_found` — the named agent/env is not in your account or system defaults.

## Environment variables
- `DAIMON_DATABASE__URL` — Postgres DSN (nested-config form; e.g. `postgresql+asyncpg://...`).
- `DAIMON_CLI__LOCAL_USER` — override the OS user for the CLI principal.
- `DAIMON_ANTHROPIC__API_KEY` — Anthropic API key (read via pydantic-settings).

## Workflow for outer agents

1. `daimon sessions create --json` → capture `session_id`.
2. `daimon run --session <id> "<user message>"`.
3. Parse stdout NDJSON; on `terminal.status == "end_turn"` (exit 0) the turn is complete.
