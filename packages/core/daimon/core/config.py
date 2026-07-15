"""Nested `pydantic-settings` for daimon-core. Constructed via `load_settings()`.

Never import a module-level settings singleton — callers construct once at the
edge (CLI entrypoint, test fixture) and inject downstream.
"""

from __future__ import annotations

import base64
import binascii
import os
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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


class DiscordSettings(BaseModel):
    """Discord adapter config.

    Optional so non-Discord deployments keep working. The ``__main__.py``
    entrypoint validates presence at boot time.
    """

    bot_token: SecretStr = Field(
        description="Discord bot token. Required to run the Discord adapter.",
    )
    max_concurrent_turns_per_tenant: int = Field(
        default=3,
        description=(
            "Maximum number of agent turns a single tenant (Discord guild) may "
            "have in flight at once. Caps one noisy guild from starving others "
            "on the shared Anthropic key."
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
    per_caller_thread_sessions: bool = Field(
        default=True,
        description=(
            "When True (default), each Discord thread keeps a separate agent "
            "session per calling user, so no user inherits another user's "
            "session identity or permissions in a shared thread. When False, "
            "a single session is shared by every caller in the thread — a "
            "legacy fallback, not recommended for production."
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
    health_port: int = Field(
        default=8083,
        description=(
            "Port for the Slack process's liveness endpoint. Must not collide "
            "with the mcp process (8080), the discord process (8081), or the "
            "scheduler process (8082) — all process groups share one host."
        ),
    )
    dev_allow_all_admin: bool = Field(
        default=False,
        description=(
            "Testing-only flag: when True, treats every Slack user as a "
            "workspace admin, bypassing the normal admin lookup, so a "
            "non-admin tester can exercise agent CRUD on a test deployment. "
            "Must stay unset in production."
        ),
    )


class GithubSettings(BaseModel):
    """GitHub repo-auth config (App-or-PAT; no OAuth flow). All fields are
    optional so deployments without GitHub App or PAT config keep working.

    Cloning via the GitHub App requires only app_id + app_private_key.
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


class CryptoSettings(BaseModel):
    """MultiFernet keys for at-rest token encryption.

    A single deployment ships one key; rotation means prepending a new key.
    Each key must be a Fernet.generate_key()-style base64-urlsafe 32-byte
    string. Empty default lets deployments without any encrypted credentials
    boot without crypto config.
    """

    keys: tuple[SecretStr, ...] = Field(
        default=(),
        description=(
            "Ordered tuple of Fernet keys used to encrypt/decrypt stored "
            "credentials. The first key encrypts new values; older keys "
            "remain valid for decrypting existing ciphertext during rotation."
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
        default=Decimal("5.00"),
        description=(
            "USD credit automatically seeded when a guild/workspace is "
            "provisioned, so a freshly-installed tenant can chat immediately "
            "on trial credit before paying. Set to 0 to require payment "
            "before use."
        ),
    )


class Settings(BaseSettings):
    database: DatabaseSettings
    anthropic: AnthropicSettings
    privacy_policy_url: HttpUrl = Field(
        default=HttpUrl("https://daimon.dev/privacy"),
        description=(
            "URL rendered on the Discord and Slack privacy panels' Policy button. "
            "Override via DAIMON_PRIVACY_POLICY_URL if you host your own policy page."
        ),
    )
    cli: CLISettings = Field(default_factory=CLISettings)
    log: LogSettings = Field(default_factory=LogSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    discord: DiscordSettings | None = None
    slack: SlackSettings | None = None
    github: GithubSettings = Field(default_factory=GithubSettings)
    crypto: CryptoSettings = Field(default_factory=CryptoSettings)
    credentials: CredentialsSettings = Field(default_factory=CredentialsSettings)
    gemini: GeminiSettings = Field(default_factory=GeminiSettings)
    notebook: NotebookSettings = Field(default_factory=NotebookSettings)
    sentry: SentrySettings = Field(default_factory=SentrySettings)
    billing: BillingSettings = Field(default_factory=BillingSettings)
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
    )


def load_settings(*, _env_file: str | None = ".env") -> Settings:
    """Construct a `Settings` from the live process env + optional `.env` file.

    `_env_file` exists to give tests a way to disable `.env` loading
    (`_env_file=None`) so they only see `monkeypatch.setenv` values.
    """
    return Settings(_env_file=_env_file)  # pyright: ignore[reportCallIssue]
