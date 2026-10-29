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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

from daimon.core.mcp_server_url import canonical_mcp_url
from daimon.core.scope_cascade import CascadeMode
from daimon.core.thread_participation import ParticipationMode
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolSafetyPolicy
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    PostgresDsn,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

if TYPE_CHECKING:
    from daimon.core.specs import SettingsFeature


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


ANTHROPIC_UNCONFIGURED_PLACEHOLDER = "sk-ant-unconfigured-openai-only-deployment"
"""Never a valid credential. `AnthropicSettings.resolved_api_key()` returns
this when no real key is configured, so every `AsyncAnthropic` client in the
app can keep its existing non-optional constructor call: an OpenAI-only
deployment that never exercises the Anthropic/default path never notices,
and one that accidentally does gets a clean 401 instead of a crash at
startup or a scattered `Optional` check at every call site."""


class AnthropicSettings(BaseModel):
    api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Anthropic API key used to authenticate all Managed Agents SDK calls. "
            "Required unless this deployment is OpenAI-only (see docs/self-hosting.md's "
            "'Self-host on OpenAI'), in which case leave it unset."
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

    def resolved_api_key(self) -> str:
        """The real key, or a placeholder for an OpenAI-only deployment.

        Never falls back to an ambient `ANTHROPIC_API_KEY`-style env var —
        unset means unset, exactly like every other explicit credential in
        this settings tree.
        """
        return (
            self.api_key.get_secret_value()
            if self.api_key is not None
            else ANTHROPIC_UNCONFIGURED_PLACEHOLDER
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
    legacy_token_cutoff: AwareDatetime | None = Field(
        default=None,
        description=(
            "UTC deadline for a temporary legacy-token deployment window. Dates before "
            "2026-10-16 or over six hours in the future are refused. At/after the deadline "
            "all unregistered tokens are rejected, regardless of issuance time. Internal "
            "tokens are always rejected without a registry row."
        ),
    )
    allow_legacy_tokens: bool = Field(
        default=False,
        description=(
            "Temporary minters-first deployment compatibility only. Requires a bounded "
            "legacy_token_cutoff; only non-internal legacy tokens qualify and stop working at that "
            "deadline even if the deployment is interrupted. Default false requires "
            "registered tokens immediately."
        ),
    )

    @model_validator(mode="after")
    def bounded_legacy_window(self) -> McpSettings:
        cutoff = self.legacy_token_cutoff
        now = datetime.now(UTC)
        if cutoff is not None and cutoff < datetime(2026, 10, 16, tzinfo=UTC):
            raise ValueError("legacy_token_cutoff must be on or after 2026-10-16 UTC")
        if cutoff is not None and cutoff > now + timedelta(hours=6):
            raise ValueError("legacy_token_cutoff cannot be more than six hours ahead")
        if self.allow_legacy_tokens and cutoff is None:
            raise ValueError("legacy compatibility requires an explicit cutoff")
        return self

    require_routine_destination: bool = Field(
        default=False,
        description=(
            "Require an explicit destination when the MCP create_routine tool is called. "
            "When enabled, a missing destination is refused before agent lookup or creation, "
            "with a warning that the routine would not post. Default false preserves "
            "record-only routines; existing routines and updates are unaffected."
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


class McpOAuthClientSettings(BaseModel):
    """One OAuth client registered by hand, and the MCP servers it may sign in to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_server_host: str = Field(min_length=1)
    mcp_servers: list[str] = Field(min_length=1)
    client_id: str = Field(min_length=1)
    client_secret: SecretStr
    token_endpoint_auth_method: Literal["client_secret_basic", "client_secret_post"] = (
        "client_secret_post"
    )
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    scope: str | None = None
    send_resource: bool = True

    @field_validator("authorization_server_host")
    @classmethod
    def _bare_host(cls, value: str) -> str:
        host = value.strip().rstrip(".").lower()
        if not host or any(char in host for char in "/:@ "):
            raise ValueError("authorization_server_host must be a bare host name")
        return host

    @field_validator("mcp_servers")
    @classmethod
    def _public_https_servers(cls, value: list[str]) -> list[str]:
        # Imported here: the package pulls in the Anthropic SDK, which a plain
        # settings load should not pay for when no client is configured.
        from daimon.core.mcp_oauth.urls import McpUrlError, assert_public_https_url

        try:
            return [
                canonical_mcp_url(assert_public_https_url(url, what="mcp_servers entry"))
                for url in value
            ]
        except McpUrlError as err:
            raise ValueError(str(err)) from err

    @field_validator("authorization_endpoint", "token_endpoint")
    @classmethod
    def _public_https(cls, value: str | None) -> str | None:
        from daimon.core.mcp_oauth.urls import McpUrlError, assert_public_https_url

        if value is None:
            return None
        try:
            return assert_public_https_url(value, what="endpoint override")
        except McpUrlError as err:
            raise ValueError(str(err)) from err

    @field_validator("scope")
    @classmethod
    def _space_separated(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.split():
            raise ValueError("scope must name at least one scope, or be left out")
        return " ".join(value.split())


class McpOAuthSettings(BaseModel):
    clients: Annotated[list[McpOAuthClientSettings], NoDecode] = Field(
        default_factory=list[McpOAuthClientSettings],
        description=(
            "OAuth clients registered by hand, for authorization servers that offer no "
            "dynamic client registration. A JSON array; each entry has "
            "authorization_server_host (the bare host of the authorization server), "
            "mcp_servers (required: the exact https URLs of the MCP servers this client may "
            "be used for), client_id, client_secret, token_endpoint_auth_method "
            "(client_secret_post by default, or client_secret_basic), optional "
            "authorization_endpoint and token_endpoint overrides (public https), optional "
            "scope (space-separated; replaces the scopes the MCP server publishes) and "
            "send_resource (default true; false leaves the RFC 8707 resource parameter out "
            "of sign-in and refresh). A sign-in uses an entry only when its MCP server is "
            "listed and its authorization server is on that host; it then also asks for "
            "offline_access when the authorization server offers it. Every other sign-in "
            "registers a client dynamically. Default: [] (always register)."
        ),
    )

    @field_validator("clients", mode="before")
    @classmethod
    def _parse_json(cls, value: object) -> object:
        # Decoded here, not by the settings source: a blank value (a compose
        # file passing `${DAIMON_MCP_OAUTH__CLIENTS:-}`) must mean "none", and
        # bad JSON must name this variable rather than the whole section.
        if not isinstance(value, str):
            return value
        if not value.strip():
            return []
        try:
            return json.loads(value)
        except json.JSONDecodeError as err:
            # The position only: the decoder's message can quote the value, a secret.
            raise ValueError(
                f"DAIMON_MCP_OAUTH__CLIENTS is not valid JSON "
                f"(line {err.lineno}, column {err.colno})"
            ) from None

    @field_validator("clients")
    @classmethod
    def _one_client_per_server(
        cls, value: list[McpOAuthClientSettings]
    ) -> list[McpOAuthClientSettings]:
        seen: set[str] = set()
        for url in (url for client in value for url in client.mcp_servers):
            if url in seen:
                raise ValueError(f"{url} is listed under more than one client")
            seen.add(url)
        return value


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


class ProgressPlansSettings(BaseModel):
    """Progress plans: the agent keeps a short plan the working card draws.

    `mode` is the deployment tier of the same cascade thread participation
    uses (deployment, workspace, channel, thread); the agent's
    `set_progress_plans` tool writes the other tiers when a person asks.
    """

    mode: CascadeMode = Field(
        default=CascadeMode.OFF,
        description=(
            'Deployment default for progress plans on the "Working on it…" card (Discord, '
            "Slack and Teams). On, the agent keeps a short checklist of a longer task's "
            "steps on the card and may add one-line updates on what it found and does "
            "next. off: shown only where a workspace, channel or thread is turned on. "
            "disabled: never shown, and cannot be turned on. on: shown everywhere unless "
            "turned off below."
        ),
    )


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
        default="claude-haiku-5-5",
        description=(
            "Model that decides whether a burst of unprompted messages deserves a reply. "
            "One short call per quiet burst; a small, fast model is the point. The call "
            "sets effort low, so pick a model that supports the effort parameter."
        ),
    )


class EnvironmentSettings(BaseModel):
    name: str = Field(
        default="",
        description="Deployment name. Empty selects production Discord command behavior.",
    )
    label: str = Field(
        default="",
        description=(
            "Environment chip text on MCP web pages, such as Staging. Empty falls back to the "
            "deployment name; both empty shows no chip. Does not affect Discord command sync."
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
    command_guild_ids: list[int] = Field(
        default_factory=list[int],
        description=(
            "Guilds allowed to receive commands and events when the environment name is set. "
            "An empty list disables all Discord ingress, including DMs, in non-production."
        ),
    )
    startup_replay_minutes: int = Field(
        default=0,
        ge=0,
        le=60,
        description=(
            "Recent Discord activity window in minutes scanned on startup for "
            "missed mentions and replies. Set 0 to disable catch-up."
        ),
    )
    startup_replay_channels_per_guild: int = Field(
        default=25,
        ge=1,
        le=100,
        description=(
            "Maximum recently active Discord channels and threads scanned per guild on startup."
        ),
    )
    startup_replay_messages_per_channel: int = Field(
        default=100,
        ge=1,
        le=1000,
        description=(
            "Maximum recent Discord messages scanned per channel or thread on startup. "
            "Current message gates and durable admissions prevent duplicate turns."
        ),
    )
    writers_none_notice: bool = Field(
        default=False,
        description=(
            "Opt in to one stop reaction on an explicit bot mention refused by a confirmed "
            "writers=none rule. Only the posting owner reacts, with per-requester, channel "
            "and bot-wide cooldowns. No text, DM or agent turn is sent. Unknown protection "
            "stays silent. Limits are process-local and reset on restart."
        ),
    )
    credential_form_expiry: bool = Field(
        default=False,
        description=(
            "Automatically retire expired, unclicked credential cards in place. "
            "Requires the existing single-posting-process wake poller switch. "
            "Default false starts no expiry task and adds no database or Discord calls. "
            "Failed card edits remain pending for retry; "
            "no credentials or waiting work are applied."
        ),
    )
    thread_open_notice_after_s: float = Field(
        default=3.0,
        ge=0,
        description=(
            "Seconds after an admitted opening mention before reacting to it to show that its "
            "Discord thread is still opening. Includes thread naming and Discord rate-limit "
            "waits. Set to 0 to react immediately. The reaction is removed when creation "
            "finishes; a failed creation is answered with a visible error reply."
        ),
    )
    unmentioned_reply_hint: bool = Field(
        default=False,
        description=(
            "React with 🔔 to a person's reply that does not @mention the bot in a thread the "
            "bot opened, as a hint that only mentions start a turn. The message itself is "
            "never answered. At most once per thread per unmentioned_reply_hint_cooldown_h, "
            "never on a message that mentions someone else, never to any bot, never where the "
            "agent may not post, and never while the thread is followed (thread "
            "participation on). Off by default: the cooldown is kept in memory, so each "
            "restart allows one more hint per thread."
        ),
    )
    unmentioned_reply_hint_cooldown_h: float = Field(
        default=24.0,
        gt=0,
        description=(
            "Hours before the unmentioned-reply hint may be shown again in the same thread. "
            "Kept in memory, so a restart allows one more."
        ),
    )
    protected_channel_notice_cooldown_min: float = Field(
        default=10.0,
        gt=0,
        description=(
            "Minutes before the same person is told again that Daimon can't reply in a "
            "channel whose Who can post rule is Nobody. Their mention gets a 🔒 reaction and "
            "a direct message naming the rule and who can change it; nothing else is posted "
            "and no turn runs. Kept in memory per person and channel, so a restart allows "
            "one more."
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
    tenant_cap_waiting_card: bool = Field(
        default=False,
        description=(
            "Opt in to queue wording on the existing initial Discord mention card while "
            "its admitted turn waits behind the tenant concurrency cap. The same card "
            "returns to ordinary progress when the turn starts. Admission, Stop and queue "
            "limits are unchanged; no other tenant information is displayed."
        ),
    )
    max_concurrent_turns: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Maximum Discord agent turns running across all guilds and DMs in this process. "
            "Unset leaves deployment-wide admission unlimited. Excess turns wait in the "
            "turn queue (see turn_queue); continuation wakes keep their existing admission path."
        ),
    )
    turn_card_unrecoverable_after_s: int = Field(
        default=86400,
        ge=1,
        description=(
            "Minimum age in seconds before Discord can retire a card after definite "
            "recovery failure or repeated failed recovery passes. Must be at least the turn "
            "ceiling plus 15 minutes. Recovery runs at startup and periodically, "
            "and deletes a known "
            "pending card when the bot has Manage Messages permission."
        ),
    )
    turn_card_unrecoverable_after_attempts: int = Field(
        default=3,
        ge=2,
        description=(
            "Failed Discord card recovery passes required before an aged intent can "
            "be retired without a definite platform failure. Count persists across restarts."
        ),
    )

    @field_validator("turn_card_unrecoverable_after_s")
    @classmethod
    def _stale_card_age_exceeds_turn_ceiling(cls, age_s: int) -> int:
        # Import after config is initialized: turn.__init__ imports admission,
        # which imports config, so a module-level import would form a cycle.
        from daimon.core.turn.ceiling import TURN_CEILING_S

        if age_s < TURN_CEILING_S + 15 * 60:
            raise ValueError(
                "turn card unrecoverable age must be at least turn ceiling plus 15 minutes"
            )
        return age_s

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
            "mention or reply to a recorded agent post. Bot-authored messages "
            "are rejected by default; these are allow-listed so a harness can "
            "drive addressed turns end-to-end without a human. Several are "
            "supported because admin-gated tools "
            "need a caller holding Manage Server while the refusal paths need "
            "one without it. Leave empty outside test deployments -- an "
            "allow-listed bot spends real credit. As a tuple field this must "
            'be set as a JSON array, e.g. \'["123","456"]\'. Daimon\'s own id '
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
            "Framework messaging endpoint without /api/messages). Graph's change "
            "notifications for private and shared channels post to it."
        ),
    )
    admin_team_id: str | None = Field(
        default=None,
        description=(
            "Entra group ID of the team whose owners administer this deployment "
            "from Teams: admins get the admin role in turns, create routines, "
            "replace shared keys, top up and see everyone's usage. Adding an "
            "admin means making them an owner of that team; no redeploy. The "
            "app must be installed in that team. To find the ID, open the "
            "team's ⋯ menu in Teams, choose Get link to team and copy the "
            "groupId parameter. Unset: nobody is an admin."
        ),
    )

    @field_validator("admin_team_id")
    @classmethod
    def _canonicalize_admin_team_id(cls, value: str | None) -> str | None:
        """Compare against the canonical lowercase form Graph and activities use."""
        if value is None or not value.strip():
            return None
        try:
            return str(UUID(value))
        except ValueError:
            raise ValueError(
                "DAIMON_TEAMS__ADMIN_TEAM_ID must be the team's Entra group ID UUID"
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


def decode_github_app_private_key(value: object) -> object:
    """Accept raw PEM or base64-encoded PEM for both App settings groups."""
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


def validate_github_app_slug(value: str | None) -> str | None:
    """Require GitHub's URL slug shape before building an installation link."""
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


class GithubSettings(BaseModel):
    """Size limits for GitHub skill imports."""

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


class GithubAppSettings(BaseModel):
    """Credentials for agent-scoped GitHub App access."""

    app_id: str | None = Field(default=None, description="GitHub App ID for agent-scoped access.")
    app_slug: str | None = Field(
        default=None, description="GitHub App URL slug used for installation links."
    )
    private_key: SecretStr | None = Field(
        default=None, description="GitHub App private key as PEM or base64-encoded PEM."
    )
    webhook_secret: SecretStr | None = Field(
        default=None, description="Secret for verifying this App's webhook signatures."
    )
    client_id: str | None = Field(
        default=None, description="GitHub App client ID for user authorization."
    )
    client_secret: SecretStr | None = Field(
        default=None, description="GitHub App client secret for user authorization."
    )

    @field_validator("private_key", mode="before")
    @classmethod
    def _decode_private_key(cls, value: object) -> object:
        return decode_github_app_private_key(value)

    @field_validator("app_slug")
    @classmethod
    def _validate_slug(cls, value: str | None) -> str | None:
        return validate_github_app_slug(value)


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


_ENTRA_AUTHORITY = HttpUrl("https://login.microsoftonline.com")


class MicrosoftAccountsSettings(BaseModel):
    """Per-person Microsoft 365 access on Teams: the Entra app people sign in to.

    Inert until `client_id`, `client_secret` and `tenant_id` are all set.
    """

    client_id: str | None = Field(
        default=None,
        description=(
            "Application (client) ID of the Entra app registration people sign in to "
            "with Connect Microsoft. Its Web redirect URI is "
            "`<public root>/oauth/microsoft/callback`."
        ),
    )
    client_secret: SecretStr | None = Field(
        default=None,
        description="Client secret of that app registration, sent as client_secret_post.",
    )
    tenant_id: str | None = Field(
        default=None,
        description=(
            "Entra tenant ID (a GUID) people sign in to. Only accounts of this tenant whose "
            "object ID matches the Teams user asking can connect."
        ),
    )
    authority: HttpUrl = Field(
        default=_ENTRA_AUTHORITY,
        description="Microsoft identity platform host; change it only for a sovereign cloud.",
    )

    @field_validator("client_id", "client_secret", "tenant_id", "authority", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object, info: ValidationInfo) -> object:
        # A compose file passing an empty variable through means "not set".
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not (isinstance(raw, str) and not raw.strip()):
            return value
        return _ENTRA_AUTHORITY if info.field_name == "authority" else None

    @field_validator("tenant_id")
    @classmethod
    def _canonical_tenant(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(uuid.UUID(value))
        except ValueError:
            raise ValueError(
                "DAIMON_MICROSOFT_ACCOUNTS__TENANT_ID must be the Entra directory tenant's UUID"
            ) from None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.tenant_id)

    @property
    def touched(self) -> bool:
        """Whether any of the three is set, so a missing one is worth a warning."""
        return bool(self.client_id or self.client_secret or self.tenant_id)


class CredentialsSettings(BaseModel):
    """Tenant-level credentials for the token broker.

    Optional — Google Workspace requires a matching authorized-user credential
    or a service-account credential with domain-wide delegation.
    """

    google_sa_json: SecretStr | None = Field(
        default=None,
        description=(
            "Full JSON contents of a Google service-account key, used by the "
            "token broker to mint delegated Google Workspace credentials."
        ),
    )

    google_user_json: SecretStr | None = Field(
        default=None,
        description=(
            "Full authorized_user JSON containing client_id, client_secret and "
            "refresh_token. Used only for agents bound to google_user_email, "
            "with the binding's scopes; requires no domain-wide delegation."
        ),
    )
    google_user_email: str | None = Field(
        default=None,
        description=(
            "Google account email belonging to google_user_json (the JSON does "
            "not contain it). Only matching agent bindings, compared without "
            "case sensitivity, may use that credential."
        ),
    )


class McpCredentialBinding(BaseModel):
    """Operator authority for one harness credential, never its secret value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^HARNESS_MCP_CRED_[A-Za-z0-9_]+$")
    origin: str
    tenants: tuple[UUID, ...] | tuple[Literal["*"],]

    @field_validator("origin")
    @classmethod
    def exact_https_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            raise ValueError("MCP credential binding requires an exact HTTPS origin")
        port = parsed.port
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        return f"https://{host}" + (f":{port}" if port not in (None, 443) else "")

    @field_validator("tenants")
    @classmethod
    def nonempty_tenants(
        cls, value: tuple[UUID, ...] | tuple[Literal["*"],]
    ) -> tuple[UUID, ...] | tuple[Literal["*"],]:
        if not value:
            raise ValueError("MCP credential binding requires at least one tenant")
        return value


class HarnessAuthBinding(McpCredentialBinding):
    """Operator authority for a harness bearer variable or Entra audience."""

    name: str = Field(min_length=1)
    auth_mode: Literal["bearer_env", "entra"] = "bearer_env"

    @model_validator(mode="after")
    def valid_reference(self) -> HarnessAuthBinding:
        from mux.drivers.selfhosted import SESSION_POOL_AUDIENCE, HarnessConfig

        # A binding authorizes an audience/origin, not an environment's routing.
        # Validate the fixed pool audience in its required routing mode here;
        # the actual environment must still explicitly opt into session_pool.
        HarnessConfig(
            harness_url=self.origin,
            auth_mode=self.auth_mode,
            auth_ref=self.name,
            session_pool=self.auth_mode == "entra" and self.name == SESSION_POOL_AUDIENCE,
        )
        if self.auth_mode == "bearer_env" and not self.name.startswith(
            "DAIMON_SELFHOSTED__HARNESS_TOKEN_"
        ):
            raise ValueError("Harness bearer binding requires an operator token variable")
        return self


class SelfHostedSettings(BaseModel):
    """Operator opt-in for the self-hosted turn runtime."""

    admission_enabled: bool = Field(
        default=False,
        description=(
            "Allow explicitly configured selfhosted channels to pass backend admission. "
            "Default false. Enabling this still requires the profile's usage observations "
            "capability and a configured self-hosted core runtime."
        ),
    )

    single_tenant: bool = Field(
        default=False,
        description=(
            "Declare this self-hosted installation single-tenant. Required for wildcard "
            "MCP credential bindings; leave false for shared installations."
        ),
    )
    mcp_allowed_http_hosts: tuple[str, ...] = Field(
        default=(),
        description=(
            "Operator-authorized exact hostnames or IP literals for credential-free HTTP MCP "
            "connections to private, link-local or metadata destinations. Empty by default. "
            "Loopback remains allowed; credential-bearing connections still require HTTPS."
        ),
    )

    harness_auth_bindings: tuple[HarnessAuthBinding, ...] = Field(
        default=(),
        description=(
            "JSON list of harness auth bindings: auth_mode (bearer_env or entra), name "
            "(DAIMON_SELFHOSTED__HARNESS_TOKEN_* variable or exact Entra audience), "
            "origin (exact HTTPS origin), tenants (UUID list). Unbound names and destinations "
            "are refused before credential resolution. Wildcards require single_tenant."
        ),
    )

    @field_validator("harness_auth_bindings")
    @classmethod
    def unique_harness_names(
        cls, value: tuple[HarnessAuthBinding, ...]
    ) -> tuple[HarnessAuthBinding, ...]:
        if len({(binding.auth_mode, binding.name) for binding in value}) != len(value):
            raise ValueError("Harness credential binding names must be unique")
        return value

    mcp_credential_bindings: tuple[McpCredentialBinding, ...] = Field(
        default=(),
        description=(
            "JSON list of operator MCP credential bindings: each has name "
            "(HARNESS_MCP_CRED_*), origin (exact HTTPS origin), and tenants (UUID list). "
            "Unbound references, other origins and other tenants are refused. "
            'Use tenants=["*"] only for a single-tenant installation. Configure the '
            "harness HARNESS_MCP_CRED_BINDINGS from the same operator source."
        ),
    )

    @field_validator("mcp_credential_bindings")
    @classmethod
    def unique_names(
        cls, value: tuple[McpCredentialBinding, ...]
    ) -> tuple[McpCredentialBinding, ...]:
        if len({binding.name for binding in value}) != len(value):
            raise ValueError("MCP credential binding names must be unique")
        return value

    @model_validator(mode="after")
    def wildcard_requires_single_tenant(self) -> SelfHostedSettings:
        if not self.single_tenant and any(
            b.tenants == ("*",)
            for b in (*self.mcp_credential_bindings, *self.harness_auth_bindings)
        ):
            raise ValueError(
                "Wildcard MCP credential bindings require a single-tenant installation"
            )
        return self


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


class SelfhostedModelPrice(BaseModel):
    """One effective Azure tariff, in USD/M tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    effective_from: date
    input: Decimal = Field(gt=0, allow_inf_nan=False)
    output: Decimal = Field(gt=0, allow_inf_nan=False)
    cache_read: Decimal = Field(ge=0, allow_inf_nan=False)
    valid_before: date | None = None

    @model_validator(mode="after")
    def validate_window(self) -> SelfhostedModelPrice:
        if self.valid_before is not None and self.valid_before <= self.effective_from:
            raise ValueError("pricing validity must extend beyond its effective date")
        return self


class SelfhostedBillingSettings(BaseModel):
    """Explicit deployment identities and effective tariff history per model."""

    model_config = ConfigDict(extra="forbid")

    admission_clock_skew_s: float = Field(
        default=5,
        ge=0,
        le=60,
        allow_inf_nan=False,
        description=(
            "Maximum harness clock lag in seconds when associating usage with an existing "
            "tariff admission. Frozen at admission; default 5, maximum 60. "
            "This tolerance never creates an admission or selects current prices."
        ),
    )

    deployments: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "JSON object mapping selfhosted Azure deployment names to underlying model ids. "
            "Targets must have configured tariffs and aliases cannot shadow another model. "
            "The runtime must call the tariff admission gate before paid model work; "
            "these settings alone do not enable refusal or execution."
        ),
    )
    models: dict[str, list[SelfhostedModelPrice]] = Field(
        default_factory=dict,
        description=(
            "JSON object keyed by underlying Azure OpenAI model id, each containing a list "
            "of historical tariffs. Each requires effective_from (ISO date), input, output "
            "and cache_read (USD per million tokens); optional valid_before expires a row. "
            "A newer effective_from ends the previous open window. Retain historical rows. "
            "Supply verified regional/contract rates; input/output must be positive. "
            "Runtime admission must persist the selected tariff before paid work."
        ),
    )

    @model_validator(mode="after")
    def validate_tariffs(self) -> SelfhostedBillingSettings:
        for model, history in self.models.items():
            if not model.strip() or not history:
                raise ValueError("models require a nonempty identity and tariff history")
            ordered = sorted(history, key=lambda rate: rate.effective_from)
            for previous, following in zip(ordered, ordered[1:], strict=False):
                if previous.effective_from == following.effective_from or (
                    previous.valid_before is not None
                    and previous.valid_before > following.effective_from
                ):
                    raise ValueError("model tariff windows must not overlap")
        for deployment, model in self.deployments.items():
            if not deployment.strip() or model not in self.models:
                raise ValueError("deployments must map to a priced underlying model")
            if deployment in self.models and deployment != model:
                raise ValueError("deployment alias cannot shadow another model")
        return self


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
    selfhosted: SelfhostedBillingSettings = Field(default_factory=SelfhostedBillingSettings)
    signup_credit: Decimal = Field(
        default=Decimal("10.00"),
        description=(
            "USD credit automatically seeded when a guild/workspace is "
            "provisioned, so a freshly-installed tenant can chat immediately "
            "on trial credit before paying. Set to 0 to require payment "
            "before use."
        ),
    )
    gemini_daily_model_cap_usd: Decimal = Field(
        default=Decimal("20"),
        ge=0,
        description=(
            "Most a tenant may spend each UTC day on any one Gemini model, in provider "
            "USD at list price, counted across every agent turn and fleet worker. A "
            "Gemini agent turn has no token envelope and its usage reaches no ledger "
            "debit, so this is the guard. Checked as each turn or worker starts, for "
            "its first request's model and any Flash fallback of it, never mid-turn; "
            "a model with no current tariff is refused, and an unpriced fallback never "
            "runs. It resets at midnight UTC. 0 allows no Gemini turns."
        ),
    )
    openai_daily_model_cap_usd: Decimal = Field(
        default=Decimal("20"),
        ge=0,
        description=(
            "Most a tenant may spend each UTC day on any one OpenAI model, in provider "
            "USD at list price, counted across every agent turn and fleet worker. "
            "OpenAI sessions carry no native spend limit on this project, so this is "
            "the guard. Checked as each turn or worker starts, never mid-turn; a model "
            "with no current tariff is refused. It resets at midnight UTC. 0 allows no "
            "OpenAI turns."
        ),
    )


class SupportSettings(BaseModel):
    """Human-support escalation: where requests land, and how many each user gets.

    `credits_per_user` is a COUNT of human interactions, deliberately not the
    USD in `BillingSettings.signup_credit`. Sharing a ledger with billing would
    let a support request eat the tenant's ability to run turns, and would give
    a paid-up tenant unlimited support. Different unit, different table.

    Each platform's requests go only to its own channel: Discord's to
    `escalation_channel_id`, Slack's to `slack_escalation_channel_id`, Teams'
    to `teams_escalation_channel_id`. An unset channel disables the affordance
    it serves entirely rather than recording requests nobody will ever see.
    Failing closed is the honest behaviour: an escalate button that reaches no
    one is worse than no button, because the person believes they have asked
    for help. Every platform spends the same per-user, per-tenant allowance
    from one ledger.
    """

    escalation_channel_id: str | None = Field(
        default=None,
        description=(
            "Discord channel id where human-support requests from Discord are "
            "posted (the unprefixed name predates per-platform channels). A Teams "
            "id (`19:…`) left here is ignored. Unset (the default) disables the "
            "escalate affordance on Discord — a request that reaches nobody is "
            "worse than no button at all. A channel rather than operator DMs: it "
            "survives one person's DMs being closed, and it leaves a shared "
            "record anyone on the rota can pick up. The bot must be able to post "
            "there."
        ),
    )
    slack_escalation_channel_id: str | None = Field(
        default=None,
        description=(
            "Slack channel id where human-support requests from Slack are "
            "posted. Unset (the default) disables the Ask the team button on "
            "Slack. Slack requests never go to another platform's channel, nor "
            "theirs here. The bot must be a member of the channel."
        ),
    )
    slack_escalation_team_id: str | None = Field(
        default=None,
        description=(
            "Slack workspace id (T…) that owns DAIMON_SUPPORT__SLACK_ESCALATION_CHANNEL_ID, "
            "for a deployment installed in several workspaces: every workspace's "
            "requests are posted with that workspace's bot token. Unset posts with "
            "the requesting workspace's own token, which suits a single-workspace "
            "install. Daimon must be installed in the named workspace."
        ),
    )
    teams_escalation_channel_id: str | None = Field(
        default=None,
        description=(
            "Teams channel id (`19:…`) where human-support requests from Teams are "
            "posted. Unset (the default) disables Ask a person on Teams. Teams "
            "requests never go to the Discord or Slack channel, nor theirs here. "
            "The Teams bot must be a member of the channel's team (and of the "
            "channel, if it is private)."
        ),
    )

    @field_validator("teams_escalation_channel_id")
    @classmethod
    def _require_teams_channel(cls, value: str | None) -> str | None:
        """Another platform's id would spend Teams credits on posts that always fail."""
        if not value:
            return None
        if not value.startswith("19:"):
            raise ValueError(
                "DAIMON_SUPPORT__TEAMS_ESCALATION_CHANNEL_ID must be a Teams channel id (19:…)"
            )
        return value

    credits_per_user: int = Field(
        default=20,
        ge=0,
        description=(
            "How many human-support requests each user gets within a tenant. "
            "A COUNT of interactions, NOT the USD in DAIMON_BILLING__SIGNUP_CREDIT — "
            "the two are deliberately separate ledgers. 0 disables escalation."
        ),
    )
    feedback_to_support: dict[uuid.UUID, bool] = Field(
        default_factory=dict[uuid.UUID, bool],
        description=(
            "Per-tenant switch, keyed by tenant UUID: true also posts every submitted "
            '\N{THUMBS DOWN SIGN} "What went wrong?" form (the reasons, the text, the '
            "person, the agent and a link to the answer) to the channel Ask a human "
            "posts to: DAIMON_SUPPORT__SLACK_ESCALATION_CHANNEL_ID for Slack, "
            "DAIMON_SUPPORT__ESCALATION_CHANNEL_ID for Discord and "
            "DAIMON_SUPPORT__TEAMS_ESCALATION_CHANNEL_ID for Teams. "
            "Missing/false (the default) keeps the form in the database only. The form "
            "tells the person their answers are shared when it is on. Spends no "
            "support credit. Configure DAIMON_SUPPORT__FEEDBACK_TO_SUPPORT as a JSON object."
        ),
    )

    def routes_feedback(self, tenant_id: uuid.UUID) -> bool:
        """Whether this tenant's submitted 👎 forms also go to the support channel."""
        return self.feedback_to_support.get(tenant_id) is True


class TurnSettings(BaseModel):
    """Neutral turn ports. mux and backend selection are on by default.

    `path`, `channel_backends` and `agent_backends` are emergency stops, not setup:
    a fresh install needs none of them. Setting one off stops new turns at
    admission and queued or retried work before it is sent again.
    """

    path: Literal["legacy", "mux"] = Field(
        default="mux",
        description=(
            "Turn I/O path. mux (the default) runs turns through the neutral "
            "Events/Sessions ports. Emergency stop only: legacy returns every turn "
            "to the Managed Agents-only path."
        ),
    )

    native_footer_cost: bool = Field(
        default=False,
        description=(
            "Display settled native OpenAI/Gemini turn cost on platform footers. "
            "Requires complete normalized usage, verified rates and root accounting. "
            "Incomplete totals display Cost unknown or Cost partial, never an estimate. "
            "Off by default; Anthropic footers are unchanged with either value."
        ),
    )

    channel_backends: bool = Field(
        default=True,
        description=(
            "Read each agent's and channel's backend configuration at admission. On by "
            "default: a channel with no backend configuration is unchanged; a "
            "configured one is checked against its profile and refused "
            "(backend_unsupported) if the profile cannot meet it or this release cannot "
            "run it yet. Emergency stop only: false reads no backend configuration and "
            "every turn runs on Anthropic as before."
        ),
    )

    agent_backends: bool = Field(
        default=True,
        description=(
            "Capture complete scoped source and resource intent for an agent-selected "
            "native backend. Unproved obligations are refused; counterpart identity "
            "readback alone never authorizes execution. Requires mux and "
            "channel_backends; on by default. Emergency stop only: false refuses "
            "agent-selected native backends."
        ),
    )

    status_log: bool = Field(
        default=False,
        description=(
            "Keep each message the agent writes apart in its answer. Off by default: "
            "consecutive messages from the agent are joined into one block of text. On: an "
            "answer made of several messages keeps every one, set apart by a blank line "
            'instead of being run together. Either way the "Working on it…" card shows the '
            "last three messages the agent wrote, oldest first, one line each (cut at 120 "
            "characters)."
        ),
    )

    @field_validator("path", mode="before")
    @classmethod
    def _empty_path_is_the_default(cls, value: object) -> object:
        # An empty value is how compose passes an unset variable: the default, not a stop.
        return "mux" if value == "" else value


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
            "metered to the tenant like any other model call. Set false to title "
            "threads with the opening message itself, cut to 50 characters."
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
            "Seconds to wait for the naming model before the thread opens under a "
            "title taken from the opening message. The thread is created only after "
            "this call, so this is the most a mention can wait before anything appears."
        ),
    )


