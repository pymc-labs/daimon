# Default-agent label golden re-record

The Discord built-in identity check now goes through `is_builtin_agent`, which
treats the deployment's default agent as built-in under any name. A reply from
the fixtures' default agent (`test-agent`) no longer starts with the
`-# test-agent` label line, as on Slack and Teams.

Recorded with `uv run python tests/golden/runner.py --turn-path legacy --regen`
against a migrated local test database. Five of 33 files change; the other 28
are byte-identical.

| Golden | Change |
| --- | --- |
| `cold_discord.json` | posted answer loses the `-# test-agent\n` prefix (1) |
| `plain_discord.json` | same (1) |
| `dead_session.json` | same (1) |
| `wake_continuation.json` | same (3) |
| `handoff_continuation.json` | same (3) |

No other field changes: requests, database rows, reactions, embeds and ids are
exact. The legacy and mux paths compare against the same files.
