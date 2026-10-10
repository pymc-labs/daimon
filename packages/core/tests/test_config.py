from __future__ import annotations

import base64
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from daimon.core.config import (
    AnthropicSettings,
    ArtifactsSettings,
    DatabaseSettings,
    DiscordSettings,
    HubSettings,
    McpSettings,
    Settings,
    SlackSettings,
    TeamsSettings,
    load_settings,
)
from pydantic import HttpUrl, PostgresDsn, SecretStr, ValidationError


def test_privacy_delete_flag_defaults_on_and_reads_nested_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.delenv("DAIMON_PRIVACY__DELETE_ENABLED", raising=False)
    assert load_settings(_env_file=None).privacy.delete_enabled is True
    monkeypatch.setenv("DAIMON_PRIVACY__DELETE_ENABLED", "false")
    assert load_settings(_env_file=None).privacy.delete_enabled is False


def test_discord_stale_card_age_exceeds_turn_ceiling_and_attempts_are_bounded() -> None:
    from daimon.core.turn.ceiling import TURN_CEILING_S

    defaults = DiscordSettings(bot_token=SecretStr("test"))
    assert defaults.turn_card_unrecoverable_after_s == 86400
    assert defaults.turn_card_unrecoverable_after_attempts == 3
    with pytest.raises(ValidationError):
        DiscordSettings(
            bot_token=SecretStr("test"), turn_card_unrecoverable_after_s=int(TURN_CEILING_S) + 899
        )
    with pytest.raises(ValidationError):
        DiscordSettings(bot_token=SecretStr("test"), turn_card_unrecoverable_after_attempts=1)
    assert (
        DiscordSettings(
            bot_token=SecretStr("test"), turn_card_unrecoverable_after_s=int(TURN_CEILING_S) + 900
        ).turn_card_unrecoverable_after_s
        == int(TURN_CEILING_S) + 900
    )


def test_agent_identity_switch_is_off_by_default_and_reads_nested_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.delenv("DAIMON_AGENT_IDENTITY__ENABLED", raising=False)
    assert not load_settings(_env_file=None).agent_identity.enabled
    monkeypatch.setenv("DAIMON_AGENT_IDENTITY__ENABLED", "true")
    assert load_settings(_env_file=None).agent_identity.enabled


def test_agent_identity_workspace_exclusions_read_json_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.delenv("DAIMON_AGENT_IDENTITY__EXCLUDED_DISCORD_GUILD_IDS", raising=False)
    monkeypatch.delenv("DAIMON_AGENT_IDENTITY__EXCLUDED_SLACK_TEAM_IDS", raising=False)
    default = load_settings(_env_file=None).agent_identity
    assert default.excluded_discord_guild_ids == []
    assert default.excluded_slack_team_ids == []
    monkeypatch.setenv("DAIMON_AGENT_IDENTITY__EXCLUDED_DISCORD_GUILD_IDS", '[123, " 456 "]')
    monkeypatch.setenv("DAIMON_AGENT_IDENTITY__EXCLUDED_SLACK_TEAM_IDS", '[" T123 "]')
    configured = load_settings(_env_file=None).agent_identity
    assert configured.excluded_discord_guild_ids == ["123", "456"]
    assert configured.excluded_slack_team_ids == ["T123"]


def test_load_settings_parses_nested_delimiter_when_env_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DAIMON_DATABASE__URL",
        "postgresql+asyncpg://u:p@h:5432/d",
    )
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_CLI__LOCAL_USER", "alice")
    monkeypatch.setenv("DAIMON_LOG__LEVEL", "DEBUG")

    settings = load_settings(_env_file=None)

    assert str(settings.database.url) == "postgresql+asyncpg://u:p@h:5432/d"
    assert settings.anthropic.api_key.get_secret_value() == "sk-test"
    assert settings.cli.local_user == "alice"
    assert settings.log.level == "DEBUG"


def test_database_pool_settings_parse_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_DATABASE__POOL_SIZE", "20")
    monkeypatch.setenv("DAIMON_DATABASE__MAX_OVERFLOW", "10")
    monkeypatch.setenv("DAIMON_DATABASE__POOL_TIMEOUT", "12.5")

    database = load_settings(_env_file=None).database

    assert (database.pool_size, database.max_overflow, database.pool_timeout) == (20, 10, 12.5)


