"""Shared selfhosted.environment v1 routing schema for hosts and drivers."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, model_validator


class HarnessConfig(BaseModel):
    """Environment native_config.value; auth_ref always names a credential source."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    harness_url: str
    auth_mode: Literal["bearer_env", "entra"] = "bearer_env"
    auth_ref: str
    packages_note: str | None = None
    transport: Literal["direct", "foundry_invocations"] = "direct"
    agent_name: str | None = None
    project_endpoint: str | None = None

    @model_validator(mode="after")
    def validate_routing(self) -> HarnessConfig:
        url = urlsplit(self.harness_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise ValueError("harness_url must be an HTTP URL without credentials")
        if url.query or url.fragment:
            raise ValueError("harness_url cannot contain a query or fragment")
        if not self.auth_ref.strip():
            raise ValueError("auth_ref must name an environment variable or Entra audience")
        if self.auth_mode == "bearer_env" and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", self.auth_ref
        ):
            raise ValueError("bearer_env auth_ref must be an environment variable name")
        if self.transport == "foundry_invocations":
            if not self.agent_name or not re.fullmatch(r"[A-Za-z0-9_.-]+", self.agent_name):
                raise ValueError("Foundry transport requires an agent_name without path separators")
            if not self.project_endpoint:
                raise ValueError("Foundry transport requires project_endpoint")
        if self.project_endpoint is not None:
            project = urlsplit(self.project_endpoint)
            if (
                project.scheme not in ("http", "https")
                or not project.hostname
                or project.username
                or project.password
                or project.query
                or project.fragment
            ):
                raise ValueError(
                    "project_endpoint must be HTTP without credentials, query or fragment"
                )
        return self
