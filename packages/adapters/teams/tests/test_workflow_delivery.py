"""Teams posts a workflow's Finish result and owner notice through the shared poller."""

from __future__ import annotations

import time
from typing import Any, cast
from unittest.mock import MagicMock

from daimon.adapters.teams.direct_chats import SdkDirectChats
from daimon.adapters.teams.lifecycle import TeamsSender
from daimon.adapters.teams.workflow_delivery import make_teams_workflow_poster
from daimon.core.delivery import DeliveryOutcome, JobPoster, poll_once
from daimon.core.stores import accounts, tenants
from daimon.core.stores.domain import RoutineRow
from daimon.core.teams_bot_framework import SERVICE_URL
from daimon.testing.factories import make_platform_principal
from daimon.testing.platform_surface import PlatformMessageCapture, SurfaceRecorder
from daimon.testing.teams_surface import OfflineTeamsSender
from daimon.testing.teams_surface import _decode as decode  # pyright: ignore[reportPrivateUsage]
from daimon.testing.workflows import NOW, make_world
from sqlalchemy.ext.asyncio import AsyncSession

HOME = "19:home@thread.tacv2"


class RosterChats(SdkDirectChats):
    """The real posting code over the offline sender; the speaker is on the roster."""

    async def member(self, conversation_id: str, aad_object_id: str) -> str | None:
        return "29:speaker" if aad_object_id == "aad-1" else None


async def _delivered(db_session: AsyncSession, nodes: dict[str, Any]) -> PlatformMessageCapture:
    world = await make_world(db_session, nodes, next(iter(nodes)), platform="teams", home=HOME)
    tenant = await tenants.get_tenant(db_session, world.tenant_id)
    owner = await accounts.get_account(db_session, world.owner)
    assert tenant is not None and owner is not None
    await make_platform_principal(
        db_session, platform="teams", external_id="aad-1", tenant=tenant, account=owner
    )
    await db_session.commit()
    await world.run()
    recorder = SurfaceRecorder(
        evidence_id="wf", destination_id=HOME, thread_id=None, clock=time.monotonic, decode=decode
    )
    sender = OfflineTeamsSender(recorder, service_url=SERVICE_URL)
    chats = RosterChats(MagicMock(), cast(TeamsSender, sender), entra_tenant_id="entra")

    async def no_routine(row: RoutineRow) -> DeliveryOutcome:
        raise AssertionError("no routine is due")

    async def no_links(*_: object) -> None:
        return None

    poster = make_teams_workflow_poster(
        world.sm, chats, tenant_id=world.tenant_id, settings=world.settings, links=no_links
    )
    assert await poll_once(world.sm, platform="teams", post=JobPoster(no_routine, poster), now=NOW)
    [message] = recorder.surviving_messages()
    return message


async def test_a_finish_posts_to_home_naming_the_workflow(db_session: AsyncSession) -> None:
    nodes = {"f": {"kind": "finish", "deliver": {"to": "home", "message": "All done"}}}
    message = await _delivered(db_session, nodes)
    assert message.body_texts == ('All done\nfrom the workflow "T"',), message


async def test_a_failed_run_sends_the_owner_an_error_card(db_session: AsyncSession) -> None:
    message = await _delivered(db_session, {"f": {"kind": "finish", "outcome": "failed"}})
    text = " ".join(message.card_texts)
    assert "Workflow run failed" in text and 'Run 1 of "T" failed.' in text, message