def test_ops_webhook_is_optional_and_reads_nested_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.delenv("DAIMON_OPS__ALERT_WEBHOOK_URL", raising=False)
    assert load_settings(_env_file=None).ops.alert_webhook_url is None

    monkeypatch.setenv("DAIMON_OPS__ALERT_WEBHOOK_URL", "https://discord.com/api/webhooks/test")
    webhook = load_settings(_env_file=None).ops.alert_webhook_url
    assert webhook is not None
    assert webhook.get_secret_value() == "https://discord.com/api/webhooks/test"


def test_load_settings_defaults_cli_local_user_to_env_user_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USER", "bob")
    monkeypatch.delenv("DAIMON_CLI__LOCAL_USER", raising=False)
    monkeypatch.setenv(
        "DAIMON_DATABASE__URL",
        "postgresql+asyncpg://u:p@h:5432/d",
    )
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")

    settings = load_settings(_env_file=None)

    assert settings.cli.local_user == "bob"


def test_load_settings_raises_when_required_fields_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "DAIMON_DATABASE__URL",
        "DAIMON_ANTHROPIC__API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ValidationError):
        load_settings(_env_file=None)


def test_load_settings_accepts_explicit_overrides_when_passed() -> None:
    settings = Settings.model_validate(
        {
            "database": {"url": "postgresql+asyncpg://u:p@h:5432/d"},
            "anthropic": {"api_key": "sk-test"},
            "cli": {"local_user": "carol"},
        }
    )
    assert settings.cli.local_user == "carol"


def test_artifacts_settings_are_off_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")

    assert load_settings(_env_file=None).artifacts is None


def test_artifacts_settings_parse_nested_env_with_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_ARTIFACTS__ENDPOINT_URL", "https://bucket.example.test")
    monkeypatch.setenv("DAIMON_ARTIFACTS__BUCKET", "private-artifacts")
    monkeypatch.setenv("DAIMON_ARTIFACTS__ACCESS_KEY_ID", "access-key")
    monkeypatch.setenv("DAIMON_ARTIFACTS__SECRET_ACCESS_KEY", "secret-key")
    monkeypatch.setenv("DAIMON_ARTIFACTS__REGION", "auto")

    artifacts = load_settings(_env_file=None).artifacts

    assert isinstance(artifacts, ArtifactsSettings)
    assert artifacts.bucket == "private-artifacts"
    assert artifacts.region == "auto"
    assert artifacts.url_ttl_seconds == 600
    assert artifacts.embed_images is True


def test_artifacts_image_embedding_can_be_disabled_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_ARTIFACTS__ENDPOINT_URL", "https://bucket.example.test")
    monkeypatch.setenv("DAIMON_ARTIFACTS__BUCKET", "private-artifacts")
    monkeypatch.setenv("DAIMON_ARTIFACTS__ACCESS_KEY_ID", "access-key")
    monkeypatch.setenv("DAIMON_ARTIFACTS__SECRET_ACCESS_KEY", "secret-key")
    monkeypatch.setenv("DAIMON_ARTIFACTS__EMBED_IMAGES", "false")

    artifacts = load_settings(_env_file=None).artifacts

    assert artifacts is not None
    assert artifacts.embed_images is False


@pytest.mark.parametrize("ttl", [0, 86_401])
def test_artifacts_url_ttl_rejects_values_outside_bounds(ttl: int) -> None:
    with pytest.raises(ValidationError):
        ArtifactsSettings(
            endpoint_url="https://bucket.example.test",
            bucket="private-artifacts",
            access_key_id="access-key",
            secret_access_key="secret-key",
            url_ttl_seconds=ttl,
        )


def test_mcp_settings_both_unset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal deployments keep working with MCP subtree fully unset."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    settings = load_settings(_env_file=None)
    assert settings.mcp.jwt_secret is None, "jwt_secret optional when unset"
    assert settings.mcp.public_url is None, "public_url optional when unset"


def test_mcp_settings_parsed_from_nested_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_MCP__JWT_SECRET", "a" * 32)
    monkeypatch.setenv("DAIMON_MCP__PUBLIC_URL", "https://mcp.example.com/mcp")
    settings = load_settings(_env_file=None)
    assert settings.mcp.jwt_secret is not None
    assert settings.mcp.jwt_secret.get_secret_value() == "a" * 32
    assert str(settings.mcp.public_url) == "https://mcp.example.com/mcp"


def test_mcp_app_root_url_strips_mcp_suffix() -> None:
    """app_root_url drops the /mcp protocol segment so app-root routes
    (/oauth/*, /cli/*, /healthz) resolve — public_url points at /mcp."""
    settings = McpSettings(public_url=HttpUrl("https://mcp.example.com/mcp"))
    assert settings.app_root_url == "https://mcp.example.com", (
        "app_root_url must strip the trailing /mcp so /oauth/github/start is reachable"
    )


