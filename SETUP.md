# Set up Daimon OS with a coding agent

Give your coding agent this file and say: "Set up Daimon OS and get a local
reply." The agent can do the repository and Docker work. You supply one
Anthropic API key and, if you want a chat integration, create and authorize its
app in a browser.

This path sets up your own deployment. An Anthropic key must come from a
workspace dedicated to it: `daimon defaults apply` manages that workspace's
Managed Agents resources. Start with a clean clone and Docker Compose.

## Local first reply

1. From the clone, check `python3 --version` (3.11 or newer) and
   `docker compose version`.
   Start the timer before preparing `.env`:

   ```bash
   python3 scripts/measure_first_reply.py start --method agent
   python3 scripts/setup.py
   ```

   Setup creates `.env` with mode `0600` and generates
   `DAIMON_MCP__JWT_SECRET`, `POSTGRES_PASSWORD` and
   `DAIMON_CRYPTO__KEYS`. It never prints their values. Read the one-line JSON
   result. An initial run without a key reports
   `DAIMON_ANTHROPIC__API_KEY` under `missing` and names the next human step.
   Running the command again must preserve all existing secret values.

   The first result includes these fields (the command also reports
   `optional_actions` for Discord):

   ```json
   {"schema_version":1,"status":"needs_input","completed":["POSTGRES_PASSWORD","DAIMON_MCP__JWT_SECRET","DAIMON_CRYPTO__KEYS","DAIMON_DATABASE__URL"],"missing":["DAIMON_ANTHROPIC__API_KEY"],"next_step":"Set DAIMON_ANTHROPIC__API_KEY in .env to a key from a dedicated Anthropic workspace.","next_optional":[]}
   ```

2. Stop here for the owner to confirm that this key belongs to a dedicated
   Anthropic workspace and put `DAIMON_ANTHROPIC__API_KEY` in `.env`. The
   agent should not print the key or put it in a command line. Run
   `python3 scripts/setup.py` again and check that the key is no longer
   missing and `status` is `ready`. If the key is unavailable, stop; Docker initialization cannot
   create a working agent without it.

3. Start the local stack. Do not start the Discord service before it has a
   bot token:

   ```bash
   docker compose up --build -d postgres init
   docker compose ps --all
   ```

   Wait for `init` to show `Exited (0)`. If it fails, read
   `docker compose logs init`. It runs migrations, applies the defaults and
   creates the `cli:local` tenant.

4. Create a CLI session and take `session_id` from the returned JSON:

   ```bash
   docker compose run --rm --no-deps --entrypoint daimon init sessions create --json
   ```

   Then run a short first turn, replacing `SESSION_ID` with that value:

   ```bash
   python3 scripts/measure_first_reply.py observe --method agent --human-steps 1 -- \
     docker compose run --rm --no-deps --entrypoint daimon init run \
     --session SESSION_ID "Say hello in one sentence."
   ```

   The turn streams newline-delimited JSON. A `sse` event of type
   `agent.message` with nonempty text is the first reply; a `terminal` event with
   `status: "end_turn"` means the turn completed. The timer prints a separate
   JSON metric on stderr with `seconds_to_first_text`,
   `seconds_to_terminal`, `human_steps` and the terminal status. Keep the
   metric and a redacted turn transcript for the hackathon report. The timer
   stores timestamps only in `.daimon-setup-timing.json` and removes that file
   after the turn.

Leave `DAIMON_MCP__PUBLIC_URL` unset for this CLI check. Managed Agents rejects
a localhost MCP URL because its sessions run remotely. Tools that call back
into Daimon need a public HTTPS URL and a proxy to port 8765. Put that URL in
`.env` and rerun `docker compose run --rm --no-deps --entrypoint daimon init
defaults apply` before testing charts, notebooks, OAuth, Discord or Slack.

## Discord, if wanted

The owner does the browser steps in [the Discord checklist](docs/self-hosting.md#3-create-the-discord-application-optional): create an app and bot, enable Message Content Intent, select `bot` and `applications.commands`, grant the listed permissions, and invite the bot to a test server. Put the token in `.env`, then run:

```bash
docker compose run --rm --no-deps --entrypoint daimon init setup verify discord --guild-id GUILD_ID
docker compose up --build -d mcp scheduler discord
```

The verifier returns JSON. Run the second command only after its status is
`passed`. Fix each named failure and rerun the verifier before testing an
`@mention`. A guild ID is a Discord server ID, not a channel ID. Discord
requires an access review once an app can reach 10,000 users across its
servers. The verifier can check the intent flag but cannot see Discord's
unique-user count or grant approval. Check the Developer Portal for an alert;
Discord's [current policy](https://support-dev.discord.com/hc/en-us/articles/40281523410967-Changes-to-Privileged-Intent-Access-for-Discord-Apps)
explains the threshold and review window.

## Slack, if wanted

Use [the checked-in app manifest](docs/slack-app-manifest.yaml) and follow its
header instructions. Slack accepts the manifest's scopes, events, slash
commands, Socket Mode setting and OAuth redirect. The owner still creates the
app, generates an app-level `connections:write` token and copies the signing
secret, app token, client ID and client secret into `.env`. The OAuth callback
needs a public HTTPS origin routed to the `mcp` service. Then run:

```bash
docker compose --profile slack up --build -d postgres init mcp scheduler slack
```

Open `https://<your-host>/oauth/slack/install` and authorize the workspace.
Run `docker compose run --rm --no-deps --entrypoint daimon init tenants list
--platform slack --json` to check that the tenant exists. See
[the self-hosting guide](docs/self-hosting.md#slack-optional) for the separate
Claude Code login mounts.

## GitHub repositories, if wanted

GitHub App registration is separate from local setup. A future
`daimon github register-app --org <org> --origin <url> --json` command will
guide the owner through creating and installing their own App. The local first
reply and chat integrations do not depend on it. Do not substitute PyMC's App
or create a PAT just to complete this setup.

## Report the result

Tell the owner what replied, the elapsed seconds and the number of human
steps. State any platform still unconfigured and the exact next browser step.
Do not include `.env`, credentials or raw tokens in the report. For a manual
baseline measurement, run the same timer with `--method manual` from the
start of the old [self-hosting path](docs/self-hosting.md); record each manual
step, then use `observe` on the same CLI turn.
