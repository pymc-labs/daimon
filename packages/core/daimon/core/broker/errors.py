"""Typed errors for the token broker.

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

    Resolution: bind a working-repo token for the agent — `request_repo_binding`
    is the chat path that collects one — or install the GitHub App on the
    repository.
    """


class ProviderConfigError(BrokerError):
    """Raised when provider configuration (settings) is missing or invalid.

    Operator-actionable: the resolution is to set the missing deployment
    configuration value.
    """