def test_mcp_app_root_url_noop_without_mcp_suffix() -> None:
    """When public_url has no /mcp path, app_root_url is the host unchanged."""
    settings = McpSettings(public_url=HttpUrl("https://mcp.example.com"))
    assert settings.app_root_url == "https://mcp.example.com", (
        "no /mcp suffix to strip — must return the base unchanged"
    )


def test_mcp_app_root_url_none_when_public_url_unset() -> None:
    """No public_url → no fabricated base."""
    assert McpSettings().app_root_url is None, "app_root_url is None when public_url unset"


def test_gemini_settings_unset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """GeminiSettings.api_key is None when env unset."""
    monkeypatch.delenv("DAIMON_GEMINI__API_KEY", raising=False)
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    settings = load_settings(_env_file=None)
    assert settings.gemini.api_key is None, "gemini.api_key optional when unset"


def test_gemini_settings_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """DAIMON_GEMINI__API_KEY populates settings.gemini.api_key."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_GEMINI__API_KEY", "gem-test-key")
    settings = load_settings(_env_file=None)
    assert settings.gemini.api_key is not None
    assert settings.gemini.api_key.get_secret_value() == "gem-test-key"


def test_mcp_file_store_dir_overrides_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """DAIMON_MCP__FILE_STORE_DIR overrides the default tempdir path."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_MCP__FILE_STORE_DIR", "/var/lib/daimon/mcp-files")
    settings = load_settings(_env_file=None)
    assert settings.mcp.file_store_dir == Path("/var/lib/daimon/mcp-files")


def test_defaults_root_default_is_relative_defaults_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single source of truth for defaults/ path lives on Settings."""
    monkeypatch.delenv("DAIMON_DEFAULTS_ROOT", raising=False)
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    settings = load_settings(_env_file=None)
    assert settings.defaults_root == Path("defaults"), (
        "default defaults_root should be Path('defaults') relative to cwd"
    )


def test_notebook_settings_max_attachment_bytes_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NotebookSettings.max_attachment_bytes defaults to 10 MiB."""
    monkeypatch.delenv("DAIMON_NOTEBOOK__MAX_ATTACHMENT_BYTES", raising=False)
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    settings = load_settings(_env_file=None)
    assert settings.notebook.max_attachment_bytes == 10 * 1024 * 1024, (
        "default per-attachment cap should be 10 MiB"
    )


def test_load_settings_max_attachment_bytes_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DAIMON_NOTEBOOK__MAX_ATTACHMENT_BYTES overrides the default."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_NOTEBOOK__MAX_ATTACHMENT_BYTES", "1234")
    settings = load_settings(_env_file=None)
    assert settings.notebook.max_attachment_bytes == 1234, "env var must override the default cap"


def test_defaults_root_overrides_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """DAIMON_DEFAULTS_ROOT overrides the field (top-level Settings, no nested delim)."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_DEFAULTS_ROOT", "/custom/path")
    settings = load_settings(_env_file=None)
    assert settings.defaults_root == Path("/custom/path"), (
        "DAIMON_DEFAULTS_ROOT must override defaults_root"
    )


# --- BillingSettings tests (TOPUP-01) ---


def test_billing_defaults_when_no_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """billing.markup and billing.signup_credit default correctly when unset."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.delenv("DAIMON_BILLING__MARKUP", raising=False)
    monkeypatch.delenv("DAIMON_BILLING__SIGNUP_CREDIT", raising=False)
    settings = load_settings(_env_file=None)
    assert settings.billing.markup == Decimal("1.0"), (
        "default markup must be Decimal('1.0') (pass-through)"
    )
    assert settings.billing.signup_credit == Decimal("10.00"), (
        "default signup_credit must be >0 (Decimal('10.00')) so one-click works on trial "
        "credit before payment; operators set 0 for pay-first"
    )


