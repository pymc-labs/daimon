"""The one unattended gate: its snapshot rewrite and its decider."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.core.mux_backend import platform_scope
from daimon.core.tool_safety import (
    ToolAnnotations,
    ToolCall,
    ToolSafetyPolicy,
)
from daimon.core.turn.posture import PolicyApproval
from daimon.core.turn.unattended import (
    gate_session_snapshot,
    protected_tools,
    unattended_posture,
)
from daimon.testing import ma_agent
from daimon.testing.ma import MARouter
from mux.testing.drivers.anthropic.ma import build_fake_anthropic, json_body, ma_session

_LINEAR = {
    "type": "mcp_toolset",
    "mcp_server_name": "linear",
    "configs": [
        {"name": "create_issue", "enabled": True, "permission_policy": {"type": "always_allow"}}
    ],
    "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
}


def _session() -> Any:
    return ma_session(
        id="ses_gate",
        agent=ma_agent(
            tools=[_LINEAR],
            mcp_servers=[{"name": "linear", "type": "url", "url": "https://linear.example/mcp"}],
        ),
    )


def _call(tool: str) -> ToolCall:
    return ToolCall(tool_use_id="tu_1", server_name="linear", tool_name=tool)


@pytest.mark.parametrize(
    ("policy", "tool", "allowed"),
    [
        (ToolSafetyPolicy(enabled=False), "create_issue", True),
        (ToolSafetyPolicy(enabled=True), "create_issue", False),
        (ToolSafetyPolicy(enabled=True), "get_issue", True),
        (ToolSafetyPolicy(enabled=True, unattended_writes=("linear",)), "create_issue", True),
    ],
)
async def test_posture_is_always_the_unattended_decider(
    policy: ToolSafetyPolicy, tool: str, allowed: bool
) -> None:
    """Never `AutoApprove`: off is the explicit policy, and the decider reads it."""
    posture = unattended_posture(
        policy,
        session=_session(),
        public_url=None,
        session_factory=MagicMock(),
        tenant_id=uuid.uuid4(),
    )
    assert isinstance(posture, PolicyApproval)
    assert (await posture.decide(_call(tool))).allow is allowed


@pytest.mark.parametrize("trusted", [True, False])
async def test_posture_reads_annotations_only_through_its_seam(trusted: bool) -> None:
    seen: list[str] = []

    async def lookup(call: ToolCall) -> ToolAnnotations | None:
        seen.append(call.key)
        return ToolAnnotations(read_only_hint=True)

    posture = unattended_posture(
        ToolSafetyPolicy(enabled=True, trusted_annotations=("linear",) if trusted else ()),
        session=_session(),
        public_url=None,
        session_factory=MagicMock(),
        tenant_id=uuid.uuid4(),
        annotations_for=lookup,
    )
    assert (await posture.decide(_call("memory_flag_list"))).allow is trusted
    assert seen == (["linear/memory_flag_list"] if trusted else [])


@pytest.mark.parametrize("enabled", [False, True])
async def test_gate_rewrites_default_and_per_tool_configs_policy_on_or_off(
    enabled: bool,
) -> None:
    """Off too: every MCP call pauses so the decider (which then allows it) sees it."""
    updates: list[dict[str, Any]] = []

    def update(request: httpx.Request, _m: Any) -> httpx.Response:
        updates.append(json_body(request)["agent"])
        return httpx.Response(200, json=_session().model_dump(mode="json"))

    router = MARouter()
    router.add("POST", r"/v1/sessions/ses_gate$", update)
    client = build_fake_anthropic(router.dispatch)
    try:
        await gate_session_snapshot(
            client,
            _session(),
            ToolSafetyPolicy(enabled=enabled),
            scope=platform_scope("test unattended gate"),
            public_url=None,
            session_factory=MagicMock(),
            tenant_id=uuid.uuid4(),
        )
    finally:
        await client.close()
    [agent] = updates
    toolset = agent["tools"][0]
    assert toolset["default_config"]["permission_policy"] == {"type": "always_ask"}
    assert [c["permission_policy"] for c in toolset["configs"]] == [{"type": "always_ask"}]
    assert agent["mcp_servers"][0]["url"] == "https://linear.example/mcp"


def _with_daimon(url: str) -> Any:
    return ma_session(
        id="ses_gate",
        agent=ma_agent(
            tools=[
                _LINEAR,
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "daimon-mcp",
                    "configs": [],
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                },
            ],
            mcp_servers=[
                {"name": "linear", "type": "url", "url": "https://linear.example/mcp"},
                {"name": "daimon-mcp", "type": "url", "url": url},
            ],
        ),
    )


async def _gate(session: Any, policy: ToolSafetyPolicy, *, status: int = 200) -> list[Any]:
    updates: list[dict[str, Any]] = []

    def update(request: httpx.Request, _m: Any) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"type": "error", "error": {"type": "x"}})
        updates.append(json_body(request)["agent"])
        return httpx.Response(200, json=session.model_dump(mode="json"))

    router = MARouter()
    router.add("POST", r"/v1/sessions/ses_gate$", update)
    client = build_fake_anthropic(router.dispatch)
    try:
        await gate_session_snapshot(
            client,
            session,
            policy,
            scope=platform_scope("test unattended gate"),
            public_url="https://daimon.example/mcp",
            session_factory=MagicMock(),
            tenant_id=uuid.uuid4(),
        )
    finally:
        await client.close()
    return updates


@pytest.mark.parametrize("enabled", [False, True])
async def test_gate_heals_a_foreign_reserved_server_policy_on_or_off(enabled: bool) -> None:
    [agent] = await _gate(
        _with_daimon("https://evil.example/mcp"), ToolSafetyPolicy(enabled=enabled)
    )
    assert [s["url"] for s in agent["mcp_servers"]] == [
        "https://linear.example/mcp",
        "https://daimon.example/mcp",
    ]
    daimon = agent["tools"][1]
    asked = {c["name"] for c in daimon["configs"]}
    assert {"request_agent_backend_switch", "microsoft_send", "lead_intake_resolve"} <= asked


async def test_a_failed_gate_raises() -> None:
    import anthropic

    with pytest.raises(anthropic.BadRequestError):
        await _gate(
            _with_daimon("https://evil.example/mcp"), ToolSafetyPolicy(enabled=True), status=400
        )


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    ("tool", "tool_input", "allowed"),
    [
        ("request_agent_backend_switch", {"preview_digest": "d"}, False),
        ("microsoft_send", {"action": "send_mail"}, False),
        (
            "lead_intake_resolve",
            {"intake_id": "opaque", "resolution": {"contact_id": "201"}},
            False,
        ),
        ("create_watch", {"source": "poll"}, False),
        ("resume_watch", {"watch_id": "id"}, False),
        ("rotate_watch_key", {"watch_id": "id"}, False),
        ("request_agent_backend_switch", {}, True),
    ],
)
async def test_posture_refuses_a_protected_final_action(
    enabled: bool, tool: str, tool_input: dict[str, object], allowed: bool
) -> None:
    posture = unattended_posture(
        ToolSafetyPolicy(enabled=enabled),
        session=_with_daimon("https://daimon.example/mcp"),
        public_url="https://daimon.example/mcp",
        session_factory=MagicMock(),
        tenant_id=uuid.uuid4(),
    )
    call = ToolCall(tool_use_id="tu", server_name="daimon-mcp", tool_name=tool, input=tool_input)
    assert (await posture.decide(call)).allow is allowed


@pytest.mark.parametrize(("enabled", "allowed"), [(True, False), (False, True)])
async def test_microsoft_write_is_protected_only_with_safety_on(
    enabled: bool, allowed: bool
) -> None:
    """As in chat: file changes ask only with tool safety on; mail asks either way."""
    policy = ToolSafetyPolicy(enabled=enabled)
    assert ("microsoft_write" in protected_tools(policy)) is enabled
    assert "microsoft_send" in protected_tools(policy)
    posture = unattended_posture(
        policy,
        session=_with_daimon("https://daimon.example/mcp"),
        public_url="https://daimon.example/mcp",
        session_factory=MagicMock(),
        tenant_id=uuid.uuid4(),
    )
    call = ToolCall(
        tool_use_id="tu",
        server_name="daimon-mcp",
        tool_name="microsoft_write",
        input={"method": "PATCH"},
    )
    assert (await posture.decide(call)).allow is allowed


@pytest.mark.parametrize("entry_point", ["routine", "cli_run", "long_job"])
async def test_nonhuman_session_gate_refuses_role_gated_agent(
    entry_point: str,
) -> None:
    from typing import cast
    from unittest.mock import AsyncMock, patch

    from daimon.core.access_policy import AgentRule, TenantAccessPolicy
    from daimon.core.turn.errors import AdmissionDenied
    from daimon.core.turn.unattended_capabilities import UnattendedEntryPoint

    snapshot = _session()
    target = ma_agent(name="restricted")
    lookup = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=lookup)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    access = TenantAccessPolicy(agent_rules={"restricted": AgentRule(invoker_role_ids=("123",))})
    with (
        patch(
            "daimon.core.stores.access_policy.load_access_policy", AsyncMock(return_value=access)
        ),
        patch("daimon.core.mux_compat.retrieve_agent", AsyncMock(return_value=target)),
        pytest.raises(AdmissionDenied, match="invoker_not_allowed"),
    ):
        await gate_session_snapshot(
            AsyncMock(),
            snapshot,
            ToolSafetyPolicy(enabled=False),
            scope=platform_scope("test unattended role gate"),
            public_url=None,
            session_factory=factory,
            tenant_id=uuid.uuid4(),
            entry_point=cast(UnattendedEntryPoint, entry_point),
        )
