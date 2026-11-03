"""Business grants remain narrower than token claims or server admin status."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.lead_intake import IntakeConfig, authorize_intake_actor
from daimon.core.membership import Membership


@pytest.mark.parametrize(
    "roles,admin,bypass,resolvers,routine,active,resolve,allowed",
    [
        (("42",), False, False, (), False, False, True, True),
        ((), False, False, ("7",), False, False, True, True),
        ((), True, False, (), False, False, True, False),
        ((), True, True, (), False, False, True, False),
        (("42",), False, False, (), True, True, True, False),
        (("42",), False, False, (), True, True, False, True),
        (("42",), False, False, (), True, False, False, False),
        ((), True, False, (), False, False, False, False),
    ],
)
def test_h1b_and_resolution_grants(
    roles: tuple[str, ...],
    admin: bool,
    bypass: bool,
    resolvers: tuple[str, ...],
    routine: bool,
    active: bool,
    resolve: bool,
    allowed: bool,
) -> None:
    agent, routine_id = uuid4(), uuid4()
    config = IntakeConfig(
        agent_id=agent,
        guild_id="1",
        channel_id="2",
        source_connection_id="gmail",
        crm_connection_id="crm",
        portal_id="3",
        resolver_user_ids=resolvers,
        routine_id=routine_id,
        reconciliation_start=datetime.now(UTC),
    )
    # Sales resolvers still require ordinary tenant admission; explicitly allow
    # this user when testing that separate configured business grant.
    policy = TenantAccessPolicy(
        invoker_role_ids=("42",),
        invoker_admin_bypass=bypass,
        invoker_user_ids=("7",) if resolvers else (),
    )

    def check() -> None:
        authorize_intake_actor(
            config,
            policy,
            user_id="7",
            facts=Membership(is_member=True, role_ids=roles, is_admin=admin),
            agent_id=agent,
            routine_id=routine_id if routine else None,
            execution_active=active,
            resolve=resolve,
        )

    if allowed:
        check()
    else:
        with pytest.raises(PermissionError):
            check()


@pytest.mark.parametrize(
    "member,agent,routine",
    [(False, True, True), (None, True, True), (True, False, True), (True, True, False)],
)
def test_missing_or_wrong_server_evidence_fails_closed(
    member: bool | None, agent: bool, routine: bool
) -> None:
    identity = uuid4()
    config = IntakeConfig(
        agent_id=identity,
        guild_id="1",
        channel_id="2",
        source_connection_id="gmail",
        crm_connection_id="crm",
        portal_id="3",
        routine_id=uuid4(),
        reconciliation_start=datetime.now(UTC),
    )
    with pytest.raises(PermissionError):
        authorize_intake_actor(
            config,
            TenantAccessPolicy(invoker_role_ids=("42",)),
            user_id="7",
            facts=Membership(is_member=member, role_ids=("42",)),
            agent_id=identity if agent else UUID(int=0),
            routine_id=config.routine_id if routine else uuid4(),
            execution_active=True,
        )


@pytest.mark.parametrize("enabled", [False, True])
def test_resolution_requires_exact_human_approval_even_when_safety_disabled(enabled: bool) -> None:
    from daimon.core.confirmation import prompt_for_tool_call
    from daimon.core.tool_safety import (
        DAIMON_SERVER_NAME,
        ToolCall,
        ToolSafetyPolicy,
        decide_tool_call,
    )

    call = ToolCall(
        tool_use_id="resolve-1",
        server_name=DAIMON_SERVER_NAME,
        tool_name="lead_intake_resolve",
        input={
            "intake_id": "synthetic-intake",
            "resolution": {
                "company_id": "200",
                "contact_id": "201",
                "deal_id": "202",
                "evidence": "Verified original identity and project with sales.",
            },
        },
    )
    policy = ToolSafetyPolicy(enabled=enabled)
    assert (
        decide_tool_call(
            policy,
            call,
            attended=True,
            trusted_servers=frozenset({DAIMON_SERVER_NAME}),
            confirmed_tools=frozenset({"lead_intake_resolve"}),
        ).outcome
        == "ask"
    )
    assert (
        decide_tool_call(
            policy,
            call,
            attended=False,
            trusted_servers=frozenset({DAIMON_SERVER_NAME}),
            confirmed_tools=frozenset({"lead_intake_resolve"}),
        ).outcome
        == "deny"
    )
    prompt = prompt_for_tool_call(call, requester_platform_user_id="7", now=datetime.now(UTC))
    assert "Company: 200" in prompt.visible_lines
    assert "Contact: 201" in prompt.visible_lines
    assert "Deal: 202" in prompt.visible_lines
    assert prompt.detail_lines == ()
    assert "Verified original identity" not in prompt.model_dump_json()
