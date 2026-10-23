"""The exhaustive set of actions for which live Daimon delegation applies.

Everything outside this enum requires platform authority. These verdicts are
request-local evidence, never account roles or persisted authorization.
"""

from collections.abc import Sequence
from enum import StrEnum


class DelegatedAction(StrEnum):
    AGENT_SETTINGS = "agent_settings"
    CHANNEL_PERMISSIONS = "channel_permissions"
    ROUTINES = "routines"
    SKILLS = "skills"
    ENVIRONMENTS = "environments"
    MODEL = "model"
    ROUTING = "routing"
    MCP_CONNECTIONS = "mcp_connections"
    CREDENTIALS = "credentials"
    TOOL_RULES = "tool_rules"
    AGENT_LIFECYCLE = "agent_lifecycle"
    HUB_PIN_EXEMPTION = "hub_pin_exemption"


def admin_for_action(
    *, platform_admin: bool, daimon_admin: bool, action: DelegatedAction | None = None
) -> bool:
    """Delegation requires an explicit allow-listed action; missing actions fail closed."""
    return platform_admin is True or (isinstance(action, DelegatedAction) and daimon_admin is True)


def is_personal_user_credential(
    *, token_kind: str | None, is_chat_execution: bool, execution_bindings: Sequence[object]
) -> bool:
    """Authority and money never accept execution credentials, regardless of their owner.

    Kind and execution classification must come from verified registry evidence.
    A personal CLI or OAuth user credential carries no execution binding.
    """
    return (
        (token_kind is None or token_kind == "cli")
        and is_chat_execution is False
        and all(value is None for value in execution_bindings)
    )
