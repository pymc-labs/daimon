"""Web settings: env parsing and the checks that stop a bad sign-in setup booting."""

from __future__ import annotations

import base64
import json

import pytest
from daimon.core.config import (
    ICON_MAX_CHARS,
    AppPanel,
    EmbeddedAppSettings,
    WebOidcSettings,
    load_settings,
)
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")


def test_web_block_is_unset_when_no_web_variable_is_set() -> None:
    assert load_settings(_env_file=None).web is None, "deployments without web must boot unchanged"


def test_lists_parse_from_comma_separated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_WEB__PUBLIC_URL", "https://web.example.com")
    monkeypatch.setenv("DAIMON_WEB__MODULES", "chat, credits")
    monkeypatch.setenv("DAIMON_WEB__OIDC__ISSUER", "https://id.example.com")
    monkeypatch.setenv("DAIMON_WEB__OIDC__CLIENT_ID", "client")
    monkeypatch.setenv("DAIMON_WEB__OIDC__CLIENT_SECRET", "secret")
    monkeypatch.setenv("DAIMON_WEB__OIDC__TENANT_ID", "6f1b1d4e-0f3c-4a52-9a53-0d3a4a8e1f10")
    monkeypatch.setenv("DAIMON_WEB__OIDC__ALLOWED_DOMAINS", "Example.com,@corp.example.com")
    monkeypatch.setenv("DAIMON_WEB__OIDC__SCOPES", "openid,email,offline_access")
    web = load_settings(_env_file=None).web
    assert web is not None and web.oidc is not None, "the web block should load"
    assert web.modules == ("chat", "credits"), "modules should split on commas"
    assert web.oidc.allowed_domains == ("example.com", "corp.example.com"), "domains lowercase"
    assert web.oidc.scopes == ("openid", "email", "offline_access"), "scopes are configurable"
    assert web.oidc.issuer == "https://id.example.com", "the issuer must stay byte-for-byte"


def test_public_url_must_be_https_unless_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_WEB__PUBLIC_URL", "http://localhost:5174")
    with pytest.raises(ValidationError, match="https"):
        load_settings(_env_file=None)
    monkeypatch.setenv("DAIMON_WEB__DEV", "true")
    web = load_settings(_env_file=None).web
    assert web is not None and web.dev, "dev allows a plain-http local address"


