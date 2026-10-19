# Re-export the most commonly used symbols.
# For module-specific imports, use daimon.testing.ma / .db / .factories directly.
#
# `ma` (and `ma_sessions` / `turn_router`, which import from it) declare
# pytest fixtures at module scope, so importing them requires pytest. The
# deployed image runs `--no-dev` (no pytest) but still needs to import
# modules living in this package, e.g. `daimon.testing.fleet_export` for the
# fleet evidence exporter — and importing *any* submodule here first runs
# this `__init__.py`. Keep those symbols out of the eager imports below and
# resolve them lazily through `__getattr__` so plain `import daimon.testing`
# (or any of its non-`ma`-dependent submodules) never requires pytest; only
# actually touching an `ma`-backed name does.
#
# `factories` and `ma_models` are lazy for a different reason: speed. They pull in
# the ORM schema and the Anthropic SDK models (about a second of imports), and the
# pytest plugin entry point imports this package into every pytest process. A child
# interpreter that never builds a tenant or an MA fixture (a shard collection, a
# plugin-load probe, a `-O` conformance replay) should not pay for them.
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
        make_wizard_session,
    )
    from daimon.testing.ma import (
        FakeMAState,
        FakeMemoryStoreState,
        MARouter,
        NotHandled,
        build_fake_anthropic,
        build_no_retry_anthropic,
        build_stub_anthropic,
        combine_handlers,
        json_body,
        list_response,
        make_agent_env_echo_handler,
        make_archive_agent_handler,
        make_fake_ma_handler,
        make_fake_memory_store_handler,
        not_found_response,
        require_api_key,
        resolved_agent_env_router,
        send_events_response,
        session_response,
        sse_response,
        stub_anthropic,
    )
    from daimon.testing.ma_models import (
        DEFAULT_AGENT_ID,
        DEFAULT_AGENT_NAME,
        DEFAULT_ENV_ID,
        DEFAULT_ENV_NAME,
        DEFAULT_MODEL_ID,
        DEFAULT_SESSION_ID,
        EMPTY_CLOUD_CONFIG,
        EMPTY_SESSION_STATS,
        EMPTY_SESSION_USAGE,
        FIXED_TS,
        FIXED_TS_STR,
        ma_agent,
        ma_environment,
        ma_model_config,
        ma_model_usage,
        ma_session,
        ma_session_agent,
        ma_session_agent_from,
        tenant_metadata,
    )
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
    "make_wizard_session": "daimon.testing.factories",
    "DEFAULT_AGENT_ID": "daimon.testing.ma_models",
    "DEFAULT_AGENT_NAME": "daimon.testing.ma_models",
    "DEFAULT_ENV_ID": "daimon.testing.ma_models",
    "DEFAULT_ENV_NAME": "daimon.testing.ma_models",
    "DEFAULT_MODEL_ID": "daimon.testing.ma_models",
    "DEFAULT_SESSION_ID": "daimon.testing.ma_models",
    "EMPTY_CLOUD_CONFIG": "daimon.testing.ma_models",
    "EMPTY_SESSION_STATS": "daimon.testing.ma_models",
    "EMPTY_SESSION_USAGE": "daimon.testing.ma_models",
    "FIXED_TS": "daimon.testing.ma_models",
    "FIXED_TS_STR": "daimon.testing.ma_models",
    "ma_agent": "daimon.testing.ma_models",
    "ma_environment": "daimon.testing.ma_models",
    "ma_model_config": "daimon.testing.ma_models",
    "ma_model_usage": "daimon.testing.ma_models",
    "ma_session": "daimon.testing.ma_models",
    "ma_session_agent": "daimon.testing.ma_models",
    "ma_session_agent_from": "daimon.testing.ma_models",
    "tenant_metadata": "daimon.testing.ma_models",
    "FakeMAState": "daimon.testing.ma",
    "FakeMemoryStoreState": "daimon.testing.ma",
    "MARouter": "daimon.testing.ma",
    "NotHandled": "daimon.testing.ma",
    "build_fake_anthropic": "daimon.testing.ma",
    "build_no_retry_anthropic": "daimon.testing.ma",
    "build_stub_anthropic": "daimon.testing.ma",
    "combine_handlers": "daimon.testing.ma",
    "json_body": "daimon.testing.ma",
    "list_response": "daimon.testing.ma",
    "make_agent_env_echo_handler": "daimon.testing.ma",
    "make_archive_agent_handler": "daimon.testing.ma",
    "make_fake_ma_handler": "daimon.testing.ma",
    "make_fake_memory_store_handler": "daimon.testing.ma",
    "not_found_response": "daimon.testing.ma",
    "require_api_key": "daimon.testing.ma",
    "resolved_agent_env_router": "daimon.testing.ma",
    "send_events_response": "daimon.testing.ma",
    "session_response": "daimon.testing.ma",
    "sse_response": "daimon.testing.ma",
    "stub_anthropic": "daimon.testing.ma",
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
    "DEFAULT_AGENT_ID",
    "DEFAULT_AGENT_NAME",
    "DEFAULT_ENV_ID",
    "DEFAULT_ENV_NAME",
    "DEFAULT_MODEL_ID",
    "DEFAULT_SESSION_ID",
    "EMPTY_CLOUD_CONFIG",
    "EMPTY_SESSION_STATS",
    "EMPTY_SESSION_USAGE",
    "ENV_ID",
    "FIXED_TS",
    "FIXED_TS_STR",
    "INIT_BODY",
    "INIT_HEADERS",
    "MODEL_ID",
    "FakeMAState",
    "FakeMemoryStoreState",
    "FakeSessionsState",
    "MARouter",
    "NotHandled",
    "asgi_lifespan",
    "build_fake_anthropic",
    "build_no_retry_anthropic",
    "build_stub_anthropic",
    "build_turn_router",
    "call_mcp_tool",
    "combine_handlers",
    "json_body",
    "link_principals",
    "list_response",
    "ma_agent",
    "ma_environment",
    "ma_model_config",
    "ma_model_usage",
    "ma_session",
    "ma_session_agent",
    "ma_session_agent_from",
    "make_account",
    "make_agent_env_echo_handler",
    "make_agent_memory_store",
    "make_archive_agent_handler",
    "make_cli_principal",
    "make_fake_ma_handler",
    "make_fake_memory_store_handler",
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
    "make_wizard_session",
    "mcp_session",
    "not_found_response",
    "parse_jsonrpc_response",
    "require_api_key",
    "resolved_agent_env_router",
    "send_events_response",
    "session_response",
    "session_turn_sse",
    "sse_response",
    "stub_anthropic",
    "tenant_metadata",
    "turn_events",
]
