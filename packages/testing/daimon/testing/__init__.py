from typing import TYPE_CHECKING

from daimon.testing.archives import make_tarball
from daimon.testing.asgi import (
    INIT_BODY,
    INIT_HEADERS,
    asgi_lifespan,
    call_mcp_tool,
    mcp_session,
    parse_jsonrpc_response,
)
from daimon.testing.crypto import make_fernet

if TYPE_CHECKING:
    # Only for type checkers; at runtime these resolve through `__getattr__`.
    from daimon.testing.factories import (
        link_principals,
        make_account,
        make_agent_memory_store,
        make_cli_principal,
        make_ledger_entry,
        make_mcp_token,
        make_platform_principal,
        make_routine,
        make_slack_user_token,
        make_tenant,
        make_tenant_config,
        make_tenant_user_cap,
        make_thread_session,
        make_usage_event,
        make_web_person,
        make_wizard_session,
    )
    from daimon.testing.ma import (
        make_agent_env_echo_handler,
        require_api_key,
        resolved_agent_env_router,
    )
    from daimon.testing.ma_models import ma_agent, ma_environment, tenant_metadata
    from daimon.testing.ma_sessions import (
        FakeSessionsState,
        make_fake_sessions_handler,
        session_turn_sse,
    )
    from daimon.testing.turn_router import (
        AGENT_ID,
        AGENT_TEXT,
        ENV_ID,
        MODEL_ID,
        build_turn_router,
        turn_events,
    )

_LAZY_MODULES_BY_NAME = {
    "link_principals": "daimon.testing.factories",
    "make_account": "daimon.testing.factories",
    "make_agent_memory_store": "daimon.testing.factories",
    "make_cli_principal": "daimon.testing.factories",
    "make_ledger_entry": "daimon.testing.factories",
    "make_mcp_token": "daimon.testing.factories",
    "make_platform_principal": "daimon.testing.factories",
    "make_routine": "daimon.testing.factories",
    "make_slack_user_token": "daimon.testing.factories",
    "make_tenant": "daimon.testing.factories",
    "make_tenant_config": "daimon.testing.factories",
    "make_tenant_user_cap": "daimon.testing.factories",
    "make_thread_session": "daimon.testing.factories",
    "make_usage_event": "daimon.testing.factories",
    "make_web_person": "daimon.testing.factories",
    "make_wizard_session": "daimon.testing.factories",
    "ma_agent": "daimon.testing.ma_models",
    "ma_environment": "daimon.testing.ma_models",
    "tenant_metadata": "daimon.testing.ma_models",
    "make_agent_env_echo_handler": "daimon.testing.ma",
    "require_api_key": "daimon.testing.ma",
    "resolved_agent_env_router": "daimon.testing.ma",
    "FakeSessionsState": "daimon.testing.ma_sessions",
    "make_fake_sessions_handler": "daimon.testing.ma_sessions",
    "session_turn_sse": "daimon.testing.ma_sessions",
    "AGENT_ID": "daimon.testing.turn_router",
    "AGENT_TEXT": "daimon.testing.turn_router",
    "ENV_ID": "daimon.testing.turn_router",
    "MODEL_ID": "daimon.testing.turn_router",
    "build_turn_router": "daimon.testing.turn_router",
    "turn_events": "daimon.testing.turn_router",
}


def __getattr__(name: str) -> object:
    module_name = _LAZY_MODULES_BY_NAME.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_MODULES_BY_NAME))


__all__ = [
    "AGENT_ID",
    "AGENT_TEXT",
    "ENV_ID",
    "INIT_BODY",
    "INIT_HEADERS",
    "MODEL_ID",
    "FakeSessionsState",
    "asgi_lifespan",
    "build_turn_router",
    "call_mcp_tool",
    "link_principals",
    "ma_agent",
    "ma_environment",
    "make_account",
    "make_agent_env_echo_handler",
    "make_agent_memory_store",
    "make_cli_principal",
    "make_fake_sessions_handler",
    "make_fernet",
    "make_ledger_entry",
    "make_mcp_token",
    "make_platform_principal",
    "make_routine",
    "make_slack_user_token",
    "make_tarball",
    "make_tenant",
    "make_tenant_config",
    "make_tenant_user_cap",
    "make_thread_session",
    "make_usage_event",
    "make_web_person",
    "make_wizard_session",
    "mcp_session",
    "parse_jsonrpc_response",
    "require_api_key",
    "resolved_agent_env_router",
    "session_turn_sse",
    "tenant_metadata",
    "turn_events",
]
