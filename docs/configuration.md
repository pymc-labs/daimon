# Configuration reference

Every environment variable daimon reads. Generated from the settings models themselves
by `scripts/generate_config_reference.py` — edit the `Field(description=...)` in the
model, not this page. CI fails when the two disagree.

Values come from the process environment and, for the daimon processes, from a `.env`
file in the working directory. `.env.example` lists the same `DAIMON_*` variables in
copy-paste form; this page adds the types, the defaults and the two standalone services.
Nested blocks use `__` as the delimiter, so `DAIMON_MCP__JWT_SECRET` is
`Settings.mcp.jwt_secret`.

Unknown `DAIMON_*` variables are ignored rather than rejected (`extra="ignore"`), so a
typo is silent — check the spelling here.

## Contents

- [Core](#core)
- [Database](#database)
- [Anthropic](#anthropic)
- [CLI](#cli)
- [Logging](#logging)
- [MCP Server](#mcp-server)
- [Hub](#hub)
- [Discord](#discord)
- [Thread Participation](#thread-participation)
- [Slack](#slack)
- [Teams](#teams)
- [GitHub](#github)
- [Crypto](#crypto)
- [Credentials](#credentials)
- [Gemini](#gemini)
- [Notebook Host](#notebook-host)
- [Report Host](#report-host)
- [Sentry](#sentry)
- [Billing Policy](#billing-policy)
- [Support](#support)
- [Thread Naming](#thread-naming)
- [Artifacts](#artifacts)
- [Scheduler](#scheduler)
- [Notebook host (standalone service)](#notebook-host-standalone-service)
- [Report host (standalone service)](#report-host-standalone-service)
- [Billing (Stripe)](#billing-stripe)
- [Docker Compose](#docker-compose)

## Core

Read from `daimon.core.config.Settings`. Prefix `DAIMON_`. Every other `DAIMON_*`
section below is a nested block on this model, reached with the `__` delimiter.

### `DAIMON_PRIVACY_POLICY_URL`

`HttpUrl` · optional · default `https://github.com/pymc-labs/daimon/blob/main/PRIVACY.md`

URL rendered on the Discord and Slack privacy panels' Policy button. Override via
DAIMON_PRIVACY_POLICY_URL if you host your own policy page.

### `DAIMON_DEFAULTS_ROOT`

`Path` · optional · default `defaults`

Filesystem path to the seeded defaults/ directory consumed by
daimon.core.defaults.apply.apply_defaults. Default Path('defaults') is relative to the
process cwd — works for in-repo dev (repo root) and the deployed container layout
(working dir contains defaults/ at root). Single source of truth shared by the
scheduler, Discord/Slack adapters, CLI session bootstrap, and MCP routine tools. Do NOT
add adapter-local defaults_root fields — they invite drift.

## Database

Read from `daimon.core.config.DatabaseSettings`. Prefix `DAIMON_DATABASE__`.

### `DAIMON_DATABASE__URL`

`PostgresDsn` · **required**

Postgres connection string used by the running application (SQLAlchemy async + asyncpg).
Required.

### `DAIMON_DATABASE__TEST_URL`

`PostgresDsn | None` · optional · default unset

Postgres connection string for the test suite. Points at a dedicated database (e.g.
daimon_test) so test runs never touch development data. Unset in production.

## Anthropic

Read from `daimon.core.config.AnthropicSettings`. Prefix `DAIMON_ANTHROPIC__`.

### `DAIMON_ANTHROPIC__API_KEY`

`SecretStr` · **required** · secret

Anthropic API key used to authenticate all Managed Agents SDK calls. Required.

### `DAIMON_ANTHROPIC__BASE_URL`

`HttpUrl` · optional · default `https://api.anthropic.com`

Base URL for the Anthropic API. Override only when routing through a proxy or a non-
default API endpoint.

## CLI

Read from `daimon.core.config.CLISettings`. Prefix `DAIMON_CLI__`.

### `DAIMON_CLI__LOCAL_USER`

`str` · optional · default read from the process environment

Display name used to identify the local operator running the CLI. Defaults to the $USER
environment variable, falling back to 'daimon' when unset.

## Logging

Read from `daimon.core.config.LogSettings`. Prefix `DAIMON_LOG__`.

### `DAIMON_LOG__LEVEL`

`'DEBUG' | 'INFO' | 'WARNING' | 'ERROR'` · optional · default `INFO`

Minimum log level emitted by the structured logger.

## MCP Server

Read from `daimon.core.config.McpSettings`. Prefix `DAIMON_MCP__`.

MCP adapter config.

Both fields are optional so deployments that don't run the MCP adapter keep working. The
`create_mcp_app` factory re-validates presence at server-boot time (raises
`BootstrapError` on miss); `ensure_mcp_vault` at session-create time skips silently when
`public_url is None`.

### `DAIMON_MCP__JWT_SECRET`

`SecretStr | None` · **required to run the MCP adapter** · default unset · secret

Secret used to sign and verify MCP bearer tokens. Required to run the MCP adapter.

### `DAIMON_MCP__PUBLIC_URL`

`HttpUrl | None` · **required to run the MCP adapter** · default unset

Externally reachable base URL of the MCP server (the streamable endpoint, e.g.
https://mcp.example.com/mcp). Required to run the MCP adapter — used to build
OAuth/CLI/health route URLs and session-create metadata.

### `DAIMON_MCP__FILE_STORE_DIR`

`Path | None` · optional · default unset

On-disk directory for the media-tool FileStore. When unset, the server uses
tempfile.gettempdir() / 'daimon-mcp-files' resolved at startup.

### `DAIMON_MCP__BUNDLE_MAX_BYTES`

`int` · optional · default `26214400`

Per-upload byte cap enforced by the bundle upload route. The default keeps headroom
under a 32 MiB proxy request limit; raise it if the front end in front of the mcp
service allows larger bodies.

### `DAIMON_MCP__BUNDLE_UPLOADS_PER_HOUR`

`int` · optional · default `20`

Per-token cap on bundle upload route calls per rolling hour. Prevents a compromised or
buggy caller from exhausting the Files API upload path. Set to 0 to disable (not
recommended in production).

### `DAIMON_MCP__BUNDLE_TTL_DAYS`

`int` · optional · default `90`

How long an uploaded bundle object is retained on the Files API before deletion.
Deletion is performed by the scheduler's pending-file sweeper, not by this process
directly — a deployment running no scheduler will never reclaim these objects.

## Hub

Read from `daimon.core.config.HubSettings`. Prefix `DAIMON_HUB__`.

OAuth-login MCP mounts for coding-agent clients.

A platform's mount appears only when both its client id and secret are set; a deployment
that sets neither runs exactly as before. The signing key is shared by both mounts and
must be a base64url 32-byte key, the same shape as a Fernet key, so the proxy uses it
directly rather than deriving one from the client secret (rotating a client secret would
otherwise invalidate every issued login).

### `DAIMON_HUB__SLACK_CLIENT_ID`

`str | None` · optional · default unset

Slack OAuth app client ID for the /slack/mcp login mount. Register
&lt;DAIMON_MCP__PUBLIC_URL origin&gt;/slack/auth/callback as a redirect URL on the Slack
app.

### `DAIMON_HUB__SLACK_CLIENT_SECRET`

`SecretStr | None` · optional · default unset · secret

Slack OAuth app client secret for the /slack/mcp login mount.

### `DAIMON_HUB__DISCORD_CLIENT_ID`

`str | None` · optional · default unset

Discord OAuth app client ID for the /discord/mcp login mount. Register
&lt;DAIMON_MCP__PUBLIC_URL origin&gt;/discord/auth/callback as a redirect URI on the
Discord app.

### `DAIMON_HUB__DISCORD_CLIENT_SECRET`

`SecretStr | None` · optional · default unset · secret

Discord OAuth app client secret for the /discord/mcp login mount.

### `DAIMON_HUB__ALLOWED_CLIENT_REDIRECT_URIS`

`list[str]` · optional · default `['http://localhost:*', 'http://127.0.0.1:*', 'https://claude.ai/*', 'https://claude.com/*']`

Redirect URI patterns an MCP client may register with the hub login mounts (wildcards
allowed). Defaults cover coding agents on loopback and claude.ai; widen only for a
client you operate.

### `DAIMON_HUB__JWT_SIGNING_KEY`

`SecretStr | None` · optional · default unset · secret

Base64url 32-byte key that signs hub login tokens. Required when any hub mount is
configured, and rejected at boot unless it decodes to exactly 32 bytes. Generate with
Fernet.generate_key().

## Discord

Read from `daimon.core.config.DiscordSettings`. Prefix `DAIMON_DISCORD__`.

Discord adapter config.

Optional so non-Discord deployments keep working. The ``__main__.py`` entrypoint
validates presence at boot time.

This whole block is optional: it stays unset until at least one of its variables is set,
and the features that read it are inactive while it is.

### `DAIMON_DISCORD__BOT_TOKEN`

`SecretStr` · **required** · secret

Discord bot token. Required to run the Discord adapter.

### `DAIMON_DISCORD__MAX_CONCURRENT_TURNS_PER_TENANT`

`int` · optional · default `3`

Maximum number of agent turns a single tenant (Discord guild) may have in flight at
once. Caps one noisy guild from starving others on the shared Anthropic key.

### `DAIMON_DISCORD__HEALTH_PORT`

`int` · optional · default `8081`

Port for the Discord process's liveness endpoint. Must not collide with the mcp process
(8080) or the scheduler process (8082) — all process groups share one host.

### `DAIMON_DISCORD__PER_CALLER_THREAD_SESSIONS`

`bool` · optional · default `True`

When True (default), each Discord thread keeps a separate agent session per calling
user, so no user inherits another user's session identity or permissions in a shared
thread. When False, a single session is shared by every caller in the thread — a legacy
fallback, not recommended for production. Setting this False also exposes credentials:
the shared session's token is minted for the thread starter's account, so any
participant can prompt the agent into calling get_cli_token and receive the starter's
bound PAT as plaintext.

### `DAIMON_DISCORD__QA_BOT_USER_IDS`

`tuple[str, ...]` · optional · default unset

Discord user ids of automated QA bots allowed to start turns by mention. Bot-authored
mentions are rejected by default; these are allow-listed so a harness can drive the
mention path end-to-end without a human. Several are supported because admin-gated tools
need a caller holding Manage Server while the refusal paths need one without it. Leave
empty outside test deployments -- an allow-listed bot spends real credit. As a tuple
field this must be set as a JSON array, e.g. '["123","456"]'. daimon's own id is refused
at the gate even if listed.

### `DAIMON_DISCORD__BOT_DISPLAY_NAME`

`str` · optional · default `daimon`

The bot's presented name across user-facing Discord render sites (@mentions, setup
messages, help text, privacy panel copy). Defaults to 'daimon' so unset deployments
render byte-identical output. Set to a distinct name (e.g. 'daimon-staging') so a non-
production deployment is visibly distinct in-channel.

## Thread Participation

Read from `daimon.core.config.ThreadParticipationSettings`. Prefix
`DAIMON_THREAD_PARTICIPATION__`.

Organic thread participation: replying in a thread unprompted.

Platform-agnostic settings (the store and tool are keyed by platform); only the Discord
adapter reads them today. `mode` is the deployment tier of a cascade (deployment,
workspace, channel, thread) that the agent's `set_thread_participation` tool writes the
other tiers of. `off` (the default) changes nothing for anyone: no classifier runs and
every server behaves as today until someone asks the agent to follow a thread, or an
admin turns a channel or the workspace on. `disabled` also refuses those requests. `on`
follows every thread unless a lower tier says otherwise.

Replying in threads unprompted. See ThreadParticipationSettings.

### `DAIMON_THREAD_PARTICIPATION__MODE`

`ParticipationMode` · optional · default `off`

Deployment default for replying in threads unprompted. off: mention-only until a thread,
channel or workspace is turned on. disabled: mention-only and cannot be turned on. on:
follow every thread unless turned off below.

### `DAIMON_THREAD_PARTICIPATION__QUIET_SECONDS`

`float` · optional · default `8.0`

How long a followed thread must be quiet after an unprompted message before the agent
decides whether to reply. A burst of messages is judged once, at the end.

### `DAIMON_THREAD_PARTICIPATION__MAX_PER_HOUR`

`int` · optional · default `20`

Backstop: maximum unprompted replies per thread per rolling hour.

### `DAIMON_THREAD_PARTICIPATION__RECENT_MESSAGES_WINDOW`

`int` · optional · default `10`

How many earlier thread messages the classifier sees before deciding.

### `DAIMON_THREAD_PARTICIPATION__CLASSIFIER_MODEL`

`str` · optional · default `claude-haiku-4-5`

Model that decides whether a burst of unprompted messages deserves a reply. One short
call per quiet burst; a small, fast model is the point.

## Slack

Read from `daimon.core.config.SlackSettings`. Prefix `DAIMON_SLACK__`.

Slack adapter config.

Optional so non-Slack deployments boot unchanged — the block is ``None`` when no
``DAIMON_SLACK__*`` env vars are present. Mirrors ``DiscordSettings``.

The OAuth/install flow reads these fields: ``client_id`` / ``client_secret`` for the
code-exchange, ``signing_secret`` for request verification, ``app_token`` for Socket
Mode.

This whole block is optional: it stays unset until at least one of its variables is set,
and the features that read it are inactive while it is.

### `DAIMON_SLACK__BOT_DISPLAY_NAME`

`str` · optional · default `daimon`

The bot's presented name in Slack setup, help, and privacy messages.

### `DAIMON_SLACK__SIGNING_SECRET`

`SecretStr` · **required** · secret

Slack request-signing secret used to verify inbound HTTP requests. Required — keeps the
whole Slack block None when no DAIMON_SLACK__* vars are set.

### `DAIMON_SLACK__APP_TOKEN`

`SecretStr` · **required** · secret

Slack app-level token (xapp-...) used to open the Socket Mode connection.

### `DAIMON_SLACK__CLIENT_ID`

`str | None` · optional · default unset

Slack OAuth app client ID, used during the 'Add to Slack' install flow.

### `DAIMON_SLACK__CLIENT_SECRET`

`SecretStr | None` · optional · default unset · secret

Slack OAuth app client secret, used during the 'Add to Slack' install flow.

### `DAIMON_SLACK__MAX_CONCURRENT_TURNS_PER_TENANT`

`int` · optional · default `3`

Maximum number of agent turns a single tenant (Slack workspace) may have in flight at
once. Caps one noisy workspace from starving others on the shared Anthropic key.

### `DAIMON_SLACK__HEALTH_PORT`

`int` · optional · default `8083`

Port for the Slack process's liveness endpoint. Must not collide with the mcp process
(8080), the discord process (8081), or the scheduler process (8082) — all process groups
share one host.

## Teams

Read from `daimon.core.config.TeamsSettings`. Prefix `DAIMON_TEAMS__`.

Microsoft Teams adapter config.

Optional so non-Teams deployments boot unchanged — the block is ``None`` when no
``DAIMON_TEAMS__*`` env vars are present. Mirrors ``SlackSettings``.

``client_id`` / ``client_secret`` / ``tenant_id`` are the Entra app registration the Bot
Framework posts activities to; ``port`` is the HTTP ingress the SDK's FastAPI adapter
binds (``/api/messages`` plus the ``/healthz`` / ``/readyz`` endpoints served by the
same listener); ``enabled`` gates ``/api/messages`` without taking the process down.

This whole block is optional: it stays unset until at least one of its variables is set,
and the features that read it are inactive while it is.

### `DAIMON_TEAMS__CLIENT_ID`

`str` · **required**

Entra (Azure AD) app registration client ID the Teams bot authenticates as — also the
audience inbound Bot Framework JWTs are validated against.

### `DAIMON_TEAMS__CLIENT_SECRET`

`SecretStr` · **required** · secret

Entra app registration client secret, used to mint Bot Framework tokens for outbound
sends.

### `DAIMON_TEAMS__TENANT_ID`

`str` · **required**

Entra tenant ID the app registration lives in. A single-tenant bot only answers
activities whose conversation and channel-data tenant both equal this value.

### `DAIMON_TEAMS__MAX_CONCURRENT_TURNS_PER_TENANT`

`int` · optional · default `3`

Maximum number of agent turns a single Teams tenant may have in flight at once. Caps one
noisy tenant from starving others on the shared Anthropic key.

### `DAIMON_TEAMS__PORT`

`int` · optional · default `3978`

Port for the Teams process's HTTP ingress and health endpoints (/api/messages, /healthz,
/readyz). The Bot Framework messaging endpoint must be configured to reach this
listener.

### `DAIMON_TEAMS__ENABLED`

`bool` · optional · default `True`

When False, /api/messages answers 503 while the health endpoints stay live — the process
keeps running so ingress can be re-enabled without a redeploy.

## GitHub

Read from `daimon.core.config.GithubSettings`. Prefix `DAIMON_GITHUB__`.

GitHub repository access config (App-or-PAT; no OAuth flow). All fields are optional so
deployments without GitHub App or PAT config keep working.

Cloning via the GitHub App requires only app_id + app_private_key. app_slug is needed
only to construct the App's install link; a deployment that never surfaces that link can
leave it unset. webhook_secret is optional and required only for the skill-sync push-
driven resync webhook.

### `DAIMON_GITHUB__OAUTH_SCOPES`

`tuple[str, ...]` · optional · default `repo,read:user,read:org,workflow`

OAuth scopes requested when a GitHub token-broker flow is used. Not consulted for GitHub
App or PAT authentication.

### `DAIMON_GITHUB__APP_ID`

`str | None` · optional · default unset

GitHub App ID used to mint short-lived installation tokens for repo cloning.

### `DAIMON_GITHUB__APP_PRIVATE_KEY`

`SecretStr | None` · optional · default unset · secret

Private key (PEM) for the GitHub App identified by app_id.

### `DAIMON_GITHUB__APP_SLUG`

`str | None` · optional · default unset

GitHub App's URL name, used to build the App's install link so members can be pointed at
it from chat and from the setup panel. Cloning does not need it, so a deployment that
never surfaces the install link can leave it unset.

### `DAIMON_GITHUB__WEBHOOK_SECRET`

`SecretStr | None` · optional · default unset · secret

Secret used to verify GitHub webhook payload signatures. Required only to enable the
skill-sync push-driven resync webhook.

### `DAIMON_GITHUB__FALLBACK_PAT`

`SecretStr | None` · optional · default unset · secret

Operator-wide personal access token used as the clone credential for public-repo
bindings that have no per-agent credential. Needs no scopes — any valid token clones a
public repo.

### `DAIMON_GITHUB__MAX_TARBALL_BYTES`

`int` · optional · default `52428800`

Raw (compressed) size cap enforced while streaming a GitHub tarball download, checked
against Content-Length when present and against the cumulative streamed byte count
regardless. 50 MiB is the operator default. Set to 0 to disable (not recommended in
production).

### `DAIMON_GITHUB__MAX_TARBALL_DECOMPRESSED_BYTES`

`int` · optional · default `209715200`

Decompressed (extracted) size cap enforced against the sum of tar member sizes before
extraction, guarding against zip bombs. 200 MiB is the operator default. Set to 0 to
disable (not recommended in production).

## Crypto

Read from `daimon.core.config.CryptoSettings`. Prefix `DAIMON_CRYPTO__`.

MultiFernet keys for at-rest token encryption.

A single deployment ships one key; rotation means prepending a new key. Each key must be
a Fernet.generate_key()-style base64-urlsafe 32-byte string. Empty default lets
deployments without any encrypted credentials boot without crypto config.

### `DAIMON_CRYPTO__KEYS`

`tuple[SecretStr, ...]` · optional · default unset · secret

Ordered tuple of Fernet keys used to encrypt/decrypt stored credentials. The first key
encrypts new values; older keys remain valid for decrypting existing ciphertext during
rotation.

## Credentials

Read from `daimon.core.config.CredentialsSettings`. Prefix `DAIMON_CREDENTIALS__`.

Tenant-level credentials for the token broker.

Optional — the Google Workspace provider raises a config error when `google_sa_json` is
unset.

### `DAIMON_CREDENTIALS__GOOGLE_SA_JSON`

`SecretStr | None` · optional · default unset · secret

Full JSON contents of a Google service-account key, used by the token broker to mint
delegated Google Workspace credentials.

## Gemini

Read from `daimon.core.config.GeminiSettings`. Prefix `DAIMON_GEMINI__`.

Optional Gemini API credentials for media MCP tools.

When unset, the media tools skip registration so the ``mcp`` process boots without them.

### `DAIMON_GEMINI__API_KEY`

`SecretStr | None` · optional · default unset · secret

Gemini API key used by media-generation MCP tools. Tools are unregistered when unset.

## Notebook Host

Read from `daimon.core.config.NotebookSettings`. Prefix `DAIMON_NOTEBOOK__`.

Optional notebook-host client config.

Both fields optional so deployments without a notebook host keep working. The MCP tool
raises ToolError when host_url is unset.

### `DAIMON_NOTEBOOK__HOST_URL`

`HttpUrl | None` · optional · default unset

Base URL of the notebook-host service (e.g. http://notebook-host:8001).

### `DAIMON_NOTEBOOK__ADMIN_SECRET`

`SecretStr | None` · optional · default unset · secret

Bearer secret used to authenticate admin calls to the notebook-host service.

### `DAIMON_NOTEBOOK__PUBLISH_RATE_PER_HOUR`

`int` · optional · default `30`

Per-principal cap on publish_notebook calls per rolling hour. Prevents a compromised or
buggy agent from exhausting the notebook host's process/port pool. Set to 0 to disable
(not recommended in production).

### `DAIMON_NOTEBOOK__MAX_ATTACHMENT_BYTES`

`int` · optional · default `10485760`

Per-attachment size cap enforced by attach_notebook_data before uploading to the
notebook host. 10 MiB is the operator default. Set to 0 to disable (the host's own
ceiling still applies as defense-in-depth).

### `DAIMON_NOTEBOOK__MAX_SOURCE_BYTES`

`int` · optional · default `1048576`

Per-upload byte budget signed into notebook upload tokens. 1 MiB mirrors the notebook
host's own ceiling, which is enforced independently as a second layer of defense.

## Report Host

Read from `daimon.core.config.ReportHostSettings`. Prefix `DAIMON_REPORT_HOST__`.

Optional report-host client config.

Both fields optional so deployments without a report host keep working. The publishing
MCP tool raises ToolError when host_url is unset.

### `DAIMON_REPORT_HOST__HOST_URL`

`HttpUrl | None` · optional · default unset

Base URL of the report-host service (e.g. http://report-host:8002).

### `DAIMON_REPORT_HOST__ADMIN_SECRET`

`SecretStr | None` · optional · default unset · secret

Bearer secret used to authenticate admin calls to the report-host service. Must be the
same value the report host itself is configured with (its DAIMON_REPORT__ADMIN_SECRETS)
— rotating one without the other breaks publishing.

## Sentry

Read from `daimon.core.config.SentrySettings`. Prefix `DAIMON_SENTRY__`.

Sentry observability config.

All fields optional so deployments without Sentry keep booting. When `dsn is None`,
Sentry initialization is a no-op.

### `DAIMON_SENTRY__DSN`

`SecretStr | None` · optional · default unset · secret

Sentry DSN. When unset, error reporting is disabled entirely.

### `DAIMON_SENTRY__ENVIRONMENT`

`str` · optional · default `production`

Environment tag attached to every Sentry event (e.g. 'production', 'staging').

### `DAIMON_SENTRY__TRACES_SAMPLE_RATE`

`float` · optional · default `0.0`

Fraction (0.0-1.0) of transactions sampled for Sentry performance tracing.

## Billing Policy

Read from `daimon.core.config.BillingSettings`. Prefix `DAIMON_BILLING__`.

Money policy: markup multiplier and trial credit seed.

Distinct from BillingConfig (billing.py) which holds Stripe secrets (flat STRIPE_* env
vars). BillingSettings is nested DAIMON_BILLING__* policy; BillingConfig is Stripe
secrets. Keep both separate.

### `DAIMON_BILLING__MARKUP`

`Decimal` · optional · default `1.0`

Multiplier applied to raw Anthropic usage cost before billing the tenant. 1.0 = pass-
through.

### `DAIMON_BILLING__SIGNUP_CREDIT`

`Decimal` · optional · default `10.00`

USD credit automatically seeded when a guild/workspace is provisioned, so a freshly-
installed tenant can chat immediately on trial credit before paying. Set to 0 to require
payment before use.

## Support

Read from `daimon.core.config.SupportSettings`. Prefix `DAIMON_SUPPORT__`.

Human-support escalation: where requests land, and how many each user gets.

`credits_per_user` is a COUNT of human interactions, deliberately not the USD in
`BillingSettings.signup_credit`. Sharing a ledger with billing would let a support
request eat the tenant's ability to run turns, and would give a paid-up tenant unlimited
support. Different unit, different table.

An unset `escalation_channel_id` disables the escalate affordance entirely rather than
recording requests nobody will ever see. Failing closed is the honest behaviour: an
escalate button that reaches no one is worse than no button, because the person believes
they have asked for help.

### `DAIMON_SUPPORT__ESCALATION_CHANNEL_ID`

`str | None` · optional · default unset

Channel id where human-support requests are posted. Unset (the default) disables the
escalate affordance entirely — a request that reaches nobody is worse than no button at
all. A channel rather than operator DMs: it survives one person's DMs being closed, and
it leaves a shared record anyone on the rota can pick up. The bot must be able to post
there.

### `DAIMON_SUPPORT__CREDITS_PER_USER`

`int` · optional · default `3`

How many human-support requests each user gets within a tenant. A COUNT of interactions,
NOT the USD in DAIMON_BILLING__SIGNUP_CREDIT — the two are deliberately separate
ledgers. 0 disables escalation.

## Thread Naming

Read from `daimon.core.config.ThreadNamingSettings`. Prefix `DAIMON_THREAD_NAMING__`.

Automatic Discord thread titles, generated by a metered Haiku call.

A top-level feature block (``DAIMON_THREAD_NAMING__*``), not a ``DiscordSettings``
field, per the feature-settings rule. Discord-only in effect: Slack threads have no
title (``tests/parity/ test_thread_naming_discord_only.py``).

### `DAIMON_THREAD_NAMING__ENABLED`

`bool` · optional · default `True`

Title bot-created Discord threads from the opening message with a short Haiku-generated
name, chosen before the thread is created. The call is metered to the tenant like any
other model call. Set false to keep the static 'Chat with &lt;agent&gt;' title.

### `DAIMON_THREAD_NAMING__MAX_INPUT_CHARS`

`int` · optional · default `2000`

Characters of the opening message sent to the naming model; longer messages are cut
here. Bounds the per-thread naming cost.

### `DAIMON_THREAD_NAMING__TIMEOUT_SECONDS`

`float` · optional · default `5.0`

Seconds to wait for the naming model before the thread opens under the static title. The
thread is created only after this call, so this is the most a mention can wait before
anything appears.

## Artifacts

Read from `daimon.core.config.ArtifactsSettings`. Prefix `DAIMON_ARTIFACTS__`.

Optional private object storage for hosted-client artifacts.

This whole block is optional: it stays unset until at least one of its variables is set,
and the features that read it are inactive while it is.

Optional private S3-compatible store for presigned chart links. Bounded image embeds
still run when this is unset.

### `DAIMON_ARTIFACTS__ENDPOINT_URL`

`HttpUrl` · **required**

S3-compatible bucket endpoint; used for uploads and presigned GET URLs.

### `DAIMON_ARTIFACTS__BUCKET`

`str` · **required**

Private bucket name. Daimon never applies a public-read ACL.

### `DAIMON_ARTIFACTS__ACCESS_KEY_ID`

`SecretStr` · **required** · secret

S3-compatible access key id.

### `DAIMON_ARTIFACTS__SECRET_ACCESS_KEY`

`SecretStr` · **required** · secret

S3-compatible secret access key.

### `DAIMON_ARTIFACTS__REGION`

`str` · optional · default `us-east-1`

S3 signing region supplied by the bucket provider.

### `DAIMON_ARTIFACTS__URL_TTL_SECONDS`

`int` · optional · default `600`

Lifetime of each presigned artifact GET URL; defaults to ten minutes.

### `DAIMON_ARTIFACTS__EMBED_IMAGES`

`bool` · optional · default `True`

Also return bounded MCP image blocks for model vision. Disable independently when a
hosted client cannot accept image content.

## Scheduler

Read from `daimon.adapters.scheduler.settings.SchedulerSettings`. Prefix
`DAIMON_SCHEDULER__`.

### `DAIMON_SCHEDULER__TICK_INTERVAL_S`

`float` · optional · default `30.0`

Seconds between scheduler ticks (loop sleep).

### `DAIMON_SCHEDULER__MAX_AGE_S`

`float` · optional · default `900.0`

Freshness window — rows whose next_fire_at slipped past now - max_age_s are advanced via
advance_stale and not fired.

### `DAIMON_SCHEDULER__MAX_CONCURRENT_FIRES`

`int` · optional · default `10`

Global cap on simultaneously-dispatched routine fires within one tick. Conservative
against the shared Anthropic key's rate limit; per-tenant caps are enforced separately
by the adapters.

### `DAIMON_SCHEDULER__DISPATCH_TIMEOUT_S`

`float` · optional · default `3000.0`

An OUTER PROCESS GUARD (asyncio.wait_for), not the turn's deadline. The core ~45-minute
ceiling (daimon.core.turn.ceiling) is enforced inside headless_runner.run_turn and fires
first, producing a TurnError(kind='ceiling'). This bound only catches a fire that hangs
OUTSIDE the ceiling's two legs (routine row bookkeeping, agent/environment resolution,
the usage-recorder factory, record_result), and must stay strictly above TURN_CEILING_S
or the core ceiling becomes unreachable for routines. run_one_tick awaits the full
gather, so a fire running to this bound blocks the tick loop for that long, and cron
slots that slip more than max_age_s (default 900s) behind during that window are
advanced by advance_stale rather than fired.

### `DAIMON_SCHEDULER__ADVISORY_LOCK_KEY`

`int` · optional · default `4918292864457134915`

Postgres pg_try_advisory_lock int64 key. Default is the ASCII encoding of 'DAIMONSC'.
Two scheduler processes share the key; the second logs that it did not get the lock and
exits non-zero.

### `DAIMON_SCHEDULER__HEALTH_PORT`

`int` · optional · default `8082`

Port for the stdlib liveness responder (used by the platform health check). Must not
collide with mcp's 8080 or discord's 8081 on the shared host.

## Notebook host (standalone service)

Read from `notebook_host.config.Settings`. Prefix `DAIMON_NOTEBOOK__`.

A standalone service in `apps/notebook-host`, deployed and configured separately from
the daimon processes. It is not part of `docker-compose.yml`.

This service shares the `DAIMON_NOTEBOOK__` prefix with a block on daimon's own
Settings, so `DAIMON_NOTEBOOK__ADMIN_SECRET`, `DAIMON_NOTEBOOK__MAX_SOURCE_BYTES` appear
twice on this page — once for the service and once for the daimon side that calls it.
They are read by different processes; a single shared env file would set both.

No field in this model carries a `Field(description=...)`, so this section lists types
and defaults only. `apps/notebook-host/src/notebook_host/config.py` documents them in
inline comments.

### `DAIMON_NOTEBOOK__DATA_DIR`

`Path` · optional · default `/data/notebooks`

### `DAIMON_NOTEBOOK__ADMIN_SECRETS`

`list[SecretStr]` · optional · default unset · secret

### `DAIMON_NOTEBOOK__ADMIN_SECRET`

`SecretStr | None` · optional · default unset · secret

### `DAIMON_NOTEBOOK__HOST_PORT`

`int` · optional · default `8001`

### `DAIMON_NOTEBOOK__MARIMO_PORT_START`

`int` · optional · default `8100`

### `DAIMON_NOTEBOOK__MARIMO_PORT_END`

`int` · optional · default `8160`

### `DAIMON_NOTEBOOK__SUBPROCESS_TTL_SECONDS`

`int` · optional · default `86400`

### `DAIMON_NOTEBOOK__SWEEP_INTERVAL_SECONDS`

`int` · optional · default `300`

### `DAIMON_NOTEBOOK__SPAWN_TIMEOUT_SECONDS`

`float` · optional · default `20.0`

### `DAIMON_NOTEBOOK__VALIDATE_ON_PUBLISH`

`bool` · optional · default `True`

### `DAIMON_NOTEBOOK__VALIDATION_TIMEOUT_SECONDS`

`float` · optional · default `60.0`

### `DAIMON_NOTEBOOK__PUBLIC_HOST`

`str` · optional · default `localhost`

### `DAIMON_NOTEBOOK__PUBLIC_URL_BASE`

`str | None` · optional · default unset

### `DAIMON_NOTEBOOK__MAX_SOURCE_BYTES`

`int` · optional · default `1048576`

### `DAIMON_NOTEBOOK__MAX_ATTACHMENT_BYTES_CEILING`

`int` · optional · default `104857600`

### `DAIMON_NOTEBOOK__MARIMO_RLIMIT_AS_BYTES`

`int` · optional · default `4294967296`

### `DAIMON_NOTEBOOK__MARIMO_RLIMIT_CPU_SECONDS`

`int` · optional · default `3600`

### `DAIMON_NOTEBOOK__JAIL_UID_START`

`int` · optional · default `100000`

### `DAIMON_NOTEBOOK__JAIL_UID_END`

`int` · optional · default `100999`

### `DAIMON_NOTEBOOK__ALLOW_UNJAILED_SPAWN`

`bool` · optional · default `False`

### `DAIMON_NOTEBOOK__UIDS_FILE`

`Path | None` · optional · default unset

### `DAIMON_NOTEBOOK__PIDS_FILE`

`Path | None` · optional · default unset

### `DAIMON_NOTEBOOK__BLOGS_FILE`

`Path | None` · optional · default unset

### `DAIMON_NOTEBOOK__CONSUMED_FILE`

`Path | None` · optional · default unset

### `DAIMON_NOTEBOOK__ALLOWED_ORIGINS`

`list[str]` · optional · default unset

## Report host (standalone service)

Read from `report_host.config.Settings`. Prefix `DAIMON_REPORT__`.

A standalone service in `apps/report-host`, deployed and configured separately from the
daimon processes. It is not part of `docker-compose.yml`.

### `DAIMON_REPORT__DATA_DIR`

`Path` · optional · default `/data/reports`

Root of the host's persistent volume: SQLite stores, bundles, PDFs.

### `DAIMON_REPORT__ADMIN_SECRETS`

`list[SecretStr]` · optional · default unset · secret

CSV of bearer tokens accepted on admin routes
(DAIMON_REPORT__ADMIN_SECRETS=primary,backup). At least one is required — the host
refuses to start without one.

### `DAIMON_REPORT__MCP_URL`

`HttpUrl` · **required**

The seam endpoint this host calls to run turns, poll cost, and mint tokens.

### `DAIMON_REPORT__PUBLIC_URL_BASE`

`HttpUrl` · **required**

External URL prefix used to build recipient links and the per-turn upload URL handed to
the agent.

### `DAIMON_REPORT__HOST_PORT`

`int` · optional · default `8002`

Port the host's uvicorn server binds.

### `DAIMON_REPORT__RESERVE_USD`

`Decimal` · optional · default `0.60`

Amount held against a report's spend cap the moment a turn starts, released and replaced
by the real cost once the turn settles. Measured reader turns ran roughly sixteen to
sixty-three cents; a revision-with-rebuild came in under this reserve.

### `DAIMON_REPORT__POLL_INTERVAL_SECONDS`

`float` · optional · default `2.0`

How often the host polls the seam for turn progress.

### `DAIMON_REPORT__TURN_TIMEOUT_SECONDS`

`int` · optional · default `1200`

A turn still running past this many seconds is cancelled by the host.

### `DAIMON_REPORT__MAX_PDF_BYTES`

`int` · optional · default `52428800`

Hard ceiling on an uploaded revised-PDF body size.

### `DAIMON_REPORT__MAX_BUNDLE_BYTES`

`int` · optional · default `26214400`

Hard ceiling on a published report bundle. Mirrors the seam's own bundle cap and is
enforced independently here as a second layer of defense.

### `DAIMON_REPORT__MAX_OPEN_THREADS_PER_RECIPIENT`

`int` · optional · default `3`

Cap on concurrently open threads a single recipient may hold.

### `DAIMON_REPORT__MAX_RUNNING_TURNS_PER_REPORT`

`int` · optional · default `4`

Cap on concurrently running turns across one report's threads.

### `DAIMON_REPORT__RECIPIENT_LINK_TTL_DAYS`

`int` · optional · default `90`

How long a per-recipient link remains valid before expiring.

### `DAIMON_REPORT__THREAD_IDLE_ARCHIVE_HOURS`

`int` · optional · default `24`

A thread idle longer than this is archived by the host's sweep.

## Billing (Stripe)

Read from the process environment by `daimon.core.billing.load_billing_config`, not from
a settings model — these carry no `DAIMON_` prefix. All seven are required together:
with any one unset, billing is disabled rather than rejected, and the top-up flow cannot
create a checkout session.

### `STRIPE_SECRET_KEY`

`str` · required for billing · secret

Stripe API secret key used for checkout sessions.

### `STRIPE_WEBHOOK_SECRET`

`str` · required for billing · secret

Signing secret for the Stripe webhook endpoint.

### `STRIPE_PRICE_10_USD`

`str` · required for billing

Stripe price id for the $10 top-up.

### `STRIPE_PRICE_25_USD`

`str` · required for billing

Stripe price id for the $25 top-up.

### `STRIPE_PRICE_50_USD`

`str` · required for billing

Stripe price id for the $50 top-up.

### `STRIPE_PRICE_100_USD`

`str` · required for billing

Stripe price id for the $100 top-up.

### `MCP_PUBLIC_URL`

`str` · required for billing

Origin the checkout return URLs are built from — billing appends `/billing/success` and
`/billing/cancel` to it. Separate from `DAIMON_MCP__PUBLIC_URL`, which billing does not
read.

## Docker Compose

Interpolated by `docker-compose.yml` itself; no daimon process reads them. They exist so
the compose file can build `DAIMON_DATABASE__URL` for every service from one password.

### `POSTGRES_USER`

`str` · optional · default `daimon`

Postgres superuser the `postgres` service is created with.

### `POSTGRES_PASSWORD`

`str` · **required**

Its password. Required — every service interpolates it into `DAIMON_DATABASE__URL`
behind a fail-fast `${VAR:?}` guard. Keep it URL-safe (avoid `@ : / % #`): it is
substituted raw into the asyncpg DSN.

### `POSTGRES_DB`

`str` · optional · default `daimon`

Database created on first boot.

### `POSTGRES_PORT`

`str` · optional · default `5432`

Host port the container's 5432 is published on, on 127.0.0.1.