class PrivateChannelMessagesSettings(BaseModel):
    """Every message in Teams private and shared channels, through Microsoft Graph.

    Teams sends a bot only the posts there that @mention it. Top-level, not
    under `TeamsSettings`, as every feature setting is; only Teams reads it.
    """

    enabled: bool = Field(
        default=False,
        description=(
            "When True, Daimon subscribes to Microsoft Graph change notifications "
            "for each Teams private or shared channel it is in, where Teams sends "
            "the bot only the posts that @mention it. Replies in followed threads, "
            "replies to Daimon's messages and typed @<bot name> mentions then "
            "work there too, usually within seconds. Needs DAIMON_TEAMS__PUBLIC_URL "
            "(Graph posts to <public_url>/api/graph/notifications and "
            "/api/graph/lifecycle) and the application permission "
            "ChannelMessage.Read.All with tenant admin consent."
        ),
    )


class PrivacySettings(BaseModel):
    delete_enabled: bool = Field(
        default=True,
        description=(
            "Show account deletion in Discord, Slack, and Teams privacy panels and allow "
            "delete confirmations. Set DAIMON_PRIVACY__DELETE_ENABLED=false to pause "
            "deletion during an event; Policy and Export remain available."
        ),
    )


class ArtifactsSettings(BaseModel):
    """Optional private object storage for hosted-client artifacts."""

    backend: Literal["s3", "azure_blob"] = Field(
        default="s3",
        description="Artifact storage backend: s3 (default) or azure_blob.",
    )
    endpoint_url: HttpUrl = Field(
        description=(
            "S3-compatible endpoint or Azure Blob account URL "
            "(https://<account>.blob.core.windows.net); used for uploads and signed GET URLs."
        ),
    )
    bucket: str = Field(
        min_length=1,
        description="Private S3 bucket or Azure Blob container name; provision it before startup.",
    )
    access_key_id: SecretStr = Field(
        default=SecretStr(""), description="S3 access key id; required only for the s3 backend."
    )
    secret_access_key: SecretStr = Field(
        default=SecretStr(""), description="S3 secret access key; required only for the s3 backend."
    )
    azure_connection_string: SecretStr | None = Field(
        default=None,
        description=(
            "Local Azurite connection string only; BlobEndpoint must match the loopback "
            "endpoint_url. Leave unset on Azure; managed identity signs user-delegation SAS "
            "without account keys."
        ),
    )
    azure_client_id: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "User-assigned managed identity client id; required for azure_blob except local "
            "Azurite. ManagedIdentityCredential uses only this identity, with no environment "
            "service-principal or developer-login fallback."
        ),
    )
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

    @model_validator(mode="after")
    def validate_backend(self) -> ArtifactsSettings:
        if self.backend == "azure_blob":
            if self.access_key_id.get_secret_value() or self.secret_access_key.get_secret_value():
                raise ValueError(
                    "azure_blob artifacts reject S3 access_key_id and secret_access_key"
                )
            if (
                self.endpoint_url.query is not None
                or self.endpoint_url.fragment is not None
                or self.endpoint_url.username is not None
                or self.endpoint_url.password is not None
                or (
                    self.azure_connection_string is None
                    and self.endpoint_url.path not in {None, "/"}
                )
            ):
                raise ValueError(
                    "Azure endpoint_url must be an account URL without path, query or userinfo"
                )
        if self.backend == "s3":
            if (
                not self.access_key_id.get_secret_value()
                or not self.secret_access_key.get_secret_value()
            ):
                raise ValueError("s3 artifacts require access_key_id and secret_access_key")
            if self.azure_connection_string is not None:
                raise ValueError("azure_connection_string requires the azure_blob backend")
        elif self.azure_connection_string is not None:
            parts = {
                key.upper(): value
                for part in self.azure_connection_string.get_secret_value().split(";")
                if "=" in part
                for key, value in [part.split("=", 1)]
            }
            endpoint = parts.get("BLOBENDPOINT", "")
            parsed = urlsplit(endpoint)
            if (
                parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or endpoint.rstrip("/") != str(self.endpoint_url).rstrip("/")
                or "BLOBSECONDARYENDPOINT" in parts
                or "USEDEVELOPMENTSTORAGE" in parts
            ):
                raise ValueError("Azurite BlobEndpoint must match the loopback endpoint_url")
        elif self.endpoint_url.scheme != "https":
            raise ValueError("Azure Blob managed identity requires an HTTPS endpoint_url")
        elif not self.azure_client_id:
            raise ValueError("azure_blob artifacts require DAIMON_ARTIFACTS__AZURE_CLIENT_ID")
        return self


