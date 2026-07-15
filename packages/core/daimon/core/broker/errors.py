"""Typed errors for the token broker (Phase 19, GH-03).

Adapter boundaries (MCP tool, CLI commands) catch these and convert to
their adapter-native error responses. Core code raises them, never
swallows them.
"""

from __future__ import annotations

from daimon.core.errors import DaimonError


class BrokerError(DaimonError):
    """Base for token-broker errors. Adapters catch this at their edge."""


class NoBindingError(BrokerError):
    """Raised when no credential / binding exists for the requesting agent.

    Operator-actionable: the resolution is to bind credentials via the
    agent-setup repo-auth panel (inline PAT), or install the GitHub App on the repo.
    """


class ProviderConfigError(BrokerError):
    """Raised when provider configuration (settings) is missing or invalid.

    Operator-actionable: the resolution is to set the missing env var.
    """
