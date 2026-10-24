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
from mux.testing.drivers.anthropic.ma import (
    FakeMemoryStoreState,
    build_fake_anthropic,
    combine_handlers,
    make_fake_ma_handler,
    make_fake_memory_store_handler,
)
from slack_sdk.errors import SlackApiError

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
    web.users_info = AsyncMock(return_value={"user": {"team_id": TEAM_ID}})
    web.conversations_info = AsyncMock(return_value={"channel": {"is_ext_shared": False}})
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
    runtime, web = await _setup(db_session, db_session_factory, seed={"/big.md": huge_content})
    with patch("daimon.adapters.slack.memory.resolve_web_client", AsyncMock(return_value=web)):
        await handle_memory_command(runtime, _payload("/big.md"))
    kwargs = web.chat_postEphemeral.call_args.kwargs
    text = kwargs["text"]
    assert len(text) <= 4000, f"exceeds Slack's hard cap: {len(text)} chars"
    assert text.rstrip().endswith("```"), f"fence not closed: {text[-40:]!r}"
    assert "truncated" in text


async def test_memory_show_escapes_embedded_fences(db_session, db_session_factory) -> None:
    """Content containing ``` (agents store markdown with code blocks) must
    not break out of the wrapping fence: exactly one opening and one closing
    fence in the final message."""
    content = "notes\n```python\nprint('hi')\n```\nmore notes"
    runtime, web = await _setup(db_session, db_session_factory, seed={"/code.md": content})
    with patch("daimon.adapters.slack.memory.resolve_web_client", AsyncMock(return_value=web)):
        await handle_memory_command(runtime, _payload("/code.md"))
    text = web.chat_postEphemeral.call_args.kwargs["text"]
    assert text.count("```") == 2, f"embedded fence broke out: {text!r}"
    assert "print('hi')" in text


async def test_memory_empty_state(db_session, db_session_factory) -> None:
    runtime, web = await _setup(db_session, db_session_factory, seed={})
    with patch("daimon.adapters.slack.memory.resolve_web_client", AsyncMock(return_value=web)):
        await handle_memory_command(runtime, _payload())
    kwargs = web.chat_postEphemeral.call_args.kwargs
    assert "no memory to show here." in kwargs["text"].lower()


async def test_memory_missing_agent_surfaces_ephemeral_error(
    db_session, db_session_factory
) -> None:
    """When the configured agent doesn't exist on the MA side, _resolve_store
    raises DaimonError — the user must see that message as an ephemeral reply
    rather than the failure only being logged."""
    runtime, web = await _setup(db_session, db_session_factory, seed={})
    runtime.deployment_default = DeploymentDefault(agent_name="ghost", environment_name="default")
    with patch("daimon.adapters.slack.memory.resolve_web_client", AsyncMock(return_value=web)):
        await handle_memory_command(runtime, _payload())
    kwargs = web.chat_postEphemeral.call_args.kwargs
    assert "ghost" in kwargs["text"]
    assert "not found" in kwargs["text"].lower()


async def test_memory_hides_an_agent_isolation_keeps_out(db_session, db_session_factory) -> None:
    """In an isolated channel only its own agents are visible, so a shared agent's memory isn't."""
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy

    runtime, web = await _setup(db_session, db_session_factory, seed={"/a.md": "alpha"})
    await set_access_policy(
        db_session,
        tenant_id=derive_tenant_uuid(platform="slack", workspace_id=TEAM_ID),
        policy=TenantAccessPolicy(sealed_channel_ids=("C1",), isolated_channel_ids=("C1",)),
    )
    await db_session.commit()
    with patch("daimon.adapters.slack.memory.resolve_web_client", AsyncMock(return_value=web)):
        await handle_memory_command(runtime, _payload())
    text = web.chat_postEphemeral.call_args.kwargs["text"]
    assert "no memory to show here." in text.lower(), "the deployment default answers elsewhere too"


async def _seed_personal(db_session, runtime, *, user_id: str, files: dict[str, str]) -> str:
    """Bind a personal store for the Slack user, as their first chat session would."""
    from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
    from daimon.core.stores.agent_memory_stores import insert_personal_memory_store
    from daimon.core.stores.identity import get_or_create_platform_principal

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=TEAM_ID)
    agent = await find_agent_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name="daimon")
    assert agent is not None
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant_id, platform="slack", external_id=user_id
    )
    store = await runtime.anthropic.beta.memory_stores.create(name=f"personal {user_id}")
    for path, content in files.items():
        await runtime.anthropic.beta.memory_stores.memories.create(
            store.id, path=path, content=content
        )
    await insert_personal_memory_store(
        db_session,
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id)),
        account_id=principal.account_id,
        memory_store_id=store.id,
    )
    await db_session.commit()
    return store.id


async def test_memory_mine_and_forget_act_only_on_the_invokers_own_store(
    db_session, db_session_factory
) -> None:
    import re

    runtime, web = await _setup(db_session, db_session_factory, seed={"/team.md": "fridays"})
    mine = await _seed_personal(db_session, runtime, user_id="U1", files={"/style.md": "tabs"})
    theirs = await _seed_personal(db_session, runtime, user_id="U2", files={"/x.md": "private"})

    async def say(user_id: str, text: str) -> str:
        payload = {**_payload(text), "user_id": user_id}
        with patch("daimon.adapters.slack.memory.resolve_web_client", AsyncMock(return_value=web)):
            await handle_memory_command(runtime, payload)
        kwargs = web.chat_postEphemeral.call_args.kwargs
        assert kwargs["user"] == user_id, "ephemeral, to the invoker only"
        return kwargs["text"]

    async def paths(store_id: str) -> list[str]:
        page = await runtime.anthropic.beta.memory_stores.memories.list(store_id, path_prefix="/")
        return [item.path async for item in page]

    listing = await say("U2", "mine")
    assert "/x.md" in listing and "/style.md" not in listing

    asked = await say("U1", "forget-mine")
    found = re.search(r"forget-mine ([0-9a-f]{6})", asked)
    assert found is not None, "the preview offers the code"
    code = found.group(1)
    assert await paths(mine) == ["/style.md"], "asking deletes nothing"

    await say("U2", f"forget-mine {code}")
    assert await paths(mine) == ["/style.md"], "U1's code does nothing for U2"
    assert await paths(theirs) == ["/x.md"]

    done = await say("U1", f"forget-mine {code}")
    assert "Deleted 1 file " in done
    assert await paths(mine) == [] and await paths(theirs) == ["/x.md"]