class DirectMessagePolicy(BaseModel):
    """Tenant recipient restrictions; platform membership is always required."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["members", "allowlist", "disabled"] = "members"
    recipient_ids: list[str] = Field(default_factory=list[str])

    def allows(self, recipient_id: str) -> bool:
        return self.mode == "members" or (
            self.mode == "allowlist" and recipient_id in self.recipient_ids
        )


class AgentIdentitySettings(BaseModel):
    enabled: bool = Field(
        default=False,
        description=(
            "Enable per-agent display names and avatars on Slack and Discord, agent name "
            "prefixes on Teams, and reply-to-agent routing on Discord. Off by default; "
            "set DAIMON_AGENT_IDENTITY__ENABLED=true after platform setup is ready."
        ),
    )
    excluded_discord_guild_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Discord guild IDs where agent identity stays off when enabled globally. "
            "Set DAIMON_AGENT_IDENTITY__EXCLUDED_DISCORD_GUILD_IDS to a JSON array of IDs. "
            "Default: [] (no guilds excluded)."
        ),
    )
    excluded_slack_team_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Slack workspace team IDs where agent identity stays off when enabled globally. "
            "Set DAIMON_AGENT_IDENTITY__EXCLUDED_SLACK_TEAM_IDS to a JSON array of IDs. "
            "Default: [] (no workspaces excluded)."
        ),
    )

    @field_validator("excluded_discord_guild_ids", "excluded_slack_team_ids", mode="before")
    @classmethod
    def _stringify_workspace_ids(cls, value: object) -> object:
        if isinstance(value, list):
            entries = cast(list[object], value)
            if all(isinstance(item, (str, int)) and not isinstance(item, bool) for item in entries):
                normalized: list[str] = []
                for item in entries:
                    normalized.append(str(item).strip())
                return normalized
            return entries
        return value


class TurnQueueSettings(BaseModel):
    """The queue a turn waits in when the concurrency caps are full (``DAIMON_TURN_QUEUE__*``).

    One in-memory queue per adapter process, served round-robin across tenants
    and FIFO within one. A queued turn shows the ordinary status card; nothing
    tells the person it is queued.
    """

    enabled: bool = Field(
        default=True,
        description=(
            "Queue turns that arrive while the global or the per-tenant concurrency cap "
            "is full, and start them as slots free. Set false to refuse them with the "
            "plain capacity notice instead."
        ),
    )
    max_per_tenant: int = Field(
        default=50,
        ge=0,
        description=(
            "Most turns one tenant (Discord server, Slack workspace, Teams tenant) may "
            "have waiting in one adapter process. A turn beyond it is refused with the "
            "plain capacity notice."
        ),
    )
    max_total: int = Field(
        default=500,
        ge=0,
        description=(
            "Most turns waiting across all tenants in one adapter process. A turn beyond "
            "it is refused with the plain capacity notice."
        ),
    )
    max_wait_s: float = Field(
        default=300.0,
        gt=0,
        description=(
            "Seconds a queued turn may wait for a slot. A safeguard only: the turn then "
            "leaves the queue and its card ends with the ordinary error."
        ),
    )


class FleetSettings(BaseModel):
    """Orchestrated worker fleets (``DAIMON_FLEET__*``). On by default.

    An orchestrator turn fans out to worker turns on any enabled backend. Their
    concurrency is a pool of its own, counted from the durable worker rows, so a
    fan-out never takes the chat turns' slots. Each agent still needs its own fleet
    grant and stays inside the tenant's budgets.
    """

    enabled: bool = Field(
        default=True,
        description=(
            "Allow orchestrator turns to start worker fleets. On by default. Emergency "
            "stop only: false refuses new fleets and new workers before they are funded, "
            "and holds finished-worker wakes until it is on again; the run's deadline "
            "sweep ends what is left. Fleets also stop when the turn path or either "
            "backend flag is off."
        ),
    )
    max_workers_per_run: int = Field(
        default=8,
        ge=1,
        description="Most workers one fleet run may start.",
    )
    max_concurrent_workers_per_tenant: int = Field(
        default=3,
        ge=1,
        description=(
            "Most worker turns one tenant may have starting or running at once, across "
            "all its fleet runs and processes. Separate from the chat turn cap."
        ),
    )
    max_concurrent_workers: int = Field(
        default=6,
        ge=1,
        description="Most worker turns one process runs at once, across all tenants.",
    )
    max_attempts: int = Field(
        default=2,
        ge=1,
        description=(
            "Most times in total a worker is started when it keeps being lost before "
            "its turn is sent. A worker whose turn was sent is reconciled, never resent."
        ),
    )


class PollableTool(BaseModel):
    """One operator-listed tool a watch may poll. The list is the only thing that allows a poll."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    server: str = Field(
        description=(
            '"daimon-mcp" for a native reader, or the https URL of an MCP server. Never a '
            "server name: names are chosen per agent."
        )
    )
    tool: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$", description="Exact tool name.")
    credential: Literal["agent", "owner", "none"] = Field(
        description='The one credential a poll may use; native readers use "none".'
    )
    returns_chat: bool = Field(
        description=(
            "Whether the tool can return chat or archive text Daimon posts. Required; "
            "refused for MCP servers, since only native readers drop Daimon's own rows."
        )
    )
    max_interval_floor_s: int = Field(
        default=300, ge=60, le=86400, description="Shortest poll interval allowed for this tool."
    )
    label: str = Field(default="", max_length=80, description="Shown on the approval card.")

    @field_validator("server")
    @classmethod
    def _canonical_server(cls, value: str) -> str:
        if value == DAIMON_SERVER_NAME:
            return value
        url = canonical_mcp_url(value)
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError('server must be "daimon-mcp" or an https URL without credentials')
        return url

    @model_validator(mode="after")
    def _reviewed(self) -> PollableTool:
        # Lazy: the catalogue reads these settings.
        from daimon.core.continuity.poll_tools import POLL_TOOLS

        if self.server == DAIMON_SERVER_NAME:
            if self.tool not in POLL_TOOLS or self.credential != "none":
                raise ValueError(
                    f"daimon-mcp/{self.tool} is not a reviewed native poll tool "
                    "with credential none"
                )
        elif self.returns_chat:
            raise ValueError(f"{self.tool} returns chat; MCP server tools that do cannot be polled")
        return self


