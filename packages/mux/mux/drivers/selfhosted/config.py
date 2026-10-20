"""Shared selfhosted.environment v1 schema and resource input validation."""

from __future__ import annotations

import ipaddress
import re
from contextlib import suppress
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from mux.contracts.resources import AgentSpec, EnvironmentSpec
from mux.errors import ProviderError
from mux.profiles.selfhosted import LOOP

_URL: re.Pattern[str] = re.compile(r"https?://[^\s<>]+")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CREDENTIAL = re.compile(
    r"(?i)\bBearer\s+\S+|\b(?:sk-[A-Za-z0-9_-]+|gh[pousr]_[A-Za-z0-9_]+|"
    r"github_pat_[A-Za-z0-9_]+|xox[baprs]-[A-Za-z0-9-]+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
)
_SECRET_KEYS = frozenset(
    {
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "x_api_key",
        "api_key",
        "password",
        "client_secret",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "private_key",
    }
)


def _url(value: str, *, allow_internal_http: bool = False, audience: bool = False) -> None:
    parsed = urlsplit(value)
    host = parsed.hostname
    _ = parsed.port  # Validate malformed port syntax as well as the host.
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("URLs must contain no credentials, query or fragment")
    if audience:
        if parsed.scheme not in ("api", "https"):
            raise ValueError("Entra auth_ref must be an api:// or https:// audience URI")
        return
    if parsed.scheme == "https":
        return
    if parsed.scheme != "http":
        raise ValueError("routing URLs must use HTTPS")
    if host in ("localhost", "127.0.0.1", "::1"):
        return
    internal = (
        "." not in host
        or host.endswith((".internal", ".svc.cluster.local"))
        or (".internal." in host and host.endswith(".azurecontainerapps.io"))
    )
    with suppress(ValueError):
        internal = internal or ipaddress.ip_address(host).is_private
    if not (allow_internal_http and internal):
        raise ValueError("HTTP requires loopback or explicit permission for an internal endpoint")


def _reject_credentials(value: JsonValue) -> None:
    """One recursive check covers free-form strings, mappings and URL values."""
    if isinstance(value, str):
        if _CREDENTIAL.search(value):
            raise ValueError("credential values must be referenced, never embedded")
        for match in _URL.finditer(value):
            parsed = urlsplit(match.group(0))
            if parsed.username or parsed.password:
                raise ValueError("URL credentials must be referenced, never embedded")
    elif isinstance(value, dict):
        for name, child in value.items():
            if name.lower().replace("-", "_") in _SECRET_KEYS and isinstance(child, str) and child:
                raise ValueError("credential fields must be supplied by reference")
            _reject_credentials(child)
    elif isinstance(value, list):
        for child in value:
            _reject_credentials(child)


class HarnessConfig(BaseModel):
    """Environment native_config.value; auth_ref names an environment variable or audience."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    harness_url: str
    auth_mode: Literal["bearer_env", "entra"] = "bearer_env"
    auth_ref: str
    packages_note: str | None = None
    transport: Literal["direct", "foundry_invocations"] = "direct"
    agent_name: str | None = None
    project_endpoint: str | None = None
    allow_insecure_http: bool = False
    """Explicit permission for direct HTTP inside a private cluster, such as ACA fallback."""

    @model_validator(mode="after")
    def validate_routing(self) -> HarnessConfig:
        _reject_credentials(self.model_dump(mode="json"))
        _url(self.harness_url, allow_internal_http=self.allow_insecure_http)
        if self.auth_mode == "bearer_env":
            if not _ENV.fullmatch(self.auth_ref):
                raise ValueError("bearer_env auth_ref must be an environment variable name")
        else:
            _url(self.auth_ref, audience=True)
        if self.transport == "foundry_invocations":
            if (
                not self.agent_name
                or self.agent_name in (".", "..")
                or not re.fullmatch(r"[A-Za-z0-9_.-]+", self.agent_name)
            ):
                raise ValueError("Foundry transport requires an agent name without path separators")
            if not self.project_endpoint:
                raise ValueError("Foundry transport requires project_endpoint")
            _url(self.project_endpoint)
            if self.auth_mode != "entra":
                raise ValueError("Foundry transport requires Entra authentication")
        elif self.agent_name is not None or self.project_endpoint is not None:
            raise ValueError("Foundry routing fields require foundry_invocations transport")
        return self


def validate_resource_spec(spec: AgentSpec | EnvironmentSpec) -> None:
    """Reject credential-bearing or unsupported resource input with a mux error.

    Extensions and tool policies have no selfhosted v1 credential schema, so
    accepting arbitrary dictionaries there would bypass reference validation.
    """
    try:
        _reject_credentials(spec.model_dump(mode="json"))
        if not spec.name.strip() or spec.name.startswith("sh_"):
            raise ValueError(
                "resource names must be nonblank and cannot use the reserved sh_ prefix"
            )
        if isinstance(spec, AgentSpec):
            if spec.model.provider != LOOP.provider or spec.extensions:
                raise ValueError("selfhosted agents require their own model and no extensions")
            for server in spec.mcp_servers or ():
                _url(server.url, allow_internal_http=True)
                if server.credential_ref is not None and not _ENV.fullmatch(server.credential_ref):
                    raise ValueError("MCP credential_ref must be an environment variable name")
                if server.tool_policy:
                    raise ValueError("MCP tool policies are not supported by selfhosted v1")
        else:
            if spec.execution != "self_hosted" or spec.native_config is None:
                raise ValueError("selfhosted environments require harness configuration")
            if (
                spec.native_config.namespace != "selfhosted.environment"
                or spec.native_config.version != 1
            ):
                raise ValueError("unsupported environment configuration")
            HarnessConfig.model_validate(dict(spec.native_config.value))
            for source in spec.sources or ():
                if source.repository_url is not None:
                    _url(source.repository_url)
                if source.credential_ref is not None and not _ENV.fullmatch(source.credential_ref):
                    raise ValueError("source credential_ref must be an environment variable name")
    except ValueError:
        raise ProviderError("invalid_request", retryable=False) from None
