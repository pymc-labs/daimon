"""Nested `pydantic-settings` for daimon-core. Constructed via `load_settings()`.

Never import a module-level settings singleton — callers construct once at the
edge (CLI entrypoint, test fixture) and inject downstream.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, cast
from uuid import UUID

from daimon.core.thread_participation import ParticipationMode
from daimon.core.tool_safety import ToolSafetyPolicy
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class DatabaseSettings(BaseModel):
    url: PostgresDsn = Field(
        description=(
            "Postgres connection string used by the running application "
            "(SQLAlchemy async + asyncpg). Required."
        ),
    )
    test_url: PostgresDsn | None = Field(
        default=None,
        description=(
            "Postgres connection string for the test suite. Points at a "
            "dedicated database (e.g. daimon_test) so test runs never touch "
            "development data. Unset in production."
        ),
    )
    pool_size: int = Field(
        default=5,
        ge=1,
        description=(
            "Persistent Postgres connections per process. Default 5; size against all "
            "application processes and the database connection limit."
        ),
    )
    max_overflow: int = Field(
        default=10,
        ge=0,
        description=(
            "Temporary Postgres connections above pool_size per process. Default 10; "
            "include these in the database connection budget. pool_size + max_overflow "
            "must be at least 4; smaller pools are rejected at engine startup to reserve "
            "independent preparation and mutation capacity with nested query headroom. "
            "Fence contenders release connections and permits between nonblocking "
            "attempts, retrying with 25-75 ms jitter for at most 5 seconds before "
            "retryable session busy."
        ),
    )
    pool_timeout: float = Field(
        default=30.0,
        gt=0,
        description=(
            "Seconds to wait for a free Postgres connection before failing. Default 30 seconds."
        ),
    )
    preparation_concurrency: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Maximum concurrent session preparations holding a Postgres advisory-lock "
            "transaction per process. Unset uses half of pool_size (at least one), "
            "leaving connections for preparation's nested queries and turn outcomes. "
            "Keep this below pool_size so nested queries have headroom. Excess "
            "preparations wait in memory instead of timing out in the DB pool."
        ),
    )


class AnthropicSettings(BaseModel):
    api_key: SecretStr = Field(
        description=(
            "Anthropic API key used to authenticate all Managed Agents SDK calls. Required."
        ),
    )
    base_url: HttpUrl = Field(
        default=HttpUrl("https://api.anthropic.com"),
        description=(
            "Base URL for the Anthropic API. Override only when routing through "
            "a proxy or a non-default API endpoint."
        ),
    )
    skills_requests_per_minute: int = Field(
        default=80,
        ge=1,
        description=(
            "Maximum Anthropic Skills API requests per minute in this process. "
            "Default 80 leaves headroom below the 100 requests/minute organization limit. "
            "Other deployments in the same organization share that limit."
        ),
    )


class CLISettings(BaseModel):
    local_user: str = Field(
        default_factory=lambda: os.environ.get("USER", "daimon"),
        description=(
            "Display name used to identify the local operator running the CLI. "
            "Defaults to the $USER environment variable, falling back to "
            "'daimon' when unset."
        ),
    )


class LogSettings(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Minimum log level emitted by the structured logger.",
    )


class ObservabilitySettings(BaseModel):
    health_interval_s: float = Field(
        default=30,
        ge=0,
        description=(
            "Seconds between runtime.health structured log lines from each long-running process. "
            "Default 30; set DAIMON_OBSERVABILITY__HEALTH_INTERVAL_S=0 to disable."
        ),
    )


class OpsSettings(BaseModel):
    alert_webhook_url: SecretStr | None = Field(
        default=None,
        description=(
            "Discord webhook URL for short operator alerts about new installs, Stripe top-ups, "
            "and Anthropic limits. Unset disables alerts. Keep the URL secret."
        ),
    )


class McpSettings(BaseModel):
    """MCP adapter config.

    Both fields are optional so deployments that don't run the MCP adapter
    keep working. The `create_mcp_app` factory re-validates presence at
    server-boot time (raises `BootstrapError` on miss); `ensure_mcp_vault` at
    session-create time skips silently when `public_url is None`.
    """

    jwt_secret: SecretStr | None = Field(
        default=None,
        description=(
            "Secret used to sign and verify MCP bearer tokens. Required to run the MCP adapter."
        ),
    )
    public_url: HttpUrl | None = Field(
        default=None,
        description=(
            "Externally reachable base URL of the MCP server (the streamable "
            "endpoint, e.g. https://mcp.example.com/mcp). Required to run the "
            "MCP adapter — used to build OAuth/CLI/health route URLs and "
            "session-create metadata."
        ),
    )
    file_store_dir: Path | None = Field(
        default=None,
        description=(
            "On-disk directory for the media-tool FileStore. When unset, the "
            "server uses tempfile.gettempdir() / 'daimon-mcp-files' resolved "
            "at startup."
        ),
    )
    bundle_max_bytes: int = Field(
        default=25 * 1024 * 1024,
        description=(
            "Per-upload byte cap enforced by the bundle upload route. The "
            "default keeps headroom under a 32 MiB proxy request limit; raise "
            "it if the front end in front of the mcp service allows larger "
            "bodies."
        ),
    )
    bundle_uploads_per_hour: int = Field(
        default=20,
        description=(
            "Per-token cap on bundle upload route calls per rolling hour. "
            "Prevents a compromised or buggy caller from exhausting the "
            "Files API upload path. Set to 0 to disable (not recommended in "
            "production)."
        ),
    )
    bundle_ttl_days: int = Field(
        default=90,
        description=(
            "How long an uploaded bundle object is retained on the Files API "
            "before deletion. Deletion is performed by the scheduler's "
            "pending-file sweeper, not by this process directly — a "
            "deployment running no scheduler will never reclaim these "
            "objects."
        ),
    )
    operator_calls_per_minute: int = Field(
        default=60,
        description=(
            "Per-token cap on tool calls an operator token (minted with "
            "`daimon mcp mint-operator-token`) may make per rolling minute, "
            "counted in each MCP process. Set to 0 to disable."
        ),
    )

    @property
    def app_root_url(self) -> str | None:
        """Base URL for the app-root routes (``/oauth/*``, ``/cli/*``,
        ``/healthz``), derived from ``public_url`` by dropping the trailing
        ``/mcp`` protocol segment.

        ``public_url`` points at the MCP streamable endpoint (``…/mcp``), but
        the OAuth/CLI/health routes are ``add_route``'d at the app root next to
        it — so clients building those URLs must not carry the ``/mcp`` suffix.
        Returns ``None`` when ``public_url`` is unset (no fabricated base)."""
        if self.public_url is None:
            return None
        return str(self.public_url).rstrip("/").removesuffix("/mcp").rstrip("/")


_HUB_SIGNING_KEY_BYTES = 32


class HubSettings(BaseModel):
    """OAuth-login MCP mounts for coding-agent clients.

    A platform's mount appears only when both its client id and secret are
    set; a deployment that sets neither runs exactly as before. The signing
    key is shared by both mounts and must be a base64url 32-byte key, the same
    shape as a Fernet key, so the proxy uses it directly rather than deriving
    one from the client secret (rotating a client secret would otherwise
    invalidate every issued login).
    """

    slack_client_id: str | None = Field(
        default=None,
        description=(
            "Slack OAuth app client ID for the /slack/mcp login mount. Register "
            "<DAIMON_MCP__PUBLIC_URL origin>/slack/auth/callback as a redirect "
            "URL on the Slack app."
        ),
    )
    slack_client_secret: SecretStr | None = Field(
        default=None,
        description="Slack OAuth app client secret for the /slack/mcp login mount.",
    )
    discord_client_id: str | None = Field(
        default=None,
        description=(
            "Discord OAuth app client ID for the /discord/mcp login mount. "
            "Register <DAIMON_MCP__PUBLIC_URL origin>/discord/auth/callback as a "
            "redirect URI on the Discord app."
        ),
    )
    discord_client_secret: SecretStr | None = Field(
        default=None,
        description="Discord OAuth app client secret for the /discord/mcp login mount.",
    )
    allowed_client_redirect_uris: list[str] = Field(
        default=[
            "http://localhost:*",
            "http://127.0.0.1:*",
            "https://claude.ai/*",
            "https://claude.com/*",
        ],
        description=(
            "Redirect URI patterns an MCP client may register with the hub login "
            "mounts (wildcards allowed). Defaults cover coding agents on loopback "
            "and claude.ai; widen only for a client you operate."
        ),
    )
    jwt_signing_key: SecretStr | None = Field(
        default=None,
        description=(
            "Base64url 32-byte key that signs hub login tokens. Required when "
            "any hub mount is configured, and rejected at boot unless it "
            "decodes to exactly 32 bytes. Generate with Fernet.generate_key()."
        ),
    )

    @field_validator("jwt_signing_key")
    @classmethod
    def _check_signing_key(cls, value: SecretStr | None) -> SecretStr | None:
        """Reject anything but a 32-byte base64url key.

        FastMCP derives an HS256 secret and warns about short keys only for a
        ``str`` secret; the proxy is handed ``bytes``, which it uses raw. A
        passphrase would therefore be accepted silently as the signing secret.
        """
        if value is None:
            return value
        try:
            decoded = base64.urlsafe_b64decode(value.get_secret_value())
        except (binascii.Error, ValueError):
            decoded = b""
        if len(decoded) != _HUB_SIGNING_KEY_BYTES:
            raise ValueError(
                "DAIMON_HUB__JWT_SIGNING_KEY must be a base64url-encoded "
                f"{_HUB_SIGNING_KEY_BYTES}-byte key; generate one with "
                "python -c 'from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())'"
            )
        return value

    @property
    def slack_configured(self) -> bool:
        return self.slack_client_id is not None and self.slack_client_secret is not None

    @property
    def discord_configured(self) -> bool:
        return self.discord_client_id is not None and self.discord_client_secret is not None


class ThreadParticipationSettings(BaseModel):
    """Organic thread participation: replying in a thread unprompted.

    Platform-agnostic settings (the store and tool are keyed by platform);
    the Discord and Teams adapters read them. `mode` is the deployment tier
    of a cascade (deployment, workspace, channel, thread) that the agent's
    `set_thread_participation` tool writes the other tiers of. `off` (the
    default) changes nothing for anyone: no classifier runs and every server
    behaves as today until someone asks the agent to follow a thread, or an
    admin turns a channel or the workspace on. `disabled` also refuses those
    requests. `on` follows every thread unless a lower tier says otherwise.
    """

    mode: ParticipationMode = Field(
        default=ParticipationMode.OFF,
        description=(
            "Deployment default for replying in threads unprompted. off: mention-only "
            "until a thread, channel or workspace is turned on. disabled: mention-only "
            "and cannot be turned on. on: follow every thread unless turned off below."
        ),
    )
    quiet_seconds: float = Field(
        default=8.0,
        gt=0,
        description=(
            "How long a followed thread must be quiet after an unprompted message before "
            "the agent decides whether to reply. A burst of messages is judged once, at "
            "the end."
        ),
    )
    max_per_hour: int = Field(
        default=20,
        ge=1,
        description="Backstop: maximum unprompted replies per thread per rolling hour.",
    )
    recent_messages_window: int = Field(
        default=10,
        ge=1,
        le=50,
        description="How many earlier thread messages the classifier sees before deciding.",
    )
    classifier_model: str = Field(
        default="claude-haiku-4-5",
        description=(
            "Model that decides whether a burst of unprompted messages deserves a reply. "
            "One short call per quiet burst; a small, fast model is the point."
        ),
    )


class DiscordSettings(BaseModel):
    """Discord adapter config.

    Optional so non-Discord deployments keep working. The ``__main__.py``
    entrypoint validates presence at boot time.
    """

    bot_token: SecretStr = Field(
        description="Discord bot token. Required to run the Discord adapter.",
    )
    thread_open_notice_after_s: float = Field(
        default=3.0,
        ge=0,
        description=(
            "Seconds after an admitted opening mention before replying in the parent channel "
            "that its Discord thread is still opening. Includes thread naming and Discord "
            "rate-limit waits. Set to 0 to reply immediately. The notice is edited with a "
            "thread link or retry guidance when creation finishes."
        ),
    )
    max_concurrent_turns_per_tenant: int = Field(
        default=3,
        description=(
            "Maximum number of agent turns a single tenant (Discord guild) may "
            "have in flight at once. Caps one noisy guild from starving others "
            "on the shared Anthropic key."
        ),
    )
    max_concurrent_turns: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Maximum Discord agent turns running across all guilds and DMs in this process. "
            "Unset leaves deployment-wide admission unlimited. Excess turns are refused "
            "with a retry notice; continuation wakes keep their existing admission path."
        ),
    )
    health_port: int = Field(
        default=8081,
        description=(
            "Port for the Discord process's liveness endpoint. Must not "
            "collide with the mcp process (8080) or the scheduler process "
            "(8082) — all process groups share one host."
        ),
    )
    qa_bot_user_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Discord user ids of automated QA bots allowed to start turns by "
            "mention. Bot-authored mentions are rejected by default; these are "
            "allow-listed so a harness can drive the mention path end-to-end "
            "without a human. Several are supported because admin-gated tools "
            "need a caller holding Manage Server while the refusal paths need "
            "one without it. Leave empty outside test deployments -- an "
            "allow-listed bot spends real credit. As a tuple field this must "
            'be set as a JSON array, e.g. \'["123","456"]\'. daimon\'s own id '
            "is refused at the gate even if listed."
        ),
    )
    bot_display_name: str = Field(
        default="daimon",
        # Constrained because the value flows into user-facing surfaces with
        # hard limits: slash-command descriptions carry fixed copy around the
        # name and must stay under Discord's 100-char cap (hence 32), and the
        # excluded characters are regex-replacement / Discord-markdown /
        # mention metacharacters that would corrupt those render sites.
        min_length=1,
        max_length=32,
        pattern=r"^[^\\`@#:]+$",
        description=(
            "The bot's presented name across user-facing Discord render sites "
            "(@mentions, setup messages, help text, privacy panel copy). Defaults "
            "to 'daimon' so unset deployments render byte-identical output. Set "
            "to a distinct name (e.g. 'daimon-staging') so a non-production "
            "deployment is visibly distinct in-channel."
        ),
    )


class SlackSettings(BaseModel):
    """Slack adapter config.

    Optional so non-Slack deployments boot unchanged — the block is ``None``
    when no ``DAIMON_SLACK__*`` env vars are present. Mirrors ``DiscordSettings``.

    The OAuth/install flow reads these fields: ``client_id`` / ``client_secret``
    for the code-exchange, ``signing_secret`` for request verification,
    ``app_token`` for Socket Mode.
    """

    bot_display_name: str = Field(
        default="daimon",
        # Keep surrounding Block Kit copy within Slack's limits and exclude
        # mention, markdown, and emoji-shortcode metacharacters.
        min_length=1,
        max_length=32,
        pattern=r"^[^\\`@#:]+$",
        description="The bot's presented name in Slack setup, help, and privacy messages.",
    )
    signing_secret: SecretStr = Field(
        description=(
            "Slack request-signing secret used to verify inbound HTTP "
            "requests. Required — keeps the whole Slack block None when no "
            "DAIMON_SLACK__* vars are set."
        ),
    )
    app_token: SecretStr = Field(
        description="Slack app-level token (xapp-...) used to open the Socket Mode connection.",
    )
    client_id: str | None = Field(
        default=None,
        description="Slack OAuth app client ID, used during the 'Add to Slack' install flow.",
    )
    client_secret: SecretStr | None = Field(
        default=None,
        description="Slack OAuth app client secret, used during the 'Add to Slack' install flow.",
    )
    max_concurrent_turns_per_tenant: int = Field(
        default=3,
        description=(
            "Maximum number of agent turns a single tenant (Slack workspace) "
            "may have in flight at once. Caps one noisy workspace from "
            "starving others on the shared Anthropic key."
        ),
    )
    history_page_limit: int = Field(
        default=100,
        ge=1,
        le=1000,
        description=(
            "Messages requested per conversations.replies call when replaying "
            "thread history. Slack clamps this per workspace: an app "
            "commercially distributed outside the Marketplace gets 15 whatever "
            "it asks for, an internal-app install gets the full page up to "
            "1000, which is also the largest value Slack accepts. The default "
            "matches the 100 messages Discord replays; every replayed message "
            "is first-turn context the model pays for."
        ),
    )
    health_port: int = Field(
        default=8083,
        description=(
            "Port for the Slack process's liveness endpoint. Must not collide "
            "with the mcp process (8080), the discord process (8081), or the "
            "scheduler process (8082) — all process groups share one host."
        ),
    )


class TeamsSettings(BaseModel):
    """Microsoft Teams adapter config.

    Optional so non-Teams deployments boot unchanged — the block is ``None``
    when no ``DAIMON_TEAMS__*`` env vars are present. Mirrors ``SlackSettings``.

    ``client_id`` / ``client_secret`` / ``tenant_id`` are the Entra app
    registration the Bot Framework posts activities to; ``port`` is the HTTP
    ingress the SDK's FastAPI adapter binds (``/api/messages`` plus the
    ``/healthz`` / ``/readyz`` endpoints served by the same listener);
    ``enabled`` gates ``/api/messages`` without taking the process down.
    """

    client_id: str = Field(
        description=(
            "Entra (Azure AD) app registration client ID the Teams bot "
            "authenticates as — also the audience inbound Bot Framework JWTs "
            "are validated against."
        ),
    )
    client_secret: SecretStr = Field(
        description=(
            "Entra app registration client secret, used to mint Bot Framework "
            "tokens for outbound sends."
        ),
    )
    tenant_id: str = Field(
        description=(
            "Entra tenant ID the app registration lives in. A single-tenant "
            "bot only answers activities whose conversation and channel-data "
            "tenant both equal this value."
        ),
    )
    max_concurrent_turns_per_tenant: int = Field(
        default=3,
        description=(
            "Maximum number of agent turns a single Teams tenant may have "
            "in flight at once. Caps one noisy tenant from starving others "
            "on the shared Anthropic key."
        ),
    )
    port: int = Field(
        default=3978,
        description=(
            "Port for the Teams process's HTTP ingress and health endpoints "
            "(/api/messages, /healthz, /readyz). The Bot Framework messaging "
            "endpoint must be configured to reach this listener."
        ),
    )
    enabled: bool = Field(
        default=True,
        description=(
            "When False, /api/messages answers 503 while the health endpoints "
            "stay live — the process keeps running so ingress can be "
            "re-enabled without a redeploy."
        ),
    )
    public_url: HttpUrl | None = Field(
        default=None,
        description=(
            "Externally reachable base URL of the Teams service (the Bot "
            "Framework messaging endpoint without /api/messages). Enables the "
            "admin sign-in that grants daimon a team's SharePoint site; its "
            "callback is <public_url>/oauth/teams/files/callback, which must be "
            "a Web redirect URI on the app registration."
        ),
    )
    admin_user_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Entra object IDs of the people who administer this deployment "
            "from Teams. Teams exposes no admin role to bots, so this list is "
            "the admin check: admins get the admin role in turns, create "
            "routines, replace shared keys, top up and see everyone's usage. "
            "Everyone else is a regular user."
        ),
    )
    restrict_guests: bool = Field(
        default=True,
        description=(
            "When True, guests (Entra B2B guest accounts in this tenant) are "
            "treated as people from another organisation: answered only in a "
            "channel kept to its own agents, with a few conversation tools and no commands or "
            "admin role. The tenant access policy's member guest list exempts "
            "some. When False, guests are treated as team members."
        ),
    )
    restrict_external_participants: bool = Field(
        default=True,
        description=(
            "When True, a shared channel's external participants (people from "
            "another tenant, via B2B direct connect) are answered only in a "
            "channel kept to its own agents, with a few conversation tools and no commands or "
            "admin role. When False, they are treated as team members."
        ),
    )

    @field_validator("admin_user_ids")
    @classmethod
    def _canonicalize_admin_user_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Compare against the canonical lowercase form inbound ids arrive in."""
        try:
            return tuple(str(UUID(item)) for item in value)
        except ValueError:
            raise ValueError(
                "DAIMON_TEAMS__ADMIN_USER_IDS must list Entra object ID UUIDs"
            ) from None

    @field_validator("tenant_id")
    @classmethod
    def _canonicalize_tenant_id(cls, value: str) -> str:
        """Normalize to the canonical lowercase UUID form and reject non-UUIDs.

        The resolver canonicalizes activity tenant ids and compares them to
        this value, and ``provision_tenant`` hashes ``workspace_id`` into the
        deterministic tenant uuid — an uppercase portal paste must not yield
        a provisioned row the resolver cannot match.
        """
        try:
            return str(UUID(value))
        except ValueError:
            raise ValueError(
                "DAIMON_TEAMS__TENANT_ID must be the Entra directory tenant's UUID"
            ) from None


class GithubSettings(BaseModel):
    """GitHub repository access config (App-or-PAT; no OAuth flow). All fields are
    optional so deployments without GitHub App or PAT config keep working.

    Cloning via the GitHub App requires only app_id + app_private_key.
    app_slug is needed only to construct the App's install link; a
    deployment that never surfaces that link can leave it unset.
    webhook_secret is optional and required only for the skill-sync
    push-driven resync webhook.
    """

    oauth_scopes: tuple[str, ...] = Field(
        default=(
            "repo",
            "read:user",
            "read:org",
            "workflow",
        ),
        description=(
            "OAuth scopes requested when a GitHub token-broker flow is used. "
            "Not consulted for GitHub App or PAT authentication."
        ),
    )
    app_id: str | None = Field(
        default=None,
        description="GitHub App ID used to mint short-lived installation tokens for repo cloning.",
    )
    app_private_key: SecretStr | None = Field(
        default=None,
        description="Private key (PEM) for the GitHub App identified by app_id.",
    )
    app_slug: str | None = Field(
        default=None,
        description=(
            "GitHub App's URL name, used to build the App's install link so "
            "members can be pointed at it from chat and from the setup "
            "panel. Cloning does not need it, so a deployment that never "
            "surfaces the install link can leave it unset."
        ),
    )
    webhook_secret: SecretStr | None = Field(
        default=None,
        description=(
            "Secret used to verify GitHub webhook payload signatures. Required "
            "only to enable the skill-sync push-driven resync webhook."
        ),
    )
    fallback_pat: SecretStr | None = Field(
        default=None,
        description=(
            "Operator-wide personal access token used as the clone credential "
            "for public-repo bindings that have no per-agent credential. "
            "Needs no scopes — any valid token clones a public repo."
        ),
    )
    max_tarball_bytes: int = Field(
        default=50 * 1024 * 1024,
        description=(
            "Raw (compressed) size cap enforced while streaming a GitHub "
            "tarball download, checked against Content-Length when present "
            "and against the cumulative streamed byte count regardless. 50 "
            "MiB is the operator default. Set to 0 to disable (not "
            "recommended in production)."
        ),
    )
    max_tarball_decompressed_bytes: int = Field(
        default=200 * 1024 * 1024,
        description=(
            "Decompressed (extracted) size cap enforced against the sum of "
            "tar member sizes before extraction, guarding against zip bombs. "
            "200 MiB is the operator default. Set to 0 to disable (not "
            "recommended in production)."
        ),
    )

    @field_validator("app_private_key", mode="before")
    @classmethod
    def _decode_base64_private_key(cls, value: object) -> object:
        """Accept the RSA private key as raw PEM or base64-encoded PEM.

        Multi-line PEM survives env delivery on Fly and Cloud Run, but the GCP
        worker VM loads secrets through docker-compose ``env_file``, whose
        format cannot represent multi-line values. base64 is single-line (its
        alphabet has no dashes), so a ``-----BEGIN`` check distinguishes raw
        PEM from a base64-encoded PEM. A value that is neither is passed through
        untouched to fail loudly downstream rather than be silently mangled.
        """
        if value is None:
            return value
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str) or "-----BEGIN" in raw:
            return value
        try:
            decoded = base64.b64decode(raw, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return value
        return decoded if "-----BEGIN" in decoded else value

    @field_validator("app_slug")
    @classmethod
    def _validate_app_slug(cls, value: str | None) -> str | None:
        """Reject anything outside a GitHub App slug's real shape.

        The slug is interpolated into a URL that is shown to users and
        clicked, so a typo or a pasted URL fragment must fail at settings
        load rather than produce a link that goes somewhere unintended.
        This is the mitigation for this deployment's phishing-shaped
        threat: the URL is built from configuration only, and the
        configuration is validated here.
        """
        if value is None:
            return value
        if (
            not value
            or not all(char.isascii() and (char.isalnum() or char == "-") for char in value)
            or value.startswith("-")
            or value.endswith("-")
            or len(value) > 100
        ):
            shown = value if len(value) <= 40 else f"{value[:40]}...(truncated)"
            raise ValueError(
                f"app_slug must be a non-empty string of ASCII letters, digits and "
                f"hyphens, with no leading or trailing hyphen and at most 100 "
                f"characters; got shape of: {shown!r}"
            )
        return value


class CryptoSettings(BaseModel):
    """MultiFernet keys for at-rest token encryption.

    A single deployment ships one key; rotation means prepending a new key.
    Each key must be a Fernet.generate_key()-style base64-urlsafe 32-byte
    string. A deployment without keys still boots, but refuses to save agent
    keys unless `allow_plaintext` opts into plaintext storage for local
    development.
    """

    keys: Annotated[tuple[SecretStr, ...], NoDecode] = Field(
        default=(),
        description=(
            "Ordered Fernet keys used to encrypt/decrypt stored credentials: a "
            "single key, a comma-separated list, or a JSON list. Required to save "
            "agent keys: without keys, saving an agent environment value is "
            "refused unless `allow_plaintext` is set. The first key encrypts new "
            "values; older keys remain valid for decrypting existing ciphertext "
            "during rotation. Run `daimon crypto verify` to confirm no plaintext "
            "rows remain."
        ),
    )

    @field_validator("keys", mode="before")
    @classmethod
    def _split_keys(cls, value: object) -> object:
        """Accept a bare key or a comma-separated list as well as a JSON list.

        The environment hands this field a raw string. Fernet keys are
        base64-urlsafe, so they never contain a comma, a quote or a bracket.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return ()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                # Fixed message: the decoder's error quotes the input, i.e. key text.
                raise ValueError(
                    "DAIMON_CRYPTO__KEYS starts with '[' but is not a valid JSON list"
                ) from None
            if not isinstance(parsed, list):
                raise ValueError(
                    "DAIMON_CRYPTO__KEYS must be a key, a comma-separated list or a JSON list"
                )
            items = [str(item).strip() for item in cast("list[object]", parsed)]
            return tuple(item for item in items if item)
        return tuple(part.strip() for part in text.split(",") if part.strip())

    allow_plaintext: bool = Field(
        default=False,
        description=(
            "Store agent environment values (agent keys) in plaintext when no "
            "`keys` are configured. For local development only: with this off "
            "and no keys, every agent key write is refused."
        ),
    )


class CredentialsSettings(BaseModel):
    """Tenant-level credentials for the token broker.

    Optional — the Google Workspace provider raises a config error when
    `google_sa_json` is unset.
    """

    google_sa_json: SecretStr | None = Field(
        default=None,
        description=(
            "Full JSON contents of a Google service-account key, used by the "
            "token broker to mint delegated Google Workspace credentials."
        ),
    )


class GeminiSettings(BaseModel):
    """Optional Gemini API credentials for media MCP tools.

    When unset, the media tools skip registration so the ``mcp`` process
    boots without them.
    """

    api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Gemini API key used by media-generation MCP tools. Tools are unregistered when unset."
        ),
    )


class NotebookSettings(BaseModel):
    """Optional notebook-host client config.

    Both fields optional so deployments without a notebook host keep
    working. The MCP tool raises ToolError when host_url is unset.
    """

    host_url: HttpUrl | None = Field(
        default=None,
        description="Base URL of the notebook-host service (e.g. http://notebook-host:8001).",
    )
    admin_secret: SecretStr | None = Field(
        default=None,
        description="Bearer secret used to authenticate admin calls to the notebook-host service.",
    )
    publish_rate_per_hour: int = Field(
        default=30,
        description=(
            "Per-principal cap on publish_notebook calls per rolling hour. "
            "Prevents a compromised or buggy agent from exhausting the "
            "notebook host's process/port pool. Set to 0 to disable (not "
            "recommended in production)."
        ),
    )
    max_attachment_bytes: int = Field(
        default=10 * 1024 * 1024,
        description=(
            "Per-attachment size cap enforced by attach_notebook_data before "
            "uploading to the notebook host. 10 MiB is the operator default. "
            "Set to 0 to disable (the host's own ceiling still applies as "
            "defense-in-depth)."
        ),
    )
    max_source_bytes: int = Field(
        default=1_048_576,
        description=(
            "Per-upload byte budget signed into notebook upload tokens. 1 "
            "MiB mirrors the notebook host's own ceiling, which is enforced "
            "independently as a second layer of defense."
        ),
    )
    allow_editable: bool = Field(
        default=False,
        description=(
            "Let `create_notebook_upload_url(editable=True)` publish the marimo "
            "code editor. Anyone holding an editor link can run arbitrary code "
            "on the notebook host, and any member or prompt-injected agent can "
            "ask for one, so this stays off unless every notebook on the host "
            "belongs to one client. Off: scratch notebooks are always read-only."
        ),
    )


class ReportHostSettings(BaseModel):
    """Optional report-host client config.

    Both fields optional so deployments without a report host keep working.
    The publishing MCP tool raises ToolError when host_url is unset.
    """

    host_url: HttpUrl | None = Field(
        default=None,
        description="Base URL of the report-host service (e.g. http://report-host:8002).",
    )
    admin_secret: SecretStr | None = Field(
        default=None,
        description=(
            "Bearer secret used to authenticate admin calls to the report-host "
            "service. Must be the same value the report host itself is "
            "configured with (its DAIMON_REPORT__ADMIN_SECRETS) — rotating one "
            "without the other breaks publishing."
        ),
    )


class SentrySettings(BaseModel):
    """Sentry observability config.

    All fields optional so deployments without Sentry keep booting. When
    `dsn is None`, Sentry initialization is a no-op.
    """

    dsn: SecretStr | None = Field(
        default=None,
        description="Sentry DSN. When unset, error reporting is disabled entirely.",
    )
    environment: str = Field(
        default="production",
        description=(
            "Environment tag attached to every Sentry event (e.g. 'production', 'staging')."
        ),
    )
    traces_sample_rate: float = Field(
        default=0.0,
        description="Fraction (0.0-1.0) of transactions sampled for Sentry performance tracing.",
    )


class BillingSettings(BaseModel):
    """Money policy: markup multiplier and trial credit seed.

    Distinct from BillingConfig (billing.py) which holds Stripe secrets
    (flat STRIPE_* env vars). BillingSettings is nested DAIMON_BILLING__*
    policy; BillingConfig is Stripe secrets. Keep both separate.
    """

    markup: Decimal = Field(
        default=Decimal("1.0"),
        description=(
            "Multiplier applied to raw Anthropic usage cost before billing "
            "the tenant. 1.0 = pass-through."
        ),
    )
    signup_credit: Decimal = Field(
        default=Decimal("10.00"),
        description=(
            "USD credit automatically seeded when a guild/workspace is "
            "provisioned, so a freshly-installed tenant can chat immediately "
            "on trial credit before paying. Set to 0 to require payment "
            "before use."
        ),
    )


class SupportSettings(BaseModel):
    """Human-support escalation: where requests land, and how many each user gets.

    `credits_per_user` is a COUNT of human interactions, deliberately not the
    USD in `BillingSettings.signup_credit`. Sharing a ledger with billing would
    let a support request eat the tenant's ability to run turns, and would give
    a paid-up tenant unlimited support. Different unit, different table.

    Discord and Teams requests go to `escalation_channel_id` (a Teams `19:…`
    channel is posted by the Teams bot, any other id is a Discord channel);
    Slack requests go only to `slack_escalation_channel_id`, never to that
    channel. An unset channel disables the affordance it serves entirely
    rather than recording requests nobody will ever see. Failing closed is the
    honest behaviour: an escalate button that reaches no one is worse than no
    button, because the person believes they have asked for help. Every
    platform spends the same per-user, per-tenant allowance from one ledger.
    """

    escalation_channel_id: str | None = Field(
        default=None,
        description=(
            "Channel id where human-support requests from Discord and Teams are "
            "posted: a Discord channel id, or a Teams channel id (`19:…`) that the "
            "Teams bot posts in. Teams requests reach a Discord channel only when "
            "the Discord bot token is also set. Unset (the default) disables the "
            "escalate affordance on Discord and Teams — a request that reaches "
            "nobody is worse than no button at all. A channel rather than operator "
            "DMs: it survives one person's DMs being closed, and it leaves a shared "
            "record anyone on the rota can pick up. The bot must be able to post there."
        ),
    )
    slack_escalation_channel_id: str | None = Field(
        default=None,
        description=(
            "Slack channel id where human-support requests from Slack are "
            "posted. Unset (the default) disables the Ask a human button on "
            "Slack. Slack requests never go to the Discord channel, nor Discord "
            "requests here. The bot must be a member of the channel."
        ),
    )
    slack_escalation_team_id: str | None = Field(
        default=None,
        description=(
            "Slack workspace id (T…) that owns DAIMON_SUPPORT__SLACK_ESCALATION_CHANNEL_ID, "
            "for a deployment installed in several workspaces: every workspace's "
            "requests are posted with that workspace's bot token. Unset posts with "
            "the requesting workspace's own token, which suits a single-workspace "
            "install. daimon must be installed in the named workspace."
        ),
    )
    credits_per_user: int = Field(
        default=20,
        ge=0,
        description=(
            "How many human-support requests each user gets within a tenant. "
            "A COUNT of interactions, NOT the USD in DAIMON_BILLING__SIGNUP_CREDIT — "
            "the two are deliberately separate ledgers. 0 disables escalation."
        ),
    )


class ThreadNamingSettings(BaseModel):
    """Automatic Discord thread titles, generated by a metered Haiku call.

    A top-level feature block (``DAIMON_THREAD_NAMING__*``), not a
    ``DiscordSettings`` field, per the feature-settings rule. Discord-only in
    effect: Slack threads have no title (``tests/parity/
    test_thread_naming_discord_only.py``).
    """

    enabled: bool = Field(
        default=True,
        description=(
            "Title bot-created Discord threads from the opening message with a short "
            "Haiku-generated name, chosen before the thread is created. The call is "
            "metered to the tenant like any other model call. Set false to keep the "
            "static 'Chat with <agent>' title."
        ),
    )
    max_input_chars: int = Field(
        default=2000,
        gt=0,
        le=20_000,
        description=(
            "Characters of the opening message sent to the naming model; longer "
            "messages are cut here. Bounds the per-thread naming cost."
        ),
    )
    timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=60,
        description=(
            "Seconds to wait for the naming model before the thread opens under the "
            "static title. The thread is created only after this call, so this is "
            "the most a mention can wait before anything appears."
        ),
    )


class ArtifactsSettings(BaseModel):
    """Optional private object storage for hosted-client artifacts."""

    endpoint_url: HttpUrl = Field(
        description="S3-compatible bucket endpoint; used for uploads and presigned GET URLs.",
    )
    bucket: str = Field(
        min_length=1,
        description="Private bucket name. Daimon never applies a public-read ACL.",
    )
    access_key_id: SecretStr = Field(description="S3-compatible access key id.")
    secret_access_key: SecretStr = Field(description="S3-compatible secret access key.")
    region: str = Field(
        default="us-east-1",
        min_length=1,
        description="S3 signing region supplied by the bucket provider.",
    )
    url_ttl_seconds: int = Field(
        default=600,
        gt=0,
        le=86_400,
        description="Lifetime of each presigned artifact GET URL; defaults to ten minutes.",
    )
    embed_images: bool = Field(
        default=True,
        description=(
            "Also return bounded MCP image blocks for model vision. Disable independently "
            "when a hosted client cannot accept image content."
        ),
    )


class DirectMessagePolicy(BaseModel):
    """Tenant recipient restrictions; platform membership is always required."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["members", "allowlist", "disabled"] = "members"
    recipient_ids: list[str] = Field(default_factory=list[str])

    def allows(self, recipient_id: str) -> bool:
        return self.mode == "members" or (
            self.mode == "allowlist" and recipient_id in self.recipient_ids
        )


class RoutingSettings(BaseModel):
    channel_defaults: Literal["confidential_only", "legacy"] = Field(
        default="confidential_only",
        description=(
            "Use channel agent defaults only in confidential (isolated) channels. "
            "Set legacy to honor existing defaults in every channel during rollback."
        ),
    )


class Settings(BaseSettings):
    security_audit_retention_days: int = Field(
        default=90,
        ge=0,
        description=(
            "Security audit retention age in days, default 90. Operators must schedule "
            "daimon audit prune TENANT_UUID for each tenant (for example daily). "
            "The command deletes older events. Set 0 to explicitly retain events forever; "
            "privacy erasure and tenant deletion still apply."
        ),
    )
    completion_pings: dict[uuid.UUID, bool] = Field(
        default_factory=dict[uuid.UUID, bool],
        description=(
            "Per-tenant completion notification policy, keyed by tenant UUID. "
            "True enables accepted/done reactions and posts the final answer as a fresh reply "
            "mentioning only the requester "
            "on Discord and Slack. On Teams it closes the status card and posts the answer "
            "fresh, mentioning the requester in channels (Teams bots cannot react). "
            "Missing/false preserves in-place delivery. "
            "Configure DAIMON_COMPLETION_PINGS as a JSON object."
        ),
    )
    budget_notices: dict[uuid.UUID, bool] = Field(
        default_factory=dict[uuid.UUID, bool],
        description=(
            "Per-tenant switch for the channel budget notice, keyed by tenant UUID. "
            "When a channel's budget is used up, its channel admins (else the server admins) "
            "get one DM per budget window on Discord, Slack and Teams. Missing/true sends it; "
            "false turns it off. Configure DAIMON_BUDGET_NOTICES as a JSON object."
        ),
    )
    direct_message_policies: dict[uuid.UUID, DirectMessagePolicy] = Field(
        default_factory=dict[uuid.UUID, DirectMessagePolicy],
        description=(
            "Per-tenant DM recipient policies keyed by tenant UUID (normalized at load; "
            "invalid keys rejected). Default is "
            "members (live membership required). Set mode=disabled to disable DMs, "
            "or mode=allowlist with recipient_ids to restrict delivery to listed "
            "members. Configure DAIMON_DIRECT_MESSAGE_POLICIES as a JSON object."
        ),
    )
    table_rendering: dict[uuid.UUID, bool] = Field(
        default_factory=dict[uuid.UUID, bool],
        description=(
            "Per-tenant table rendering opt-in, keyed by tenant UUID. True renders "
            "final Markdown tables as PNG on Discord and native tables on Slack. "
            "Missing/false preserves plain text. Set DAIMON_TABLE_RENDERING to a JSON object."
        ),
    )
    database: DatabaseSettings
    anthropic: AnthropicSettings
    privacy_policy_url: HttpUrl = Field(
        default=HttpUrl("https://github.com/pymc-labs/daimon/blob/main/PRIVACY.md"),
        description=(
            "URL rendered on the privacy panels' Policy button. "
            "Override via DAIMON_PRIVACY_POLICY_URL if you host your own policy page."
        ),
    )
    cli: CLISettings = Field(default_factory=CLISettings)
    routing: RoutingSettings = Field(
        default_factory=RoutingSettings,
        description="Agent default routing mode; see RoutingSettings.",
    )
    log: LogSettings = Field(default_factory=LogSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    ops: OpsSettings = Field(default_factory=OpsSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    hub: HubSettings = Field(default_factory=HubSettings)
    discord: DiscordSettings | None = None
    thread_participation: ThreadParticipationSettings = Field(
        default_factory=ThreadParticipationSettings,
        description="Replying in threads unprompted. See ThreadParticipationSettings.",
    )
    slack: SlackSettings | None = None
    teams: TeamsSettings | None = None
    github: GithubSettings = Field(default_factory=GithubSettings)
    crypto: CryptoSettings = Field(default_factory=CryptoSettings)
    credentials: CredentialsSettings = Field(default_factory=CredentialsSettings)
    gemini: GeminiSettings = Field(default_factory=GeminiSettings)
    notebook: NotebookSettings = Field(default_factory=NotebookSettings)
    report_host: ReportHostSettings = Field(default_factory=ReportHostSettings)
    sentry: SentrySettings = Field(default_factory=SentrySettings)
    billing: BillingSettings = Field(default_factory=BillingSettings)
    support: SupportSettings = Field(default_factory=SupportSettings)
    thread_naming: ThreadNamingSettings = Field(default_factory=ThreadNamingSettings)
    tool_safety: ToolSafetyPolicy = Field(
        default_factory=ToolSafetyPolicy,
        description=(
            "Read/write classes and confirmation for attached third-party MCP tools. "
            "See ToolSafetyPolicy."
        ),
    )
    artifacts: ArtifactsSettings | None = Field(
        default=None,
        description=(
            "Optional private S3-compatible store for presigned chart links. "
            "Bounded image embeds still run when this is unset."
        ),
    )
    defaults_root: Path = Field(
        default_factory=lambda: Path("defaults"),
        description=(
            "Filesystem path to the seeded defaults/ directory consumed by "
            "daimon.core.defaults.apply.apply_defaults. Default Path('defaults') "
            "is relative to the process cwd — works for in-repo dev (repo "
            "root) and the deployed container layout (working dir contains "
            "defaults/ at root). Single source of truth shared by the "
            "scheduler, Discord/Slack adapters, CLI session bootstrap, and "
            "MCP routine tools. Do NOT add adapter-local defaults_root "
            "fields — they invite drift."
        ),
    )

    model_config = SettingsConfigDict(
        env_prefix="DAIMON_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Validation errors must never echo a raw value (crypto keys, API keys).
        hide_input_in_errors=True,
    )


def load_settings(*, _env_file: str | None = ".env") -> Settings:
    """Construct a `Settings` from the live process env + optional `.env` file.

    `_env_file` exists to give tests a way to disable `.env` loading
    (`_env_file=None`) so they only see `monkeypatch.setenv` values.
    """
    return Settings(_env_file=_env_file)  # pyright: ignore[reportCallIssue]


class _CryptoSettingsSource(BaseSettings):
    """Crypto-only settings for migrations and standalone store sessions."""

    crypto: CryptoSettings = Field(default_factory=CryptoSettings)
    model_config = SettingsConfigDict(
        env_prefix="DAIMON_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )


def load_crypto_settings() -> CryptoSettings:
    """Load keys without requiring unrelated API or database configuration."""
    return _CryptoSettingsSource().crypto