async def _hold_channel(
    db_session, runtime, *, shared_files: dict[str, str], mark: bool = True
) -> None:
    """Bind a channel store holding `shared_files` for the agent; mark C1 shared if `mark`."""
    from daimon.core.stores.agent_memory_stores import insert_channel_memory_store
    from daimon.core.stores.shared_channels import mark_channel_shared

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=TEAM_ID)
    (agent,) = [a async for a in runtime.anthropic.beta.agents.list() if a.name == "daimon"]
    store = await runtime.anthropic.beta.memory_stores.create(name="channel", description="d")
    for p, c in shared_files.items():
        await runtime.anthropic.beta.memory_stores.memories.create(store.id, path=p, content=c)
    await insert_channel_memory_store(
        db_session,
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id)),
        platform="slack",
        channel_id="C1",
        memory_store_id=store.id,
    )
    if mark:
        await mark_channel_shared(
            db_session, tenant_id=tenant_id, platform="slack", channel_id="C1", source="platform"
        )
    await db_session.commit()


async def test_memory_in_a_shared_channel_shows_the_channel_store_not_the_agent_wide_one(
    db_session, db_session_factory
) -> None:
    runtime, web = await _setup(db_session, db_session_factory, seed={"/wide.md": "internal"})
    await _hold_channel(db_session, runtime, shared_files={"/channel.md": "agreed here"})
    with patch("daimon.adapters.slack.memory.resolve_web_client", AsyncMock(return_value=web)):
        await handle_memory_command(runtime, _payload())
        await handle_memory_command(runtime, _payload("/channel.md"))
        await handle_memory_command(runtime, _payload("/wide.md"))
    listing, shown, wide = (call.kwargs["text"] for call in web.chat_postEphemeral.call_args_list)
    assert "/channel.md" in listing and "/wide.md" not in listing
    assert "agreed here" in shown
    assert "No memory file called" in wide, "agent-wide files are not reachable from here"


async def test_personal_memory_commands_say_it_is_not_used_in_a_shared_channel(
    db_session, db_session_factory
) -> None:
    runtime, web = await _setup(db_session, db_session_factory, seed={})
    await _hold_channel(db_session, runtime, shared_files={"/channel.md": "agreed here"})
    with patch("daimon.adapters.slack.memory.resolve_web_client", AsyncMock(return_value=web)):
        for text in ("mine", "mine /a.md", "forget-mine", "forget-mine abc123"):
            await handle_memory_command(runtime, _payload(text))
    for call in web.chat_postEphemeral.call_args_list:
        assert "Personal memory isn't used in this channel." in call.kwargs["text"]


def _lookup_failure() -> SlackApiError:
    return SlackApiError("boom", {"ok": False, "error": "ratelimited"})


@pytest.mark.parametrize(
    "unproven",
    ["other-team", "users-info-fails", "no-team-in-reply", "live-shared-channel"],
)
async def test_memory_for_an_asker_not_proven_a_member_never_shows_agent_wide_memory(
    unproven: str, db_session, db_session_factory
) -> None:
    """Nothing is recorded about the asker or the channel yet, so the live Slack
    facts decide: only a workspace member in a channel Slack does not call shared
    sees agent-wide memory."""
    runtime, web = await _setup(db_session, db_session_factory, seed={"/wide.md": "internal"})
    await _hold_channel(db_session, runtime, shared_files={"/channel.md": "agreed"}, mark=False)
    if unproven == "other-team":
        web.users_info.return_value = {"user": {"team_id": "T-OTHER"}}
    elif unproven == "users-info-fails":
        web.users_info.side_effect = _lookup_failure()
    elif unproven == "no-team-in-reply":
        web.users_info.return_value = {"user": {}}
    else:
        web.conversations_info.return_value = {"channel": {"is_ext_shared": True}}
    with patch("daimon.adapters.slack.memory.resolve_web_client", AsyncMock(return_value=web)):
        await handle_memory_command(runtime, _payload())
        await handle_memory_command(runtime, _payload("mine"))
    listing, mine = (call.kwargs["text"] for call in web.chat_postEphemeral.call_args_list)
    assert "/channel.md" in listing and "/wide.md" not in listing
    assert "Personal memory isn't used in this channel." in mine


async def test_memory_for_a_member_when_slack_lookups_fail_on_the_channel_uses_the_registry(
    db_session, db_session_factory
) -> None:
    runtime, web = await _setup(db_session, db_session_factory, seed={"/wide.md": "internal"})
    web.conversations_info.side_effect = _lookup_failure()
    with patch("daimon.adapters.slack.memory.resolve_web_client", AsyncMock(return_value=web)):
        await handle_memory_command(runtime, _payload())
    assert "/wide.md" in web.chat_postEphemeral.call_args.kwargs["text"]
