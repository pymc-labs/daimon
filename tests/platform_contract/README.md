# Platform contract

Checks that every Slack and Discord payload Daimon builds is accepted by the platform's
own validators. The adapter tests answer `ok: true` from a fake transport, so they cannot
catch a payload the platform refuses (the Slack feedback modal once shipped
`max_length: 4000`, which Slack rejects above 3000).

- `catalogue.py` lists the payloads by name (`slack/feedback_modal`, `discord/turn_card_done`, ...),
  each built by the adapter's real builders or a real `SlackTurnLifecycle` run. The module
  docstrings of `slack_payloads.py` and `discord_payloads.py` list what is not covered.
- `local_rules.py` holds rules the platforms enforce at send time but their validators miss.
- `test_offline.py` builds every entry and applies those rules. No network. It runs in the
  normal `pytest` run.
- `test_platform_validators.py` sends every entry to the real validators. It is marked
  `platform_contract`, which the default `pytest` deselects (like `contract` and `slow`), and
  each platform is skipped when its variables are unset.

## Run the offline checks

```bash
uv run pytest tests/platform_contract -n 2
```

## Run against the real platforms

Do not use xdist for these: the calls are paced to each platform's rate limit in one process, and the module skips itself under xdist.

| Variable | Used for |
| --- | --- |
| `SLACK_QA_BOT_TOKEN` | Slack `blocks.validate` (the QA workspace bot; needs no scopes) |
| `DISCORD_QA_BOT_TOKEN` | Discord bot that posts to the scratch channel |
| `DISCORD_PLATFORM_CONTRACT_CHANNEL_ID` | id of the scratch channel; the run fails if it is not in the Daimon QA guild (`DISCORD_PLATFORM_CONTRACT_GUILD_ID` overrides the expected guild id) |

Both tokens are in staging Secret Manager. Read them into the test process only, and never
print them:

```bash
SLACK_QA_BOT_TOKEN="$(gcloud secrets versions access latest --secret=SLACK_QA_BOT_TOKEN --project=pymc-daimon-staging)" \
  uv run pytest -m platform_contract tests/platform_contract -k slack
```

For Discord, use a channel in the Daimon QA guild made by the QA driver (the `daimon-qa` skill's
`qa.py new <name> --guild <guild> --parent <category> --unique`, and `qa.py rm <id>` afterwards).
The check posts each message body to that channel, so keep the channel private to the QA bot and
delete it when done:

```bash
SLACK_QA_BOT_TOKEN=... DISCORD_QA_BOT_TOKEN="$(gcloud secrets versions access latest --secret=DISCORD_QA_BOT_TOKEN --project=pymc-daimon-staging)" \
DISCORD_PLATFORM_CONTRACT_CHANNEL_ID=<channel id> \
  uv run pytest -m platform_contract tests/platform_contract
```

### What it does to the platforms

- Slack: one `blocks.validate` call per payload, about one per second. A `ratelimited` answer
  (HTTP 200, no `Retry-After`) is retried with backoff. Nothing is posted.
- Discord: one `POST /channels/{id}/messages` per payload with `allowed_mentions.parse = []`,
  then an immediate `DELETE`. Rejected posts are not deleted because nothing was created.
  Requests stay inside the 5-per-second channel bucket.

### Canaries

Each platform also sends one known-bad payload that must be rejected: the Slack feedback modal with
`max_length: 4000`, and a Discord button with an 81-character label. If a validator accepts
everything (wrong endpoint, a token for a different app, an outage that answers `ok`), the canary
fails the run.

### What this cannot prove

Rendering, ack deadlines, permissions, and what happens after a click. Slack `views.open` and
Discord modals are interaction-only and are not sent: Slack views go through `blocks.validate`
only, and Discord modals are covered by `scripts/lint_discord_modals.py`.