def test_billing_markup_parsed_from_env_as_decimal(monkeypatch: pytest.MonkeyPatch) -> None:
    """DAIMON_BILLING__MARKUP is parsed as Decimal, not float."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_BILLING__MARKUP", "1.25")
    settings = load_settings(_env_file=None)
    assert settings.billing.markup == Decimal("1.25"), (
        "DAIMON_BILLING__MARKUP=1.25 must parse to Decimal('1.25')"
    )
    assert isinstance(settings.billing.markup, Decimal), (
        "billing.markup must be a Decimal instance, not float"
    )


def test_billing_signup_credit_parsed_from_env_as_decimal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DAIMON_BILLING__SIGNUP_CREDIT is parsed as Decimal."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_BILLING__SIGNUP_CREDIT", "5.00")
    settings = load_settings(_env_file=None)
    assert settings.billing.signup_credit == Decimal("5.00"), (
        "DAIMON_BILLING__SIGNUP_CREDIT=5.00 must parse to Decimal('5.00')"
    )
    assert isinstance(settings.billing.signup_credit, Decimal), (
        "billing.signup_credit must be a Decimal instance, not float"
    )


def test_notebook_settings_max_source_bytes_defaults_to_one_mib() -> None:
    from daimon.core.config import NotebookSettings

    s = NotebookSettings()
    assert s.max_source_bytes == 1_048_576, (
        "default source budget is 1 MiB, mirroring the host ceiling"
    )


def test_discord_health_port_defaults_to_8081(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discord liveness port defaults to 8081 (distinct from mcp 8080 / scheduler 8082)."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_DISCORD__BOT_TOKEN", "discord-token")
    settings = load_settings(_env_file=None)
    assert settings.discord is not None, "discord subtree present when bot_token is set"
    assert settings.discord.health_port == 8081, "health_port defaults to 8081"


def test_discord_ignores_the_removed_per_caller_thread_sessions_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deployments that still set the removed variable keep booting."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_DISCORD__BOT_TOKEN", "discord-token")
    monkeypatch.setenv("DAIMON_DISCORD__PER_CALLER_THREAD_SESSIONS", "false")
    settings = load_settings(_env_file=None)
    assert settings.discord is not None, "a leftover removed setting must not break loading"
    assert not hasattr(settings.discord, "per_caller_thread_sessions"), (
        "the removed setting must not come back as a field"
    )


def test_discord_health_port_parsed_from_nested_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """DAIMON_DISCORD__HEALTH_PORT overrides the liveness port."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_DISCORD__BOT_TOKEN", "discord-token")
    monkeypatch.setenv("DAIMON_DISCORD__HEALTH_PORT", "9091")
    settings = load_settings(_env_file=None)
    assert settings.discord is not None, "discord subtree present when bot_token is set"
    assert settings.discord.health_port == 9091, (
        "health_port parses from DAIMON_DISCORD__HEALTH_PORT"
    )


# --- GithubSettings tarball size caps ---


def test_github_settings_max_tarball_bytes_defaults_to_50_mib() -> None:
    from daimon.core.config import GithubSettings

    settings = GithubSettings()
    assert settings.max_tarball_bytes == 50 * 1024 * 1024, (
        "default raw tarball cap should be 50 MiB"
    )


def test_github_settings_max_tarball_decompressed_bytes_defaults_to_200_mib() -> None:
    from daimon.core.config import GithubSettings

    settings = GithubSettings()
    assert settings.max_tarball_decompressed_bytes == 200 * 1024 * 1024, (
        "default decompressed tarball cap should be 200 MiB"
    )


def test_github_settings_max_tarball_bytes_env_override_to_zero_disables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DAIMON_GITHUB__MAX_TARBALL_BYTES=0 must load as 0 (disables the guard)."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_GITHUB__MAX_TARBALL_BYTES", "0")
    settings = load_settings(_env_file=None)
    assert settings.github.max_tarball_bytes == 0, (
        "env var 0 must load as max_tarball_bytes == 0 (disables the cap)"
    )


# --- GithubSettings app_private_key base64-tolerant loading ---

_FAKE_PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\nlmnop\n-----END RSA PRIVATE KEY-----\n"


def test_github_app_private_key_raw_pem_passes_through() -> None:
    """A raw multi-line PEM is stored unchanged (Fly / Cloud Run delivery)."""
    from daimon.core.config import GithubSettings

    settings = GithubSettings(app_private_key=_FAKE_PEM)
    assert settings.app_private_key is not None, "key should be set"
    assert settings.app_private_key.get_secret_value() == _FAKE_PEM, (
        "raw PEM must be stored byte-for-byte, unchanged"
    )


def test_github_app_private_key_base64_decodes_to_pem() -> None:
    """A base64-encoded PEM (single-line, env_file-safe) decodes back to the PEM."""
    import base64

    from daimon.core.config import GithubSettings

    encoded = base64.b64encode(_FAKE_PEM.encode()).decode()
    assert "-----BEGIN" not in encoded, "base64 must not contain PEM delimiters"
    settings = GithubSettings(app_private_key=encoded)
    assert settings.app_private_key is not None, "key should be set"
    assert settings.app_private_key.get_secret_value() == _FAKE_PEM, (
        "a base64-encoded PEM must decode to the original PEM"
    )