def _default_pollable_tools() -> list[PollableTool]:
    return [
        PollableTool(
            server="daimon-mcp",
            tool=tool,
            credential="none",
            returns_chat=True,
            max_interval_floor_s=60,
            label=label,
        )
        for tool, label in (("read_thread", "Discord thread"), ("read_channel", "Discord channel"))
    ]


class WatchesSettings(BaseModel):
    @field_validator("executor_secret", mode="before")
    @classmethod
    def empty_executor_secret(cls, value: object) -> object:
        return None if value == "" else value

    executor_secret: SecretStr | None = Field(
        default=None,
        min_length=32,
        max_length=256,
        description=(
            "Private poll executor shared secret, also supplied as "
            "DAIMON_SIDECAR__EXECUTOR_SECRET. Unset disables executor transport. "
            "Never expose the private listener through the public proxy."
        ),
    )
    enabled: bool = Field(
        default=False,
        description=(
            "Enable reviewed Discord READ-tool polling and watch dispatch. "
            "Page-event watches are not yet delivered. "
            "Keep disabled "
            "until the execution-bound unattended origin grants, non-gateable cleanup "
            "and watch release are reviewed. "
            "The database global gate must also be explicitly enabled by an operator."
        ),
    )
    pollable_tools: Annotated[list[PollableTool], NoDecode] = Field(
        default_factory=_default_pollable_tools,
        description=(
            "JSON list of the tools a watch may poll in this deployment. Only a listed "
            "tool can be polled: a watch on a tool taken off the list pauses at its next "
            'poll and its owner is told. Each entry has server ("daimon-mcp" for a native '
            "reader, otherwise an https MCP server URL, never a server name), tool (exact "
            'name), credential ("none" for native readers; "agent" or "owner" for '
            "MCP servers), returns_chat (required: true only for native readers, which "
            "drop Daimon's own messages), max_interval_floor_s (shortest interval, at "
            "least 60, default 300) and label (shown on the approval card). Native "
            "entries must be read_thread or read_channel. MCP server entries are checked "
            "here but are not polled yet. A server's read-only annotation never allows a "
            "poll; only this list does. A watch below a raised floor polls at the "
            "floor. Default (also used when the value is empty): read_thread and "
            "read_channel on daimon-mcp, credential none, returns_chat true, floor 60. "
            "Set [] to allow no polls."
        ),
    )

    @field_validator("pollable_tools", mode="before")
    @classmethod
    def _parse_pollable_tools(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        if not value.strip():
            # A blanked variable means "use the default", never "allow nothing".
            return _default_pollable_tools()
        try:
            return json.loads(value)
        except json.JSONDecodeError as err:
            raise ValueError(
                f"DAIMON_WATCHES__POLLABLE_TOOLS is not valid JSON "
                f"(line {err.lineno}, column {err.colno})"
            ) from None

    @field_validator("pollable_tools")
    @classmethod
    def _unique_pollable_tools(cls, value: list[PollableTool]) -> list[PollableTool]:
        seen: set[tuple[str, str]] = set()
        for entry in value:
            if (entry.server, entry.tool) in seen:
                raise ValueError(f"{entry.server}/{entry.tool} is listed more than once")
            seen.add((entry.server, entry.tool))
        return value


class ComputeProfile(BaseModel):
    image: str = Field(
        description="Operator-reviewed thin MCP image reference, preferably a digest."
    )
    regions: list[str] = Field(default_factory=lambda: ["iad"], description="Allowed Fly regions.")
    cpu_kind: Literal["shared", "performance"] = Field(
        default="shared", description="Fly CPU class."
    )
    cpus: int = Field(default=2, gt=0, le=16, description="Fixed CPU count for this image profile.")
    memory_mb: int = Field(default=4096, ge=256, le=131072, description="Fixed RAM in MB.")


class ComputeSettings(BaseModel):
    enabled: bool = Field(
        default=False,
        description=(
            "Allow attended owners to launch Fly sessions. Disabling launch does not "
            "disable cleanup."
        ),
    )
    deployment_id: str = Field(
        default="",
        description=(
            "Stable unique deployment identifier fencing Fly resources. Required to use compute."
        ),
    )
    api_token: SecretStr | None = Field(
        default=None,
        repr=False,
        description=(
            "Fly API token, control plane and scheduler only. Never mounted in compute machines."
        ),
    )
    org_slug: str = Field(
        default="", description="Fly organization for this deployment's disposable sessions."
    )
    session_auth_key: SecretStr | None = Field(
        default=None,
        repr=False,
        description=(
            "Stable control-plane HMAC key deriving per-session MCP tokens. Never "
            "mounted on machines."
        ),
    )
    per_person_sessions: int = Field(
        default=2,
        gt=0,
        description=(
            "Maximum active sessions per person within a tenant, including pending deletion."
        ),
    )
    default_idle_ttl_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="Default idle lifetime in hours; bounded by max_ttl_hours.",
    )
    max_ttl_hours: int = Field(
        default=168,
        ge=1,
        le=168,
        description=(
            "Maximum launch or extension lifetime in hours. No dollar cap or metering is provided."
        ),
    )
    per_session_tasks: int = Field(
        default=4,
        gt=0,
        le=32,
        description=(
            "Maximum background commands running at once on one session. A background "
            "command may run for at most max_ttl_hours."
        ),
    )
    profiles: dict[str, ComputeProfile] = Field(
        default_factory=dict,
        description=(
            "Operator-managed image profiles. Empty means no images can be launched. "
            "No environment or credentials are copied from template apps."
        ),
    )


