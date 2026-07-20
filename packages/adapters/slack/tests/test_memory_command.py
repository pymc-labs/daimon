"""Slack /memory handler — list, show, empty state."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from daimon.adapters.slack.memory import handle_memory_command
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_memory_stores import insert_memory_store
from daimon.testing.factories import make_tenant
from daimon.testing.ma import (
    FakeMemoryStoreState,
    build_fake_anthropic,
    combine_handlers,
    make_fake_ma_handler,
    make_fake_memory_store_handler,
)

pytestmark = pytest.mark.asyncio

TEAM_ID = "T123"


async def _setup(db_session, db_session_factory, *, seed: dict[str, str]):
    # make_tenant derives tenant_id from (platform, workspace_id) itself — it
    # doesn't take an id= override — so passing the same workspace_id the
    # command derives from (TEAM_ID) with platform="slack" reproduces the
    # exact tenant_id the command will compute (same approach as the Discord
    # /memory tests).
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=TEAM_ID)
    tenant = await make_tenant(db_session, platform="slack", workspace_id=TEAM_ID)
    assert tenant.id == tenant_id
    mem_state = FakeMemoryStoreState()
    client = build_fake_anthropic(
        combine_handlers(make_fake_memory_store_handler(mem_state), make_fake_ma_handler())
    )
    agent = await client.beta.agents.create(
        name="daimon",
        model="claude-sonnet-4-6",
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": "daimon"},
    )
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id))
    store = await client.beta.memory_stores.create(name="m", description="d")
    for p, c in seed.items():
        await client.beta.memory_stores.memories.create(store.id, path=p, content=c)
    await insert_memory_store(
        db_session, tenant_id=tenant_id, agent_id=agent_uuid, memory_store_id=store.id
    )
    await db_session.commit()

    runtime = MagicMock(spec=SlackRuntime)
    runtime.anthropic = client
    runtime.sessionmaker = db_session_factory
    runtime.deployment_default = DeploymentDefault(agent_name="daimon", environment_name="default")
    web = MagicMock()
    web.chat_postEphemeral = AsyncMock()
    return runtime, web


def _payload(text: str = "") -> dict[str, str]:
    return {"team_id": TEAM_ID, "user_id": "U1", "channel_id": "C1", "text": text}


async def test_memory_list(db_session, db_session_factory) -> None:
    runtime, web = await _setup(db_session, db_session_factory, seed={"/a.md": "alpha"})
    with patch("daimon.adapters.slack.memory.resolve_web_client", AsyncMock(return_value=web)):
        await handle_memory_command(runtime, _payload())
    kwargs = web.chat_postEphemeral.call_args.kwargs
    assert "/a.md" in kwargs["text"]


async def test_memory_show(db_session, db_session_factory) -> None:
    runtime, web = await _setup(db_session, db_session_factory, seed={"/a.md": "alpha"})
    with patch("daimon.adapters.slack.memory.resolve_web_client", AsyncMock(return_value=web)):
        await handle_memory_command(runtime, _payload("/a.md"))
    kwargs = web.chat_postEphemeral.call_args.kwargs
    assert "alpha" in kwargs["text"]


async def test_memory_show_truncates_content_with_closed_fence(
    db_session, db_session_factory
) -> None:
    """Boundary case: content ~2x the platform limit must still render under
    Slack's hard 4000-char cap with a CLOSED code fence — never truncated
    mid-fence, which would corrupt rendering for the rest of the message."""
    huge_content = "x" * 7600  # ~2x _SLACK_LIMIT (3800)
    runtime, web = await _setup(
        db_session, db_session_factory, seed={"/big.md": huge_content}
    )
    with patch("daimon.adapters.slack.memory.resolve_web_client", AsyncMock(return_value=web)):
        await handle_memory_command(runtime, _payload("/big.md"))
    kwargs = web.chat_postEphemeral.call_args.kwargs
    text = kwargs["text"]
    assert len(text) <= 4000, f"exceeds Slack's hard cap: {len(text)} chars"
    assert text.rstrip().endswith("```"), f"fence not closed: {text[-40:]!r}"
    assert "truncated" in text


async def test_memory_empty_state(db_session, db_session_factory) -> None:
    runtime, web = await _setup(db_session, db_session_factory, seed={})
    with patch("daimon.adapters.slack.memory.resolve_web_client", AsyncMock(return_value=web)):
        await handle_memory_command(runtime, _payload())
    kwargs = web.chat_postEphemeral.call_args.kwargs
    assert "no memories" in kwargs["text"].lower()


async def test_memory_missing_agent_surfaces_ephemeral_error(
    db_session, db_session_factory
) -> None:
    """When the configured agent doesn't exist on the MA side, _resolve_store
    raises DaimonError — the user must see that message as an ephemeral reply
    rather than the failure only being logged."""
    runtime, web = await _setup(db_session, db_session_factory, seed={})
    runtime.deployment_default = DeploymentDefault(
        agent_name="ghost", environment_name="default"
    )
    with patch("daimon.adapters.slack.memory.resolve_web_client", AsyncMock(return_value=web)):
        await handle_memory_command(runtime, _payload())
    kwargs = web.chat_postEphemeral.call_args.kwargs
    assert "ghost" in kwargs["text"]
    assert "not found" in kwargs["text"].lower()