def test_github_app_private_key_none_stays_none() -> None:
    """Unset key remains None (deployments without a GitHub App)."""
    from daimon.core.config import GithubSettings

    settings = GithubSettings()
    assert settings.app_private_key is None, "unset private key must remain None"


# --- GithubSettings app_slug validation ---


def test_github_app_slug_defaults_to_none() -> None:
    """Unset app_slug remains None (deployment with no GitHub App install link)."""
    from daimon.core.config import GithubSettings

    settings = GithubSettings()
    assert settings.app_slug is None, "unset app_slug must remain None"


def test_github_app_slug_accepts_valid_slug() -> None:
    """A slug of letters, digits and hyphens is accepted unchanged."""
    from daimon.core.config import GithubSettings

    settings = GithubSettings(app_slug="acme-daimon-42")
    assert settings.app_slug == "acme-daimon-42", "valid slug must be stored unchanged"


def test_github_app_slug_rejects_empty_string() -> None:
    """An empty app_slug is rejected rather than silently accepted."""
    from daimon.core.config import GithubSettings

    with pytest.raises(ValidationError):
        GithubSettings(app_slug="")


def test_github_app_slug_rejects_slash() -> None:
    """A slug containing '/' is rejected (a URL fragment, not a slug)."""
    from daimon.core.config import GithubSettings

    with pytest.raises(ValidationError):
        GithubSettings(app_slug="acme/daimon")


def test_github_app_slug_rejects_dot() -> None:
    """A slug containing '.' is rejected."""
    from daimon.core.config import GithubSettings

    with pytest.raises(ValidationError):
        GithubSettings(app_slug="acme.daimon")


def test_github_app_slug_rejects_at_sign() -> None:
    """A slug containing '@' is rejected."""
    from daimon.core.config import GithubSettings

    with pytest.raises(ValidationError):
        GithubSettings(app_slug="acme@daimon")


def test_github_app_slug_rejects_leading_hyphen() -> None:
    """A slug with a leading hyphen is rejected."""
    from daimon.core.config import GithubSettings

    with pytest.raises(ValidationError):
        GithubSettings(app_slug="-acme-daimon")


def test_github_app_slug_rejects_over_length_cap() -> None:
    """A slug over the length cap is rejected."""
    from daimon.core.config import GithubSettings

    with pytest.raises(ValidationError):
        GithubSettings(app_slug="a" * 101)


# --- SlackSettings tests ---


def test_slack_settings_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-Slack deployments boot unchanged — slack block is None with no DAIMON_SLACK__* vars."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.delenv("DAIMON_SLACK__SIGNING_SECRET", raising=False)
    monkeypatch.delenv("DAIMON_SLACK__APP_TOKEN", raising=False)
    monkeypatch.delenv("DAIMON_SLACK__CLIENT_ID", raising=False)
    monkeypatch.delenv("DAIMON_SLACK__CLIENT_SECRET", raising=False)
    settings = load_settings(_env_file=None)
    assert settings.slack is None, "slack block must be None with no DAIMON_SLACK__* vars"


def test_slack_settings_parsed_from_nested_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """DAIMON_SLACK__* vars construct settings.slack with the right field values."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_SLACK__SIGNING_SECRET", "s" * 32)
    monkeypatch.setenv("DAIMON_SLACK__APP_TOKEN", "xapp-test")
    settings = load_settings(_env_file=None)
    assert settings.slack is not None, "slack block must be present when DAIMON_SLACK__* are set"
    assert settings.slack.app_token.get_secret_value() == "xapp-test", (
        "app_token must parse from DAIMON_SLACK__APP_TOKEN"
    )


def test_slack_settings_health_port_defaults_to_8083(monkeypatch: pytest.MonkeyPatch) -> None:
    """STURN-01: slack liveness port defaults to 8083 (distinct from mcp 8080 / discord 8081 / scheduler 8082)."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_SLACK__SIGNING_SECRET", "s" * 32)
    monkeypatch.setenv("DAIMON_SLACK__APP_TOKEN", "xapp-test")
    settings = load_settings(_env_file=None)
    assert settings.slack is not None, "slack block must be present when DAIMON_SLACK__* are set"
    assert settings.slack.health_port == 8083, (
        "health_port must default to 8083 (collision-free: mcp=8080, discord=8081, scheduler=8082)"
    )


# --- privacy_policy_url (CLEAN-05) ---