class LongJobsSettings(BaseModel):
    max_deadline_minutes: int = Field(
        default=70,
        gt=0,
        description=(
            "Operator hard ceiling for long-job deadlines in minutes. "
            "Durations above 70 min are not yet validated on MA."
        ),
    )
    max_usd: Decimal = Field(
        default=Decimal("20"),
        gt=0,
        allow_inf_nan=False,
        description=(
            "Operator hard ceiling in USD per long job; tenant and agent policy may only lower it."
        ),
    )
    per_tenant_running: int = Field(
        default=3, gt=0, description="Maximum launching and running long jobs per tenant."
    )
    per_owner_running: int = Field(
        default=2,
        gt=0,
        description="Maximum launching and running long jobs per owner within a tenant.",
    )


class RepoMemorySettings(BaseModel):
    enabled: bool = Field(
        default=False,
        description=(
            "Enable native repository memory tools and turn injection after a verified import."
        ),
    )
    legacy_mcp_server_names: tuple[str, ...] = Field(
        default=(),
        description=(
            "Legacy MCP server names whose repo_memory_list and repo_memory_view tools "
            "are disabled in sessions when native repository memory is enabled. "
            "Set these before cutover so the agent sees one memory source."
        ),
    )

    @field_validator("legacy_mcp_server_names")
    @classmethod
    def _legacy_names(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        if "daimon-mcp" in names:
            raise ValueError("The native daimon-mcp server cannot be a legacy memory server.")
        return names


def get_repo_memory_settings(settings: object) -> RepoMemorySettings:
    """Absent or undeclared settings keep the optional feature disabled."""
    value = getattr(settings, "repo_memory", None)
    return value if isinstance(value, RepoMemorySettings) else RepoMemorySettings()


class Settings(BaseSettings):
    compute: ComputeSettings = Field(default_factory=ComputeSettings)
    long_jobs: LongJobsSettings = Field(default_factory=LongJobsSettings)
    security_audit_retention_days: int = Field(
        default=90,
        ge=0,
        description=(
            "Security audit retention age in days, default 90. Operators must schedule "
            "`daimon audit prune TENANT_UUID` for each tenant (for example daily). "
            "The command deletes older events. Set 0 to explicitly retain events forever; "
            "privacy erasure and tenant deletion still apply."
        ),
    )
    completion_pings: dict[uuid.UUID, bool] = Field(
        default_factory=dict[uuid.UUID, bool],
        description=(
            "Per-tenant completion notification policy, keyed by tenant UUID. "
            "Discord and Slack acknowledge mentions independently of this setting and clear "
            "the acknowledgment when the turn settles. True adds a success check mark and "
            "posts the final answer as a fresh reply "
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
            "When a channel's budget is used up, the server admins "
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
            "Per-tenant table rendering, keyed by tenant UUID. Discord renders final "
            "Markdown tables as PNG unless the tenant is set to false. Slack renders "
            "native tables only when set to true. Set DAIMON_TABLE_RENDERING to a JSON object."
        ),
    )
    database: DatabaseSettings
    anthropic: AnthropicSettings = Field(default_factory=AnthropicSettings)
    privacy_policy_url: HttpUrl = Field(
        default=HttpUrl("https://github.com/pymc-labs/daimon/blob/main/PRIVACY.md"),
        description=(
            "URL rendered on the privacy panels' Policy button. "
            "Override via DAIMON_PRIVACY_POLICY_URL if you host your own policy page."
        ),
    )
    privacy: PrivacySettings = Field(default_factory=PrivacySettings)
    cli: CLISettings = Field(default_factory=CLISettings)
    log: LogSettings = Field(default_factory=LogSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    ops: OpsSettings = Field(default_factory=OpsSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    mcp_oauth: McpOAuthSettings = Field(default_factory=McpOAuthSettings)
    hub: HubSettings = Field(default_factory=HubSettings)
    agent_identity: AgentIdentitySettings = Field(default_factory=AgentIdentitySettings)
    environment: EnvironmentSettings = Field(default_factory=EnvironmentSettings)
    discord: DiscordSettings | None = None
    thread_participation: ThreadParticipationSettings = Field(
        default_factory=ThreadParticipationSettings,
        description="Replying in threads unprompted. See ThreadParticipationSettings.",
    )
    progress_plans: ProgressPlansSettings = Field(
        default_factory=ProgressPlansSettings,
        description="The agent's plan on the working card. See ProgressPlansSettings.",
    )
    slack: SlackSettings | None = None
    teams: TeamsSettings | None = None
    github: GithubSettings = Field(default_factory=GithubSettings)
    github_app: GithubAppSettings = Field(default_factory=GithubAppSettings)
    repo_memory: RepoMemorySettings = Field(default_factory=RepoMemorySettings)
    microsoft_accounts: MicrosoftAccountsSettings = Field(default_factory=MicrosoftAccountsSettings)
    crypto: CryptoSettings = Field(default_factory=CryptoSettings)
    credentials: CredentialsSettings = Field(default_factory=CredentialsSettings)
    selfhosted: SelfHostedSettings = Field(default_factory=SelfHostedSettings)
    gemini: GeminiSettings = Field(default_factory=GeminiSettings)
    notebook: NotebookSettings = Field(default_factory=NotebookSettings)
    report_host: ReportHostSettings = Field(default_factory=ReportHostSettings)
    sentry: SentrySettings = Field(default_factory=SentrySettings)
    billing: BillingSettings = Field(default_factory=BillingSettings)
    support: SupportSettings = Field(default_factory=SupportSettings)
    turn: TurnSettings = Field(default_factory=TurnSettings)
    thread_naming: ThreadNamingSettings = Field(default_factory=ThreadNamingSettings)
    turn_queue: TurnQueueSettings = Field(default_factory=TurnQueueSettings)
    fleet: FleetSettings = Field(default_factory=FleetSettings)
    watches: WatchesSettings = Field(default_factory=WatchesSettings)
    private_channel_messages: PrivateChannelMessagesSettings = Field(
        default_factory=PrivateChannelMessagesSettings
    )
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
            "Optional private S3-compatible or Azure Blob store for signed chart links. "
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

    def microsoft_accounts_missing(self) -> tuple[str, ...]:
        """Settings Connect Microsoft still needs; empty when it is on or wholly unset.

        A partial setup leaves the feature off with one startup warning rather
        than failing every service, like the other optional integrations.
        """
        ms = self.microsoft_accounts
        if not ms.touched:
            return ()
        wanted = (
            ("DAIMON_MICROSOFT_ACCOUNTS__CLIENT_ID", ms.client_id is not None),
            ("DAIMON_MICROSOFT_ACCOUNTS__CLIENT_SECRET", ms.client_secret is not None),
            ("DAIMON_MICROSOFT_ACCOUNTS__TENANT_ID", ms.tenant_id is not None),
            ("DAIMON_CRYPTO__KEYS", bool(self.crypto.keys)),
            ("DAIMON_MCP__PUBLIC_URL", self.mcp.public_url is not None),
        )
        return tuple(name for name, present in wanted if not present)

    @property
    def configured_features(self) -> frozenset[SettingsFeature]:
        """Optional features set up here; a default skill can require one (`SkillRef.requires`).

        Microsoft accounts count on `configured` alone, as for `microsoft_send`.
        """
        return (
            frozenset({"microsoft_accounts"}) if self.microsoft_accounts.configured else frozenset()
        )

    @property
    def microsoft_accounts_enabled(self) -> bool:
        """Whether the MCP server offers Connect Microsoft and the `microsoft_*` tools.

        Turn processes ask before `microsoft_send` on `configured` alone, so
        they need no crypto keys or public URL to agree.
        """
        return self.microsoft_accounts.touched and not self.microsoft_accounts_missing()

    @model_validator(mode="after")
    def _private_channel_messages_need_a_public_url(self) -> Settings:
        teams = self.teams
        if self.private_channel_messages.enabled and teams is not None and teams.public_url is None:
            raise ValueError(
                "DAIMON_PRIVATE_CHANNEL_MESSAGES__ENABLED needs DAIMON_TEAMS__PUBLIC_URL"
            )
        return self

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
    settings = Settings(_env_file=_env_file)  # pyright: ignore[reportCallIssue]
    if settings.turn.path == "mux" and settings.turn.channel_backends:
        from daimon.core.turn.runtime_manifest import validate_configured_runtime_manifest

        validate_configured_runtime_manifest()
    return settings


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


class _TurnSettingsSource(BaseSettings):
    """Turn-only settings for callers that already own their SDK client."""

    turn: TurnSettings = Field(default_factory=TurnSettings)
    model_config = SettingsConfigDict(
        env_prefix="DAIMON_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )


def load_turn_settings(*, _env_file: str | None = ".env") -> TurnSettings:
    """Read the same turn flag without unrelated API/database configuration."""
    turn = _TurnSettingsSource(_env_file=_env_file).turn  # pyright: ignore[reportCallIssue]
    if turn.path == "mux" and turn.channel_backends:
        from daimon.core.turn.runtime_manifest import validate_configured_runtime_manifest

        validate_configured_runtime_manifest()
    return turn


class _WatchesSettingsSource(BaseSettings):
    watches: WatchesSettings = Field(default_factory=WatchesSettings)
    model_config = SettingsConfigDict(
        env_prefix="DAIMON_", env_nested_delimiter="__", extra="ignore", hide_input_in_errors=True
    )


def load_watches_settings() -> WatchesSettings:
    """Read the producer gate without loading unrelated credentials or local dotenv files."""
    return _WatchesSettingsSource().watches


class _SelfHostedSettingsSource(BaseSettings):
    """Admission settings without unrelated API or database configuration."""

    selfhosted: SelfHostedSettings = Field(default_factory=SelfHostedSettings)
    model_config = SettingsConfigDict(
        env_prefix="DAIMON_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )


def load_selfhosted_settings(*, _env_file: str | None = ".env") -> SelfHostedSettings:
    """Read the operator admission switch without constructing provider clients."""
    return _SelfHostedSettingsSource(_env_file=_env_file).selfhosted  # pyright: ignore[reportCallIssue]


class _BillingSettingsSource(BaseSettings):
    """Tariff discovery without unrelated provider or database configuration."""

    billing: BillingSettings = Field(default_factory=BillingSettings)
    model_config = SettingsConfigDict(
        env_prefix="DAIMON_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )


def load_billing_settings(*, _env_file: str | None = ".env") -> BillingSettings:
    """Read billing policy without constructing a host or provider clients."""
    return _BillingSettingsSource(_env_file=_env_file).billing  # pyright: ignore[reportCallIssue]
