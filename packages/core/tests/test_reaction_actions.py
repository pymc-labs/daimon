"""Reaction actions: the binding model, the result convention, the mark match and the credential."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt
import pytest
from daimon.core.config import McpIdentitySettings
from daimon.core.reaction_actions import (
    DEFAULT_WARNING,
    MAX_TEXT,
    ReactionAction,
    bindings_for,
    confirm_binding,
    marking_actions,
    message_url,
    outcome_of,
)
from daimon.core.reaction_credential import (
    CredentialError,
    ReactionCall,
    SingleUse,
    mint,
    verify,
)
from daimon.core.turn.state import TextBlock, ToolUseBlock
from pydantic import ValidationError

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SECRET = b"s" * 32
SERVER = {
    "name": "tools",
    "url": "https://tools.example/mcp",
    "auth": "bearer",
    "requester_meta": True,
    "workspaces": ["discord:1"],
    "agents": ["Helper"],
}


def _action(**more: Any) -> ReactionAction:
    fields: dict[str, Any] = {
        "workspace": "discord:1",
        "server": "tools",
        "agent": "Helper",
        "mark_tool": "propose",
        "mark_emoji": "mark:111",
        "confirm_tool": "confirm",
        "confirm_emoji": "done:222",
    }
    return ReactionAction.model_validate({**fields, **more})


def _block(**more: Any) -> ToolUseBlock:
    fields: dict[str, Any] = {
        "kind": "tool_use",
        "id": "t1",
        "type": "agent.mcp_tool_use",
        "name": "propose",
        "input": {},
        "mcp_server_name": "tools",
        "status": "complete",
    }
    return ToolUseBlock(**{**fields, **more})


def test_binding_defaults_and_ids() -> None:
    action = _action()
    assert (action.mark_emoji_id, action.confirm_emoji_id) == (111, 222)
    assert action.warning_emoji == DEFAULT_WARNING


@pytest.mark.parametrize(
    "more",
    [
        {"mark_emoji": "plain"},
        {"confirm_emoji": "done:222", "mark_emoji": "mark:222"},
        {"warning_emoji": "warn"},
        {"warning_emoji": "x:1"},
        {"extra": 1},
    ],
)
def test_a_bad_binding_is_refused(more: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _action(**more)


def test_lookups_are_per_workspace_and_emoji_id() -> None:
    action = _action()
    assert bindings_for([action], "discord:1") == (action,)
    assert bindings_for([action], "discord:2") == ()
    assert confirm_binding([action], workspace="discord:1", emoji_id_=222) is action
    assert confirm_binding([action], workspace="discord:1", emoji_id_=111) is None
    assert confirm_binding([action], workspace="discord:2", emoji_id_=222) is None


def test_the_message_link_names_server_channel_and_message() -> None:
    assert message_url("1", "2", "3") == "https://discord.com/channels/1/2/3"


def test_the_result_convention() -> None:
    assert outcome_of({"content": [{"type": "text", "text": "saved"}]}).status == "ok"
    assert outcome_of({}).status == "ok"
    error = outcome_of({"isError": True, "content": [{"type": "text", "text": "no  way"}]})
    assert (error.status, error.text) == ("error", "no way")
    structured = outcome_of({"structuredContent": {"status": "attention", "message": "Look"}})
    assert (structured.status, structured.text) == ("attention", "Look")
    text = json.dumps({"status": "attention", "message": "Check"})
    assert outcome_of({"content": [{"type": "text", "text": text}]}).status == "attention"
    assert outcome_of({"structuredContent": {"status": "done"}}).status == "ok"
    long = outcome_of({"isError": True, "content": [{"type": "text", "text": "x" * 999}]})
    assert len(long.text) == MAX_TEXT


PUBLIC = "https://daimon.example"
RELAY = f"{PUBLIC}/mcp-identity/tools"


def _marks(blocks: list[ToolUseBlock], action: ReactionAction | None = None, **more: Any) -> Any:
    args: dict[str, Any] = {
        "agent_name": "Helper",
        "server_urls": {"tools": RELAY},
        "public_url": PUBLIC,
    }
    return marking_actions(blocks, [action or _action()], **{**args, **more})


def test_the_mark_needs_a_clean_finished_call_of_the_bound_tool() -> None:
    action = _action()
    assert marking_actions(
        [TextBlock("text", "hi"), _block()],
        [action],
        agent_name="Helper",
        server_urls={"tools": RELAY},
        public_url=PUBLIC,
    ) == (action,)
    assert _marks([_block(status="failed")]) == ()
    assert _marks([_block(status="pending")]) == ()
    assert _marks([_block(is_error=True)]) == ()
    assert _marks([_block(mcp_server_name="other")]) == ()
    assert _marks([_block(name="confirm")]) == ()


def test_the_mark_trusts_the_agent_and_the_relay_url_not_the_label() -> None:
    assert _marks([_block()]) != ()
    # The agent must be the bound agent.
    assert _marks([_block()], agent_name="Other") == ()
    # Another agent's server labelled "tools" points at another host or relay name.
    assert (
        _marks([_block()], server_urls={"tools": "https://evil.example/mcp-identity/tools"}) == ()
    )
    assert _marks([_block()], server_urls={"tools": f"{PUBLIC}/mcp-identity/other"}) == ()
    assert _marks([_block()], server_urls={"tools": "https://tools.example/mcp"}) == ()
    # A label chosen freely still counts when its URL is the bound relay.
    assert _marks([_block(mcp_server_name="mine")], server_urls={"mine": RELAY}) != ()
    # No label for the block, or no public URL, fails closed.
    assert _marks([_block()], server_urls={}) == ()
    assert _marks([_block()], public_url=None) == ()


def test_an_attention_result_is_not_marked() -> None:
    from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
        BetaManagedAgentsTextBlock,
    )

    text = json.dumps({"status": "attention", "message": "Look"})
    block = _block(result_content=[BetaManagedAgentsTextBlock(type="text", text=text)])
    assert _marks([block]) == ()


def _call(**more: str) -> ReactionCall:
    fields = {
        "workspace": "discord:1",
        "server": "tools",
        "tool": "confirm",
        "agent": "Helper",
        "user": "9",
        "guild": "1",
        "channel": "2",
        "message": "3",
    }
    return ReactionCall(**{**fields, **more})


def test_a_credential_round_trips_and_names_one_call() -> None:
    token = mint(_call(parent="7"), secret=SECRET, now=NOW)
    got = verify(token, secret=SECRET, now=NOW + timedelta(seconds=10))
    assert got.jti
    assert replace(got, jti="") == _call(parent="7")
    assert mint(_call(), secret=SECRET, now=NOW) != mint(_call(), secret=SECRET, now=NOW)


def test_a_credential_can_be_claimed_once() -> None:
    used = SingleUse()
    assert used.claim("a", now=NOW)
    assert not used.claim("a", now=NOW + timedelta(seconds=1))
    assert used.claim("b", now=NOW)
    # An id is forgotten only after its credential would have expired.
    assert used.claim("a", now=NOW + timedelta(seconds=17))


def test_a_credential_is_refused_when_expired_forged_or_not_ours() -> None:
    token = mint(_call(), secret=SECRET, now=NOW)
    with pytest.raises(CredentialError):
        verify(token, secret=SECRET, now=NOW + timedelta(seconds=16))
    with pytest.raises(CredentialError):
        verify(token, secret=b"t" * 32, now=NOW)
    with pytest.raises(CredentialError):
        verify(token, secret=SECRET, now=NOW - timedelta(seconds=5))
    with pytest.raises(CredentialError):
        verify("garbage", secret=SECRET, now=NOW)
    claims: dict[str, Any] = pyjwt.decode(token, options={"verify_signature": False})
    for edit in ({"sub": "account"}, {"purpose": "other"}, {"aud": "other"}, {"user": ""}):
        forged = pyjwt.encode({**claims, **edit}, SECRET, algorithm="HS256")
        with pytest.raises(CredentialError):
            verify(forged, secret=SECRET, now=NOW)
    long_lived = pyjwt.encode({**claims, "exp": claims["exp"] + 3600}, SECRET, algorithm="HS256")
    with pytest.raises(CredentialError):
        verify(long_lived, secret=SECRET, now=NOW)
    # An ordinary account credential is not a reaction credential.
    account = pyjwt.encode({"sub": "a", "iat": 1}, SECRET, algorithm="HS256")
    with pytest.raises(CredentialError):
        verify(account, secret=SECRET, now=NOW)


def _settings(server: dict[str, object] | None = None, **action: object) -> McpIdentitySettings:
    chosen = server or SERVER
    return McpIdentitySettings.model_validate(
        {
            "servers": [chosen],
            "bearers": {"tools": "key"} if chosen.get("auth") == "bearer" else {},
            "reaction_actions": [_action(**action).model_dump()],
        }
    )


def test_the_setting_is_parsed_from_json_and_defaults_off() -> None:
    assert McpIdentitySettings().reaction_actions == []
    text = json.dumps([_action().model_dump()])
    parsed = McpIdentitySettings.model_validate(
        {"servers": [SERVER], "bearers": {"tools": "key"}, "reaction_actions": text}
    )
    assert parsed.reaction_actions == [_action()]


@pytest.mark.parametrize(
    "server",
    [
        {**SERVER, "requester_meta": False},
        {**SERVER, "workspaces": ["discord:2"]},
        {**SERVER, "agents": ["Other"]},
        {"name": "tools", "url": "https://t.example/mcp", "issuer": "i", "audience": "a"},
    ],
)
def test_an_action_needs_a_bearer_server_that_stamps_and_allows_it(
    server: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="reaction action"):
        _settings(server)
    with pytest.raises(ValidationError, match="reaction action"):
        McpIdentitySettings.model_validate(
            {"servers": [], "reaction_actions": [_action().model_dump()]}
        )