def test_privacy_policy_url_defaults_to_in_repo_doc_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No override → operator gets the in-repo PRIVACY.md, not a dead domain."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.delenv("DAIMON_PRIVACY_POLICY_URL", raising=False)
    settings = load_settings(_env_file=None)
    assert (
        str(settings.privacy_policy_url)
        == "https://github.com/pymc-labs/daimon/blob/main/PRIVACY.md"
    ), "default privacy_policy_url must point at the in-repo PRIVACY.md"


def test_privacy_policy_url_overrides_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """DAIMON_PRIVACY_POLICY_URL lets an operator point at their own policy page."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_PRIVACY_POLICY_URL", "https://example.com/privacy")
    settings = load_settings(_env_file=None)
    assert str(settings.privacy_policy_url) == "https://example.com/privacy", (
        "DAIMON_PRIVACY_POLICY_URL must override the default privacy_policy_url"
    )


def test_slack_settings_max_concurrent_turns_per_tenant_defaults_to_3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STURN-06: per-tenant turn cap defaults to 3, mirroring DiscordSettings."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_SLACK__SIGNING_SECRET", "s" * 32)
    monkeypatch.setenv("DAIMON_SLACK__APP_TOKEN", "xapp-test")
    settings = load_settings(_env_file=None)
    assert settings.slack is not None, "slack block must be present when DAIMON_SLACK__* are set"
    assert settings.slack.max_concurrent_turns_per_tenant == 3, (
        "max_concurrent_turns_per_tenant must default to 3 (STURN-06 per-tenant cap)"
    )


# --- HubSettings tests ---


def test_hub_settings_default_to_unconfigured() -> None:
    settings = Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
    )
    assert settings.hub.slack_configured is False, (
        f"slack hub must be unconfigured by default, got {settings.hub!r}"
    )
    assert settings.hub.discord_configured is False, (
        f"discord hub must be unconfigured by default, got {settings.hub!r}"
    )


def test_hub_settings_platform_configured_requires_both_id_and_secret() -> None:
    hub = HubSettings(discord_client_id="123")
    assert hub.discord_configured is False, "client id alone must not count as configured"
    hub = HubSettings(discord_client_id="123", discord_client_secret=SecretStr("s"))
    assert hub.discord_configured is True, "id plus secret must count as configured"


def test_hub_settings_accepts_a_fernet_shaped_signing_key() -> None:
    key = Fernet.generate_key().decode()
    hub = HubSettings(jwt_signing_key=SecretStr(key))
    assert hub.jwt_signing_key is not None and hub.jwt_signing_key.get_secret_value() == key, (
        f"a Fernet-generated key must be accepted unchanged, got {hub.jwt_signing_key!r}"
    )


def test_hub_settings_rejects_a_passphrase_signing_key() -> None:
    with pytest.raises(ValidationError, match="DAIMON_HUB__JWT_SIGNING_KEY"):
        HubSettings(jwt_signing_key=SecretStr("hunter2"))


def test_hub_settings_rejects_a_signing_key_of_the_wrong_length() -> None:
    short = base64.urlsafe_b64encode(b"\x00" * 16).decode()
    with pytest.raises(ValidationError, match="DAIMON_HUB__JWT_SIGNING_KEY"):
        HubSettings(jwt_signing_key=SecretStr(short))


def test_hub_settings_default_redirect_allowlist_is_loopback_and_claude() -> None:
    hub = HubSettings()
    assert hub.allowed_client_redirect_uris == [
        "http://localhost:*",
        "http://127.0.0.1:*",
        "https://claude.ai/*",
        "https://claude.com/*",
    ], f"got {hub.allowed_client_redirect_uris!r}"


def test_hub_settings_redirect_allowlist_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_HUB__ALLOWED_CLIENT_REDIRECT_URIS", '["https://ide.example/*"]')
    settings = load_settings(_env_file=None)
    assert settings.hub.allowed_client_redirect_uris == ["https://ide.example/*"], (
        f"got {settings.hub.allowed_client_redirect_uris!r}"
    )


def test_hub_settings_read_from_nested_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_HUB__SLACK_CLIENT_ID", "slack-id")
    monkeypatch.setenv("DAIMON_HUB__SLACK_CLIENT_SECRET", "slack-secret")
    settings = load_settings(_env_file=None)
    assert settings.hub.slack_client_id == "slack-id", f"got {settings.hub.slack_client_id!r}"
    assert settings.hub.slack_configured is True, "env-provided id+secret must configure slack"


def test_thread_naming_defaults_on_with_bounded_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.delenv("DAIMON_THREAD_NAMING__ENABLED", raising=False)
    settings = load_settings(_env_file=None)
    assert settings.thread_naming.enabled is True, "auto thread naming is on by default"
    assert settings.thread_naming.max_input_chars == 2000, (
        "the naming prompt is bounded to 2000 chars of the opening message by default"
    )
    assert settings.thread_naming.timeout_seconds == 5.0, (
        "a mention waits at most 5 s for a title before the thread opens by default"
    )


def test_thread_naming_parsed_from_top_level_nested_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_THREAD_NAMING__ENABLED", "false")
    monkeypatch.setenv("DAIMON_THREAD_NAMING__MAX_INPUT_CHARS", "500")
    monkeypatch.setenv("DAIMON_THREAD_NAMING__TIMEOUT_SECONDS", "2.5")
    settings = load_settings(_env_file=None)
    assert settings.thread_naming.enabled is False, (
        "DAIMON_THREAD_NAMING__ENABLED=false must turn the feature off"
    )
    assert settings.thread_naming.max_input_chars == 500, (
        "DAIMON_THREAD_NAMING__MAX_INPUT_CHARS must override the input bound"
    )
    assert settings.thread_naming.timeout_seconds == 2.5, (
        "DAIMON_THREAD_NAMING__TIMEOUT_SECONDS must override the wait for a title"
    )


def test_slack_display_name_defaults_to_daimon() -> None:
    settings = SlackSettings(signing_secret=SecretStr("test"), app_token=SecretStr("xapp-test"))
    assert settings.bot_display_name == "daimon", "unset display name must preserve existing copy"


@pytest.mark.parametrize(
    "name", ["", "a" * 33, "bot@name", "bot#name", "bot:name", "bot`name", "bot\\name"]
)
def test_slack_display_name_rejects_invalid_names(name: str) -> None:
    with pytest.raises(ValidationError):
        SlackSettings(
            signing_secret=SecretStr("test"),
            app_token=SecretStr("xapp-test"),
            bot_display_name=name,
        )


def test_teams_tenant_id_canonicalizes_uuid_case() -> None:
    """An uppercase Entra portal paste normalizes to the canonical UUID form
    the resolver and provision_tenant both compare against."""
    settings = TeamsSettings(
        client_id=str(UUID(int=1)),
        client_secret=SecretStr("test"),
        tenant_id=str(UUID(int=0xABCDEF)).upper(),
    )
    assert settings.tenant_id == str(UUID(int=0xABCDEF))


def test_teams_tenant_id_rejects_non_uuid() -> None:
    with pytest.raises(ValidationError):
        TeamsSettings(
            client_id=str(UUID(int=1)),
            client_secret=SecretStr("test"),
            tenant_id="not-a-tenant-uuid",
        )


def test_teams_admin_user_ids_canonicalize_and_reject_non_uuids() -> None:
    base = {"client_id": "id", "client_secret": SecretStr("s"), "tenant_id": str(UUID(int=1))}
    admin = str(UUID(int=7))
    assert TeamsSettings(**base, admin_user_ids=(admin.upper(),)).admin_user_ids == (admin,)
    with pytest.raises(ValidationError):
        TeamsSettings(**base, admin_user_ids=("alice",))


def test_completion_policy_validates_and_normalizes_uuid_keys(monkeypatch):
    import uuid

    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    tenant = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
    monkeypatch.setenv("DAIMON_COMPLETION_PINGS", '{"AAAAAAAA-0000-0000-0000-000000000001": true}')
    assert load_settings(_env_file=None).completion_pings == {tenant: True}
    monkeypatch.setenv("DAIMON_COMPLETION_PINGS", '{"typo": true}')
    with pytest.raises(ValidationError):
        load_settings(_env_file=None)


def test_table_rendering_map_validates_and_normalizes_uuid_keys(monkeypatch):
    import uuid

    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    tenant = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
    monkeypatch.setenv("DAIMON_TABLE_RENDERING", '{"AAAAAAAA-0000-0000-0000-000000000001": true}')
    assert load_settings(_env_file=None).table_rendering == {tenant: True}
    monkeypatch.setenv("DAIMON_TABLE_RENDERING", '{"not-a-uuid": true}')
    with pytest.raises(ValidationError):
        load_settings(_env_file=None)


@pytest.mark.parametrize("days", ["90", "7", "0"])
def test_security_audit_retention_environment(monkeypatch, days):
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_SECURITY_AUDIT_RETENTION_DAYS", days)
    assert load_settings(_env_file=None).security_audit_retention_days == int(days)


def test_security_audit_retention_default_and_negative_rejection(monkeypatch):
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.delenv("DAIMON_SECURITY_AUDIT_RETENTION_DAYS", raising=False)
    assert load_settings(_env_file=None).security_audit_retention_days == 90
    monkeypatch.setenv("DAIMON_SECURITY_AUDIT_RETENTION_DAYS", "-1")
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        load_settings(_env_file=None)


@pytest.mark.parametrize("form", ["bare", "comma", "json", "padded-comma"])
def test_crypto_keys_accept_a_bare_key_a_comma_list_or_a_json_list(
    monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    """The documented raw `Fernet.generate_key()` value must boot, not raise SettingsError."""
    first, second = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    raw = {
        "bare": first,
        "comma": f"{first},{second}",
        "json": f'["{first}", "{second}"]',
        "padded-comma": f" {first} , {second} ",
    }[form]
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", raw)

    keys = [k.get_secret_value() for k in load_settings(_env_file=None).crypto.keys]

    assert keys == ([first] if form == "bare" else [first, second])


def test_crypto_keys_empty_env_means_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", "")

    assert load_settings(_env_file=None).crypto.keys == ()


def test_malformed_crypto_keys_json_never_echoes_key_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in the JSON form must not print fragments of the keys in the boot error."""
    from daimon.core.config import load_crypto_settings

    first, second = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_CRYPTO__KEYS", f'["{first}", "{second}]')

    for load in (lambda: load_settings(_env_file=None), load_crypto_settings):
        with pytest.raises(Exception) as exc_info:
            load()
        rendered = f"{exc_info.value}\n{exc_info.value!r}"
        for key in (first, second):
            assert key[:12] not in rendered, "a key fragment leaked into the error"
            assert key[-12:] not in rendered, "a key fragment leaked into the error"


