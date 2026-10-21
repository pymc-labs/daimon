"""Shared selfhosted.environment v1 schema and resource input validation."""

from __future__ import annotations

import ipaddress
import re
import socket
from contextlib import suppress
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from mux.contracts.resources import AgentSpec, EnvironmentSpec, MCPConnection
from mux.errors import ProviderError
from mux.profiles.selfhosted import LOOP

_URL: re.Pattern[str] = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>]+")
_PRIVATE_V4 = tuple(
    ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_PRIVATE_V6 = ipaddress.ip_network("fc00::/7")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MCP_CREDENTIAL_ENV = re.compile(r"HARNESS_MCP_CRED_[A-Za-z0-9_]+")
SESSION_POOL_AUDIENCE = "https://dynamicsessions.io"
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
    address = None
    with suppress(ValueError):
        address = ipaddress.ip_address(host)
    if address is None:
        # inet_aton accepts the integer, octal, hex and shortened IPv4 forms
        # that the HTTP resolver also accepts, without performing DNS I/O.
        with suppress(OSError):
            address = ipaddress.IPv4Address(socket.inet_aton(host))
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if host == "localhost" or (address is not None and address.is_loopback):
        return
    if address is not None:
        internal = (
            any(address in network for network in _PRIVATE_V4)
            if isinstance(address, ipaddress.IPv4Address)
            else address in _PRIVATE_V6
        )
    else:
        internal = not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9.]+)", host) and (
            "." not in host
            or host.endswith((".internal", ".svc.cluster.local"))
            or (".internal." in host and host.endswith(".azurecontainerapps.io"))
        )
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
            for key, child in parse_qsl(parsed.query) + parse_qsl(parsed.fragment):
                if (
                    key.lower().replace("-", "_")
                    in _SECRET_KEYS
                    | {
                        "key",
                        "auth",
                        "sig",
                        "signature",
                        "credential",
                    }
                    and child
                ):
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
    session_pool: bool = False
    """Route direct C3 calls through an ACA dynamic-session pool management endpoint."""

    @model_validator(mode="after")
    def validate_routing(self) -> HarnessConfig:
        _reject_credentials([self.harness_url, self.auth_ref, self.project_endpoint])
        _url(self.harness_url, allow_internal_http=self.allow_insecure_http)
        if self.auth_mode == "bearer_env":
            if not _ENV.fullmatch(self.auth_ref):
                raise ValueError("bearer_env auth_ref must be an environment variable name")
        else:
            _url(self.auth_ref, audience=True)
        if self.auth_ref == SESSION_POOL_AUDIENCE and not self.session_pool:
            raise ValueError("the dynamic sessions audience requires session_pool=true")
        if self.session_pool and (
            self.transport != "direct"
            or self.auth_mode != "entra"
            or self.auth_ref != SESSION_POOL_AUDIENCE
            or urlsplit(self.harness_url).scheme != "https"
        ):
            raise ValueError(
                "session pools require direct HTTPS and Entra audience https://dynamicsessions.io"
            )
        if self.transport == "foundry_invocations":
            if not self.agent_name or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", self.agent_name
            ):
                raise ValueError(
                    "Foundry agent names require 1–63 ASCII letters, digits, underscores or "
                    "hyphens, starting with a letter or digit"
                )
            if not self.project_endpoint:
                raise ValueError("Foundry transport requires project_endpoint")
            _url(self.project_endpoint)
            if self.auth_mode != "entra":
                raise ValueError("Foundry transport requires Entra authentication")
        elif self.agent_name is not None or self.project_endpoint is not None:
            raise ValueError("Foundry routing fields require foundry_invocations transport")
        return self


def _validate_mcp_policy(server: MCPConnection) -> None:
    """Admit only permission labels and a closed list of tool names."""
    policy = server.tool_policy
    allowed = policy.get("allowed_tools")
    if "allowed_tools" in policy and (
        not isinstance(allowed, list)
        or any(
            not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
            for name in allowed
        )
    ):
        raise ValueError("MCP allowed_tools must contain tool names")
    for name, permission in policy.items():
        if name == "allowed_tools":
            continue
        tool = name.removeprefix(server.name + "/")
        if permission not in ("auto", "ask", "deny") or not re.fullmatch(
            r"[A-Za-z0-9_.-]+|\*", tool
        ):
            raise ValueError(
                "MCP policy requires tools in the server namespace and permission labels"
            )
        if isinstance(allowed, list) and tool not in allowed and permission != "deny":
            raise ValueError("MCP policy cannot widen allowed_tools")


def validate_resource_spec(spec: AgentSpec | EnvironmentSpec) -> None:
    """Reject credential-bearing or unsupported resource input with a mux error.

    Tool policies contain only closed permission labels and tool names;
    arbitrary dictionaries cannot bypass credential-reference validation.
    """
    try:
        _reject_credentials(dict(spec.metadata or {}))
        if not spec.name.strip() or spec.name.startswith("sh_"):
            raise ValueError(
                "resource names must be nonblank and cannot use the reserved sh_ prefix"
            )
        if isinstance(spec, AgentSpec):
            _reject_credentials(dict(spec.model.options or {}))
            if spec.model.provider != LOOP.provider or spec.extensions:
                raise ValueError("selfhosted agents require their own model and no extensions")
            for server in spec.mcp_servers or ():
                _reject_credentials(server.model_dump(mode="json", exclude={"name"}))
                _url(server.url, allow_internal_http=server.credential_ref is None)
                if server.credential_ref is not None and not _MCP_CREDENTIAL_ENV.fullmatch(
                    server.credential_ref
                ):
                    raise ValueError("MCP credential_ref must name a HARNESS_MCP_CRED_ variable")
                _validate_mcp_policy(server)
        else:
            _reject_credentials(spec.model_dump(mode="json")["packages"])
            if spec.execution != "self_hosted" or spec.native_config is None:
                raise ValueError("selfhosted environments require harness configuration")
            if (
                spec.native_config.namespace != "selfhosted.environment"
                or spec.native_config.version != 1
            ):
                raise ValueError("unsupported environment configuration")
            HarnessConfig.model_validate(dict(spec.native_config.value))
            for source in spec.sources or ():
                _reject_credentials(
                    {
                        "repository_url": source.repository_url,
                        "credential_ref": source.credential_ref,
                    }
                )
                if source.repository_url is not None:
                    _url(source.repository_url)
                if source.credential_ref is not None and not _ENV.fullmatch(source.credential_ref):
                    raise ValueError("source credential_ref must be an environment variable name")
    except ValueError:
        raise ProviderError("invalid_request", retryable=False) from None
