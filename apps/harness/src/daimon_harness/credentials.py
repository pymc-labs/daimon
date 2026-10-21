"""Per-turn MCP credentials stay in memory and out of replayable request bodies."""

from __future__ import annotations

import ipaddress
import logging
import os
import threading
import traceback
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from mux.drivers.selfhosted.config import reject_metadata_url
from mux.drivers.selfhosted.wire import MCPServer, TurnRequest
from mux.mcp_url import canonical_mcp_url
from pydantic import JsonValue, TypeAdapter

JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

VIRTUAL_ENV = "DAIMON_MCP_AUTHORIZATION"
RESERVED_SERVER = "daimon-mcp"


def normalized_mcp_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() or ord(char) < 32 for char in url)
    ):
        raise ValueError("invalid MCP URL")
    _ = parsed.port  # Preserve strict malformed-port rejection before comparison.
    reject_metadata_url(url)
    return canonical_mcp_url(url)


def mcp_origin(url: str) -> str:
    parsed = urlsplit(normalized_mcp_url(url))
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def load_bindings() -> dict[str, str]:
    """Validate operator-owned bindings; tenant authorization happens in the host."""
    raw: JsonValue = JSON.validate_json(os.environ.get("HARNESS_MCP_CRED_BINDINGS", "[]"))
    if not isinstance(raw, list):
        raise ValueError("MCP credential bindings must be a list")
    bindings: dict[str, str] = {}
    for binding in raw:
        if not isinstance(binding, dict) or set(binding) != {"name", "origin", "tenants"}:
            raise ValueError("invalid MCP credential binding")
        name = binding["name"]
        if (
            not isinstance(name, str)
            or not name.startswith("HARNESS_MCP_CRED_")
            or name == "HARNESS_MCP_CRED_BINDINGS"
            or name in bindings
        ):
            raise ValueError("invalid MCP credential binding")
        origin = binding["origin"]
        tenants = binding["tenants"]
        if (
            not isinstance(origin, str)
            or urlsplit(origin).path
            or mcp_origin(origin) != normalized_mcp_url(origin)
        ):
            raise ValueError("credential binding must specify an origin only")
        if urlsplit(origin).scheme != "https":
            raise ValueError("credential binding requires HTTPS")
        if not isinstance(tenants, list) or not tenants:
            raise ValueError("credential binding requires tenant UUIDs")
        if tenants != ["*"]:
            for tenant in tenants:
                if not isinstance(tenant, str):
                    raise ValueError("credential binding requires tenant UUIDs")
                UUID(tenant)
        bindings[name] = mcp_origin(origin)
    return bindings


def validate_server(server: MCPServer) -> None:
    normalized_mcp_url(server.url)
    for env in server.headers_env.values():
        if env == VIRTUAL_ENV:
            if server.name != RESERVED_SERVER:
                raise ValueError("reserved MCP credential referenced by another server")
        elif not env.startswith("HARNESS_MCP_CRED_"):
            raise ValueError("MCP credential references must start with HARNESS_MCP_CRED_")
    if server.headers_env or server.name == RESERVED_SERVER:
        parsed = urlsplit(server.url)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if parsed.scheme != "https" and not loopback:
            raise ValueError("credential-bearing MCP servers require HTTPS")
    expected_url = os.environ.get("HARNESS_DAIMON_MCP_URL")
    if server.name == RESERVED_SERVER:
        if not expected_url or normalized_mcp_url(server.url) != normalized_mcp_url(expected_url):
            raise ValueError("Daimon MCP URL does not match harness configuration")
    elif expected_url and normalized_mcp_url(server.url) == normalized_mcp_url(expected_url):
        raise ValueError("Daimon MCP URL requires its reserved server name")


def turn_credentials(
    request: TurnRequest,
    authorization: str | None,
    operator_credentials: dict[str, str] | None = None,
    bindings: dict[str, str] | None = None,
) -> dict[str, str]:
    credentials: dict[str, str] = {}
    bindings = load_bindings() if bindings is None else bindings
    for server in request.mcp_servers:
        validate_server(server)
        if server.name == RESERVED_SERVER:
            if not any(
                header.lower() == "authorization" and env == VIRTUAL_ENV
                for header, env in server.headers_env.items()
            ):
                raise ValueError("Daimon MCP requires its reserved credential reference")
            if (
                not authorization
                or not authorization.startswith("Bearer ")
                or not authorization[7:]
            ):
                raise ValueError("Daimon MCP credential is missing")
            credentials[VIRTUAL_ENV] = authorization
        for env in server.headers_env.values():
            if env != VIRTUAL_ENV:
                if bindings.get(env) != mcp_origin(server.url):
                    raise ValueError("MCP credential is unbound or bound to a different origin")
                value = (
                    operator_credentials if operator_credentials is not None else os.environ
                ).get(env)
                if not value:
                    raise ValueError("MCP credential is not configured")
                credentials[env] = value
    return credentials