def test_slack_settings_history_page_limit_defaults_to_discord_replay_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The requested conversations.replies page size matches Discord's 100-message replay.

    Slack clamps the value per workspace, so a capped install still gets 15; an
    internal-app install gets the same depth a Discord thread does.
    """
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_SLACK__SIGNING_SECRET", "s" * 32)
    monkeypatch.setenv("DAIMON_SLACK__APP_TOKEN", "xapp-test")
    settings = load_settings(_env_file=None)
    assert settings.slack is not None, "slack block must be present when DAIMON_SLACK__* are set"
    assert settings.slack.history_page_limit == 100, (
        "history_page_limit must default to 100, the depth Discord replays"
    )


def test_slack_settings_history_page_limit_overrides_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator on an unclamped workspace can raise the page size to Slack's maximum."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_SLACK__SIGNING_SECRET", "s" * 32)
    monkeypatch.setenv("DAIMON_SLACK__APP_TOKEN", "xapp-test")
    monkeypatch.setenv("DAIMON_SLACK__HISTORY_PAGE_LIMIT", "1000")
    settings = load_settings(_env_file=None)
    assert settings.slack is not None
    assert settings.slack.history_page_limit == 1000, (
        "DAIMON_SLACK__HISTORY_PAGE_LIMIT must override the default page size"
    )


def test_slack_settings_history_page_limit_rejects_zero() -> None:
    """A page size Slack would refuse fails at boot, not on the first mention.

    conversations.replies answers ``invalid_limit`` for 0 and for anything above
    1000, and the listener boundary posts nothing on failure, so an unbounded
    field would turn a typo in .env into a workspace-wide silent bot.
    """
    from daimon.core.config import SlackSettings

    with pytest.raises(ValidationError):
        SlackSettings(signing_secret="s" * 32, app_token="xapp-test", history_page_limit=0)


def test_slack_settings_history_page_limit_rejects_above_slack_maximum() -> None:
    from daimon.core.config import SlackSettings

    with pytest.raises(ValidationError):
        SlackSettings(signing_secret="s" * 32, app_token="xapp-test", history_page_limit=1001)


def test_feedback_to_support_is_off_by_default_and_reads_a_per_tenant_map(monkeypatch):
    import uuid

    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    on = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
    off = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")
    assert load_settings(_env_file=None).support.routes_feedback(on) is False
    monkeypatch.setenv(
        "DAIMON_SUPPORT__FEEDBACK_TO_SUPPORT",
        '{"AAAAAAAA-0000-0000-0000-000000000001": true, '
        '"aaaaaaaa-0000-0000-0000-000000000002": false}',
    )
    support = load_settings(_env_file=None).support
    assert support.routes_feedback(on) is True
    assert support.routes_feedback(off) is False
    assert support.routes_feedback(uuid.uuid4()) is False
    monkeypatch.setenv("DAIMON_SUPPORT__FEEDBACK_TO_SUPPORT", '{"typo": true}')
    with pytest.raises(ValidationError):
        load_settings(_env_file=None)
