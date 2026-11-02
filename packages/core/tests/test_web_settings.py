"""Web settings: env parsing and the checks that stop a bad sign-in setup booting."""

from __future__ import annotations

import json

import pytest
from daimon.core.config import load_settings
from daimon.core.mcp_identity import encode_seed, generate_signing_key
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
    web = load_settings(_env_file=None).web
    assert web is not None, "the web block should load"
    assert web.modules == ("chat", "credits"), "modules should split on commas"


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


_SERVER = {
    "name": "data",
    "url": "https://data.example.com/mcp",
    "issuer": "6f1b1d4e-0f3c-4a52-9a53-0d3a4a8e1f10",
    "audience": "https://data.example.com",
}
_NODE = {"IDENTITY_SERVER": "data", "CLIENT_ID": "c", "CLIENT_SECRET": "s"}
_NODE_PREFIX = "DAIMON_WEB__NODES__DATA_SERVER__"


def _node_env(monkeypatch: pytest.MonkeyPatch, *, node: dict[str, str], key: bool = True) -> None:
    monkeypatch.setenv("DAIMON_WEB__PUBLIC_URL", "https://web.example.com")
    monkeypatch.setenv("DAIMON_MCP_IDENTITY__SERVERS", json.dumps([_SERVER]))
    if key:
        monkeypatch.setenv("DAIMON_MCP_IDENTITY__SIGNING_KEY", encode_seed(generate_signing_key()))
    for name, value in node.items():
        monkeypatch.setenv(name if name.startswith("DAIMON_") else _NODE_PREFIX + name, value)


def test_a_node_loads_from_flat_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    _node_env(monkeypatch, node={**_NODE, "SCOPE": "openid data"})
    web = load_settings(_env_file=None).web
    assert web is not None, "the web block should load"
    node = web.nodes.data_server
    assert node is not None, "the kind is the block the variables name"
    assert (node.identity_server, node.label, node.scope) == (
        "data",
        "Data server",
        "openid data",
    ), "the node keeps its server, a default label and its scope"
    assert node.client_secret.get_secret_value() == "s", "the secret is its own variable"
    assert list(web.nodes.configured()) == ["data_server"], "configured nodes are keyed by kind"


def test_no_node_is_configured_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_WEB__PUBLIC_URL", "https://web.example.com")
    web = load_settings(_env_file=None).web
    assert web is not None and web.nodes.configured() == {}, "nodes are opt-in"


def test_a_node_block_with_only_some_fields_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every process parses the web block, but only the web service gets all its node variables."""
    monkeypatch.setenv("DAIMON_WEB__PUBLIC_URL", "https://web.example.com")
    monkeypatch.setenv(_NODE_PREFIX + "CLIENT_SECRET", "s")
    web = load_settings(_env_file=None).web
    assert web is not None, "the web block should load"
    node = web.nodes.data_server
    assert node is not None and node.client_secret is not None, "the secret alone is a node block"
    assert node.identity_server is None and node.client_id is None, (
        "the rest is left to the web app"
    )


def test_a_node_of_an_unknown_kind_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _node_env(monkeypatch, node={"DAIMON_WEB__NODES__OTHER__CLIENT_ID": "c"})
    with pytest.raises(ValidationError, match="other"):
        load_settings(_env_file=None)


def test_a_node_error_never_echoes_its_client_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    _node_env(monkeypatch, node={**_NODE, "CLIENT_SECRET": "s3cret-value", "EXTRA": "x"})
    with pytest.raises(ValidationError) as caught:
        load_settings(_env_file=None)
    assert "s3cret-value" not in str(caught.value), "settings errors hide their input"
