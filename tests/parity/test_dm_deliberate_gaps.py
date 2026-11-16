"""Executable record of where direct messages deliberately differ between platforms.

Discord, Slack and Teams hold a direct message until the sender goes quiet and answer
a burst as one turn (`daimon.core.dm_debounce.DmBursts`); the web chat has no such
wait. Only a Discord direct message goes into a continuing session as the new
words alone; Slack opens a new session for every turn, so each one carries the
earlier turns.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.web.stream import StreamRegistry
from daimon.core import direct_messages
from daimon.core.authz import AgentRef, Place, Surface, build_subject
from daimon.core.direct_messages import reply_to_dm_result, start_cold_dm
from daimon.core.features.registry import FEATURES
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.scope import ResolvedConfig
from daimon.core.stores.direct_messages import (
    set_dm_agent_name,
    set_dm_enabled,
    start_conversation,
)
from daimon.core.stores.domain import Role
from daimon.core.turn.admission import Admission, AdmissionGrant
from daimon.core.turn.run import RunOutcome
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.turn_fakes import deps_for


def test_the_web_chat_does_not_hold_and_merge_messages() -> None:
    """A web turn rejects a message sent while it runs; nothing waits for more to arrive.

    The page shows one running turn and sends one message at a time, so there is no burst
    to merge. If the web chat gains a hold, route it through `DmBursts` and replace this.
    """
    assert FEATURES["dm_debounce_seconds"].platforms == frozenset({"discord", "slack", "teams"}), (
        "the wait is offered only where direct messages are held"
    )
    streams = StreamRegistry()
    thread, message = uuid.uuid4(), uuid.uuid4()
    streams.open(thread, message)
    with pytest.raises(RuntimeError, match="already running"):
        streams.open(thread, uuid.uuid4())


@pytest.mark.parametrize(
    ("platform", "reuses_session", "replayed"),
    [("discord", True, False), ("slack", False, True)],
)
async def test_only_a_discord_direct_message_skips_the_replay_into_a_continuing_session(
    db_session_factory, monkeypatch, platform, reuses_session, replayed
):
    """Slack opens a new session per turn (so no session holds the history); Discord reuses one."""
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, workspace_id="700000101")
        account = await make_account(session, tenant=tenant)
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
        await set_dm_agent_name(session, tenant_id=tenant.id, agent_name="private")
    admission = Admission(
        account_id=account.id,
        agent=ma_agent(id="ag_private", name="private", tenant_id=tenant.id),
        environment=ma_environment(id="env_1", name="default", tenant_id=tenant.id),
        config=ResolvedConfig(agent_name="private", environment_name="default"),
    )
    deps = replace(
        deps_for(None, db_session_factory),
        membership_resolver=CachedMembershipResolver(
            AsyncMock(return_value=Membership(is_member=True))
        ),
    )
    row = await start_cold_dm(
        deps,
        admission,
        tenant_id=tenant.id,
        workspace_id=tenant.external_id,
        route_key="555",
        external_user_id="999",
    )
    if platform != row.platform:
        async with db_session_factory.begin() as session:
            await start_conversation(
                session,
                conversation=row.model_copy(update={"platform": platform}),
                now=datetime.now(UTC),
            )
    admission = replace(
        admission,
        grant=AdmissionGrant(
            tenant_id=tenant.id,
            subject=build_subject(is_admin=False, platform_user_id="999", verified_membership=True),
            surface=Surface.DM,
            turn_place=Place(),
            agent=AgentRef.of("private"),
            run_place=Place(),
            channel_id=row.channel_id,
            thread_id=row.scope_id,
            is_dm=True,
            cold_dm=True,
            human_invocation=True,
        ),
    )
    # The session the bind returns has answered a turn here, as a continuing one has.
    prepared = SimpleNamespace(
        admission=admission,
        reused=reuses_session,
        watermark="100" if reuses_session else None,
        continuity=SimpleNamespace(state="continued"),
    )
    bind = AsyncMock(return_value=prepared)
    monkeypatch.setattr(direct_messages, "admit", AsyncMock(return_value=admission))
    monkeypatch.setattr(direct_messages, "bind_session", bind)
    sent: list[str] = []

    async def run(deps, prepared, **kwargs):
        sent.append(str(kwargs["user_message"]))
        return RunOutcome(
            TurnState(content=[TextBlock(kind="text", text="a cat")]), "sesn_dm", None, False
        )

    monkeypatch.setattr(direct_messages, "run_prepared_turn", run)
    for number, question in enumerate(("an earlier question", "the new question")):
        await reply_to_dm_result(
            deps,
            platform=platform,
            route_key="555",
            external_user_id="999",
            message_id=str(100 + number),
            expected_scope_id=row.scope_id,
            text=question,
            role=Role.USER,
            verified_membership=True,
        )
    assert bind.await_args.kwargs["reuse_existing"] is reuses_session, (
        "Slack never attaches a later turn to a prior turn's session"
    )
    assert ("an earlier question" in sent[-1]) is replayed, (
        "Slack carries the history every turn; Discord sends it only into a new session"
    )
