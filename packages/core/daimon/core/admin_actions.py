"""The exhaustive set of actions for which live Daimon delegation applies.

Everything outside this enum requires platform authority. These verdicts are
request-local evidence, never account roles or persisted authorization.
"""

from collections.abc import Sequence
from enum import StrEnum

BILLING_PANEL_TOKEN_LABEL = "billing-panel"


class DelegatedAction(StrEnum):
    AGENT_SETTINGS = "agent_settings"
    CHANNEL_PERMISSIONS = "channel_permissions"
    ROUTINES = "routines"
    SKILLS = "skills"
    ENVIRONMENTS = "environments"
    MODEL = "model"
    ROUTING = "routing"
    TOOL_RULES = "tool_rules"
    AGENT_LIFECYCLE = "agent_lifecycle"
    HUB_PIN_EXEMPTION = "hub_pin_exemption"


def admin_for_action(
    *, platform_admin: bool, daimon_admin: bool, action: DelegatedAction | None = None
) -> bool:
    """Delegation requires an explicit allow-listed action; missing actions fail closed."""
    return platform_admin is True or (isinstance(action, DelegatedAction) and daimon_admin is True)


def is_personal_user_credential(
    *,
    token_kind: str | None,
    is_chat_execution: bool,
    execution_bindings: Sequence[object],
    token_label: str | None = None,
) -> bool:
    """Only registered personal CLI credentials may exercise human authority.

    Kind and label come exclusively from the verified registry row. Billing
    panel credentials are server-to-server checkout grants, never personal.
    """
    return token_label != BILLING_PANEL_TOKEN_LABEL and is_checkout_credential(
        token_kind=token_kind,
        is_chat_execution=is_chat_execution,
        execution_bindings=execution_bindings,
    )


def is_checkout_credential(
    *, token_kind: str | None, is_chat_execution: bool, execution_bindings: Sequence[object]
) -> bool:
    """Checkout also accepts the short-lived token from a live-checked human panel.

    No legacy or execution credential can reach checkout. The checkout route
    additionally binds the account and re-attests its live platform authority.
    """
    return (
        token_kind == "cli"
        and is_chat_execution is False
        and all(value is None for value in execution_bindings)
    )