@pytest.mark.parametrize(
    "url", ["http://localhost:5174", "http://127.0.0.1:5174", "http://[::1]:5174"]
)
def test_dev_is_allowed_on_a_loopback_public_url(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("DAIMON_WEB__PUBLIC_URL", url)
    monkeypatch.setenv("DAIMON_WEB__DEV", "true")
    web = load_settings(_env_file=None).web
    assert web is not None and web.dev, f"dev should be allowed on {url}"


@pytest.mark.parametrize(
    "url", ["https://web.example.com", "http://web.example.com", "https://localhost.example.com"]
)
def test_dev_is_refused_unless_the_public_url_is_loopback(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    """The dev sign-in accepts anyone, so a reachable host must not enable it."""
    monkeypatch.setenv("DAIMON_WEB__PUBLIC_URL", url)
    monkeypatch.setenv("DAIMON_WEB__DEV", "true")
    with pytest.raises(ValidationError, match="loopback"):
        load_settings(_env_file=None)


def test_scopes_without_openid_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_WEB__PUBLIC_URL", "https://web.example.com")
    monkeypatch.setenv("DAIMON_WEB__OIDC__ISSUER", "https://id.example.com")
    monkeypatch.setenv("DAIMON_WEB__OIDC__CLIENT_ID", "client")
    monkeypatch.setenv("DAIMON_WEB__OIDC__CLIENT_SECRET", "secret")
    monkeypatch.setenv("DAIMON_WEB__OIDC__TENANT_ID", "6f1b1d4e-0f3c-4a52-9a53-0d3a4a8e1f10")
    monkeypatch.setenv("DAIMON_WEB__OIDC__SCOPES", "email,profile")
    with pytest.raises(ValidationError, match="openid"):
        load_settings(_env_file=None)


_SVG = (
    "data:image/svg+xml;base64,"
    + base64.b64encode(b"<svg xmlns='http://www.w3.org/2000/svg'/>").decode()
)
_APP = {"key": "notes", "name": "Notes", "icon": "book", "origin": "https://notes.example.com"}


def test_embedded_app_config_parses_from_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented shape loads: an inline icon, a nav entry and an agent panel."""
    monkeypatch.setenv("DAIMON_WEB__PUBLIC_URL", "https://web.example.com")
    app = {
        **_APP,
        "icon": _SVG,
        "origin": "https://notes.example.com/",
        "connections_path": "/connections",
        "nav": [{"label": "Notes", "path": "/notes", "icon": "database"}],
        "panels": {
            "agent": {
                "embed": "/embed/agents/external/{agent_id}",
                "page": "/agents/external/{agent_id}",
            }
        },
    }
    monkeypatch.setenv("DAIMON_WEB__APPS", json.dumps([app]))
    web = load_settings(_env_file=None).web
    assert web is not None, "the web block should load"
    (loaded,) = web.apps
    assert loaded.icon == _SVG, "an inline icon is kept as given"
    assert loaded.origin == "https://notes.example.com", "origin loses the trailing slash"
    assert loaded.panels.agent is not None, "the agent panel is set"
    assert loaded.panels.agent.embed == "/embed/agents/external/{agent_id}", "templates are kept"
    assert loaded.panels.agent.page == "/agents/external/{agent_id}", "the page template is kept"


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("https://Notes.Example.com", "https://notes.example.com"),
        ("https://notes.example.com:443/", "https://notes.example.com"),
        ("http://notes.example.com:80", "http://notes.example.com"),
        ("https://notes.example.com:8443", "https://notes.example.com:8443"),
        ("https://notes.example.com:80", "https://notes.example.com:80"),
    ],
)
def test_embedded_app_origin_is_written_as_a_browser_does(given: str, kept: str) -> None:
    assert EmbeddedAppSettings(**{**_APP, "origin": given}).origin == kept, (
        "lowercase host, default port dropped, so it equals the browser's `event.origin`"
    )


def test_embedded_app_without_panels_has_none() -> None:
    assert EmbeddedAppSettings(**_APP).panels.agent is None, "panels are optional"
    panel = AppPanel(embed="/embed/{agent_id}")
    assert panel.page is None, "the full page is optional"


@pytest.mark.parametrize(
    "template",
    [
        "embed/{agent_id}",
        "//other.example.com/{agent_id}",
        "/embed/agents",
        "/embed/{id}",
        "/embed/{agent_id}/{agent_id}",
        "/embed/{agent_id}/{other}",
        "/embed/{agent_id",
    ],
)
def test_agent_panel_templates_need_a_path_with_one_agent_id(template: str) -> None:
    with pytest.raises(ValidationError, match="agent_id"):
        AppPanel(embed=template)
    with pytest.raises(ValidationError, match="agent_id"):
        AppPanel(embed="/embed/{agent_id}", page=template)


def test_stale_panel_names_are_refused_rather_than_ignored() -> None:
    with pytest.raises(ValidationError, match="items"):
        EmbeddedAppSettings(**_APP, panels={"items": "/data/items/{id}"})  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "icon", ["database", "key-round", _SVG, "data:image/png;base64,iVBORw0KGgo="]
)
def test_icons_take_a_name_or_an_inline_image(icon: str) -> None:
    assert EmbeddedAppSettings(**{**_APP, "icon": icon}).icon == icon, "a valid icon is kept"


@pytest.mark.parametrize(
    "icon",
    [
        "",
        "Database",
        "https://cdn.example.com/icon.svg",
        "data:image/svg+xml,<svg/>",
        "data:image/gif;base64,R0lGODlh",
        "data:text/html;base64,PGI+",
        "data:image/png;base64,not base64!",
    ],
)
def test_icons_refuse_other_forms(icon: str) -> None:
    with pytest.raises(ValidationError, match="icon"):
        EmbeddedAppSettings(**{**_APP, "icon": icon})


def test_icons_are_capped_at_16_kb() -> None:
    edge = "data:image/png;base64," + "A" * (ICON_MAX_CHARS - len("data:image/png;base64,"))
    assert EmbeddedAppSettings(**{**_APP, "icon": edge}).icon == edge, "16 KB fits"
    with pytest.raises(ValidationError, match="16 KB"):
        EmbeddedAppSettings(**{**_APP, "icon": edge + "A"})


def test_the_sign_in_button_icon_takes_the_same_forms() -> None:
    oidc = {
        "issuer": "https://id.example.com",
        "client_id": "client",
        "client_secret": "secret",
        "tenant_id": "6f1b1d4e-0f3c-4a52-9a53-0d3a4a8e1f10",
    }
    assert WebOidcSettings(**oidc, icon=_SVG).icon == _SVG, "an inline image is accepted"  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValidationError, match="icon"):
        WebOidcSettings(**oidc, icon="https://id.example.com/mark.svg")  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValidationError, match="16 KB"):
        WebOidcSettings(**oidc, icon="data:image/png;base64," + "A" * ICON_MAX_CHARS)  # pyright: ignore[reportArgumentType]


def test_connections_path_is_a_site_path_or_unset() -> None:
    app = {"key": "n", "name": "N", "icon": "book", "origin": "https://n.example.com"}
    assert EmbeddedAppSettings(**app).connections_path is None
    assert (
        EmbeddedAppSettings(**app, connections_path="/connections").connections_path
        == "/connections"
    )
    with pytest.raises(ValidationError, match="starting with /"):
        EmbeddedAppSettings(**app, connections_path="connections")
