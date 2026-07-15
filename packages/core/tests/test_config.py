from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from daimon.core.config import McpSettings, Settings, load_settings
from pydantic import HttpUrl, ValidationError


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


def test_mcp_settings_both_unset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase-1-only deployments keep working with MCP subtree fully unset."""
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
    """Phase 29: GeminiSettings.api_key is None when env unset."""
    monkeypatch.delenv("DAIMON_GEMINI__API_KEY", raising=False)
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    settings = load_settings(_env_file=None)
    assert settings.gemini.api_key is None, "gemini.api_key optional when unset"


def test_gemini_settings_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 29: DAIMON_GEMINI__API_KEY populates settings.gemini.api_key."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_GEMINI__API_KEY", "gem-test-key")
    settings = load_settings(_env_file=None)
    assert settings.gemini.api_key is not None
    assert settings.gemini.api_key.get_secret_value() == "gem-test-key"


def test_mcp_file_store_dir_overrides_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 29: DAIMON_MCP__FILE_STORE_DIR overrides the default tempdir path."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_MCP__FILE_STORE_DIR", "/var/lib/daimon/mcp-files")
    settings = load_settings(_env_file=None)
    assert settings.mcp.file_store_dir == Path("/var/lib/daimon/mcp-files")


def test_defaults_root_default_is_relative_defaults_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 38-04: single source of truth for defaults/ path lives on Settings."""
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
    """Phase 43: NotebookSettings.max_attachment_bytes defaults to 10 MiB."""
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
    """Phase 43: DAIMON_NOTEBOOK__MAX_ATTACHMENT_BYTES overrides the default."""
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


# --- BillingSettings tests (Phase 53, TOPUP-01) ---


def test_billing_defaults_when_no_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """billing.markup and billing.signup_credit default correctly when unset."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.delenv("DAIMON_BILLING__MARKUP", raising=False)
    monkeypatch.delenv("DAIMON_BILLING__SIGNUP_CREDIT", raising=False)
    settings = load_settings(_env_file=None)
    assert settings.billing.markup == Decimal("1.0"), (
        "default markup must be Decimal('1.0') (pass-through, D-15)"
    )
    assert settings.billing.signup_credit == Decimal("5.00"), (
        "default signup_credit must be >0 (Decimal('5.00')) so one-click works on trial "
        "credit before payment (D-25); operators set 0 for pay-first"
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
    """D-07: discord liveness port defaults to 8081 (distinct from mcp 8080 / scheduler 8082)."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_DISCORD__BOT_TOKEN", "discord-token")
    settings = load_settings(_env_file=None)
    assert settings.discord is not None, "discord subtree present when bot_token is set"
    assert settings.discord.health_port == 8081, "health_port defaults to 8081 (D-07)"


def test_discord_per_caller_thread_sessions_defaults_to_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#196: fresh deploys must run per-caller thread sessions by default so the
    #162 confused-deputy (frozen thread-starter identity) leak does not ship."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_DISCORD__BOT_TOKEN", "discord-token")
    monkeypatch.delenv("DAIMON_DISCORD__PER_CALLER_THREAD_SESSIONS", raising=False)
    settings = load_settings(_env_file=None)
    assert settings.discord is not None, "discord subtree present when bot_token is set"
    assert settings.discord.per_caller_thread_sessions is True, (
        "per_caller_thread_sessions must default to True on a fresh deploy (#196)"
    )


def test_discord_per_caller_thread_sessions_opt_out_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators can still explicitly disable per-caller thread sessions."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.setenv("DAIMON_DISCORD__BOT_TOKEN", "discord-token")
    monkeypatch.setenv("DAIMON_DISCORD__PER_CALLER_THREAD_SESSIONS", "false")
    settings = load_settings(_env_file=None)
    assert settings.discord is not None, "discord subtree present when bot_token is set"
    assert settings.discord.per_caller_thread_sessions is False, (
        "explicit false must still opt out of per-caller thread sessions"
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


# --- GithubSettings tarball size caps (RATE-03) ---


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


# --- SlackSettings tests (Phase 78, SCORE-03) ---


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


def test_privacy_policy_url_defaults_to_daimon_dev_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No override → operator gets the original daimon.dev privacy page."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")
    monkeypatch.delenv("DAIMON_PRIVACY_POLICY_URL", raising=False)
    settings = load_settings(_env_file=None)
    assert str(settings.privacy_policy_url) == "https://daimon.dev/privacy", (
        "default privacy_policy_url must remain https://daimon.dev/privacy unchanged"
    )


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