def mcp_headers(
    server: MCPServer, credentials: dict[str, str], bindings: dict[str, str] | None = None
) -> dict[str, str]:
    # Repeat validation at the resolution boundary, even for direct adapter callers.
    validate_server(server)
    bindings = load_bindings() if bindings is None else bindings
    for env in server.headers_env.values():
        if env != VIRTUAL_ENV and bindings.get(env) != mcp_origin(server.url):
            raise ValueError("MCP credential is unbound or bound to a different origin")
    return {header: credentials[env] for header, env in server.headers_env.items()}


def redact(value: JsonValue, credentials: dict[str, str]) -> JsonValue:
    if isinstance(value, str):
        for secret in credentials.values():
            if not secret:
                continue
            value = value.replace(secret, "[redacted]")
            if secret.startswith("Bearer "):
                value = value.replace(secret[7:], "[redacted]")
        return value
    if isinstance(value, list):
        return [redact(item, credentials) for item in value]
    if isinstance(value, dict):
        return {
            str(redact(key, credentials)): redact(item, credentials) for key, item in value.items()
        }
    return value


def process_credentials() -> dict[str, str]:
    """Capture startup secrets before the process environment is scrubbed."""
    return {
        name: value
        for name, value in os.environ.items()
        if value
        and name != "HARNESS_MCP_CRED_BINDINGS"
        and (
            name in {"IDENTITY_HEADER", "MSI_ENDPOINT", "IDENTITY_ENDPOINT", "AZURE_CLIENT_ID"}
            or name.startswith("HARNESS_MCP_CRED_")
            or any(marker in name.upper() for marker in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))
        )
    }


_LOG_LOCK = threading.RLock()
_LOG_CREDENTIALS: dict[object, dict[str, str]] = {}
_log_factory_installed = False


def _scrub_log_text(value: str) -> str:
    with _LOG_LOCK:
        secrets = {secret for values in _LOG_CREDENTIALS.values() for secret in values.values()}
    secrets |= {secret[7:] for secret in secrets if secret.startswith("Bearer ")}
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            value = value.replace(secret, "[redacted]")
    return value


def _scrub_log_value(value: object) -> object:
    if isinstance(value, str):
        return _scrub_log_text(value)
    if isinstance(value, tuple):
        return tuple(_scrub_log_value(item) for item in cast(tuple[object, ...], value))
    if isinstance(value, dict):
        return {
            _scrub_log_value(key): _scrub_log_value(item)
            for key, item in cast(Mapping[object, object], value).items()
        }
    if isinstance(value, list):
        return [_scrub_log_value(item) for item in cast(list[object], value)]
    if value is None or isinstance(value, (int, float)):
        return value
    return _scrub_log_text(str(value))


class CredentialLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Logger filters (notably the SDK's SensitiveHeadersFilter) have already
        # run. Preserve interpolation arguments for any later handler filters.
        record.msg = _scrub_log_text(str(record.msg))
        record.args = cast(tuple[object, ...] | dict[str, object], _scrub_log_value(record.args))
        if record.exc_info:
            record.exc_text = _scrub_log_text("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = _scrub_log_text(record.stack_info)
        return True


_log_filter = CredentialLogFilter()


def remember_log_secret(credentials: dict[str, str], name: str, value: str) -> None:
    with _LOG_LOCK:
        credentials[name] = value


@contextmanager
def credential_log_redaction(credentials: dict[str, str]) -> Iterator[dict[str, str]]:
    global _log_factory_installed
    key = object()
    with _LOG_LOCK:
        if not _log_factory_installed:
            original = logging.getLogRecordFactory()

            def factory(*args: object, **kwargs: object) -> logging.LogRecord:
                record = original(*args, **kwargs)
                # Attach at emission time so newly configured SDK/capture handlers
                # are covered too. Do not format or change the record here.
                logger: logging.Logger | None = logging.getLogger(record.name)
                while logger is not None:
                    for handler in logger.handlers:
                        handler.addFilter(_log_filter)
                    logger = logger.parent if logger.propagate else None
                if logging.lastResort is not None:
                    logging.lastResort.addFilter(_log_filter)
                return record

            logging.setLogRecordFactory(factory)
            _log_factory_installed = True
        values = credentials.copy()
        _LOG_CREDENTIALS[key] = values
    try:
        yield values
    finally:
        with _LOG_LOCK:
            _LOG_CREDENTIALS.pop(key, None)
