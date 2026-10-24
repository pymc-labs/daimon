"""Optional provider calls fail before client access and isolate concurrent requests."""

import asyncio
from typing import cast

import pytest
from anthropic import AsyncAnthropic
from daimon.core.anthropic_guard import (
    AnthropicDisabled,
    require_anthropic_allowed,
    without_anthropic,
)
from daimon.core.mux_backend import (
    TurnBackendRequest,
    managed_agents,
    resource_scope,
    turn_backend,
)
from daimon.core.mux_compat import retrieve_agent, retrieve_environment
from daimon.core.output_ports_compat import list_output_records
from daimon.core.session_ports_compat import retrieve_session_record
from daimon.core.thread_classifier import classify
from daimon.core.thread_naming import suggest_thread_name


class NoAnthropic:
    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"guard touched Anthropic.{name}")


async def test_no_anthropic_guard_covers_messages_and_legacy_resource_helpers() -> None:
    client = cast(AsyncAnthropic, NoAnthropic())
    with without_anthropic():
        with pytest.raises(AnthropicDisabled):
            managed_agents(client)
        scope = resource_scope(tenant_id="tenant")
        with pytest.raises(AnthropicDisabled):
            turn_backend(
                TurnBackendRequest(
                    profile="anthropic.managed_agents",
                    client=client,
                    scope=scope,
                    session_id="session",
                )
            )
        for operation in (
            retrieve_agent(client, "agent", scope=scope),
            retrieve_environment(client, "environment", scope=scope),
            retrieve_session_record(client, "session", scope=scope),
            list_output_records(client, "session", scope=scope),
        ):
            with pytest.raises(AnthropicDisabled):
                await operation
        with pytest.raises(AnthropicDisabled):
            await suggest_thread_name(client, message_text="private", max_input_chars=100)
        with pytest.raises(AnthropicDisabled):
            await classify(
                client, model="claude-test", bot_display_name="bot", recent=[], candidates=[]
            )
    require_anthropic_allowed()


async def test_no_anthropic_guard_is_inherited_and_does_not_leak_between_tasks() -> None:
    entered, checked = asyncio.Event(), asyncio.Event()

    async def selfhosted() -> None:
        with without_anthropic():
            entered.set()
            await checked.wait()
            with pytest.raises(AnthropicDisabled):
                await asyncio.create_task(probe())

    async def probe() -> None:
        require_anthropic_allowed()

    async def ordinary() -> None:
        await entered.wait()
        require_anthropic_allowed()
        checked.set()

    await asyncio.gather(selfhosted(), ordinary())
    require_anthropic_allowed()
