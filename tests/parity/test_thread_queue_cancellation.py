"""Live adapter regressions for Sol's PR403 cancel_popped_batch probe.

Only transport, external gate responses and turn bodies are controlled seams.
Admission, composition, drain, error boundaries and owner cleanup stay real.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import discord as sdk
import pytest
from daimon.adapters.discord import bot as discord
from daimon.adapters.slack import app as slack
from daimon.adapters.teams import app as teams
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.ma_identity import derive_tenant_uuid
from slack_sdk.errors import SlackApiError

TENANT = derive_tenant_uuid(platform="discord", workspace_id="123")
CLASSES = {"discord": discord.DaimonBot, "slack": slack.SlackApp, "teams": teams.TeamsApp}


class CancellationProbe:
    def __init__(self, platform, monkeypatch, notice_error):
        self.platform = platform
        self.key = 9101 if platform == "discord" else "thread-a"
        self.started, self.release, self.draining = (asyncio.Event() for _ in range(3))
        self.turns, self.notices = [], []
        self.owner_kind = "mention"
        self.notice_error = notice_error
        self.obj = obj = CLASSES[platform].__new__(CLASSES[platform])
        obj.runtime = SimpleNamespace(
            sessionmaker=None,
            settings=SimpleNamespace(
                bot_display_name="Daimon",
                discord=SimpleNamespace(
                    bot_display_name="Daimon",
                    max_concurrent_turns_per_tenant=3,
                    max_concurrent_turns=None,
                    qa_bot_user_ids=(),
                ),
                slack=SimpleNamespace(max_concurrent_turns_per_tenant=3),
            ),
        )
        obj.draining = False
        obj._processing, obj._pending, obj._deferred_dispatch = set(), {}, {}
        obj._inflight, obj._last_message_at = {}, {}
        obj._recovery, obj._participation = None, None
        obj._global_inflight = 0
        obj._orphan_recovery_task = None
        obj._participation_pending = {}
        obj._connection = SimpleNamespace(user=SimpleNamespace(id=999))
        obj._wait_for_orphan_recovery = AsyncMock()
        obj._maybe_post_connect_nudge = AsyncMock()
        obj._turn_cap = AsyncMock(return_value=3)
        obj._dispatch_continuations = self.dispatch
        obj._say = self.say
        if platform == "discord":
            obj._handle_mention = self.turn
        elif platform == "slack":
            obj._run_thread_turn = self.turn
            obj._notify_undrained_mentions = AsyncMock(wraps=obj._notify_undrained_mentions)
        else:
            obj._run_turn = self.turn
        self.client = SimpleNamespace(
            reactions_add=AsyncMock(), chat_postMessage=AsyncMock(), chat_postEphemeral=AsyncMock()
        )
        if notice_error:
            self.client.chat_postMessage.side_effect = SlackApiError(
                "notice unavailable", {"ok": False, "error": "channel_not_found"}
            )
        monkeypatch.setattr(
            discord,
            "get_tenant_liveness",
            AsyncMock(
                return_value=SimpleNamespace(
                    id=TENANT, archived_at=None, provision_status="ready", turn_cap=3
                )
            ),
        )
        monkeypatch.setattr(
            discord,
            "_channel_protection_state",
            AsyncMock(return_value=SimpleNamespace(may_post=True)),
        )
        monkeypatch.setattr(slack, "get_turn_cap", AsyncMock(return_value=3))
        monkeypatch.setattr(slack, "turn_target_protected", AsyncMock(return_value=False))

        async def route(sessionmaker, inbound, tenant):
            return inbound

        monkeypatch.setattr(teams, "route_to_setup", route)

    def message(self, number, author="A"):
        if self.platform == "discord":
            channel = MagicMock(spec=sdk.Thread)
            channel.id = self.key

            async def send(text):
                self.notices.append((number, text))
                if self.notice_error:
                    raise sdk.HTTPException(
                        SimpleNamespace(status=503, reason="Service Unavailable"),
                        "notice unavailable",
                    )

            channel.send = send
            return SimpleNamespace(
                id=number,
                content=str(number),
                author=SimpleNamespace(id=author, bot=False, display_name=author),
                guild=SimpleNamespace(id=123),
                channel=channel,
                mentions=[SimpleNamespace(id=999)],
                attachments=[number],
                add_reaction=AsyncMock(),
            )
        if self.platform == "slack":
            return dict(
                ts=str(number),
                event_ts=str(number),
                thread_ts=self.key,
                text=str(number),
                user=author,
                channel="C",
                files=[dict(id=f"F{number}")],
            )
        return TeamsInbound(
            kind="dm",
            entra_tenant_id=str(TENANT),
            user_id=author,
            conversation_id=self.key,
            channel_id=self.key,
            activity_id=str(number),
            text=str(number),
            service_url="https://region.example",
        )

    async def turn(self, message, *args, **kwargs):
        if self.platform == "discord":
            number, text = message.id, message.content
        elif self.platform == "slack":
            number, text = int(message["ts"]), message["text"]
        else:
            number, text = int(message.activity_id), message.text
        self.turns.append((number, kwargs.get("content_override") or text))
        if number == 0:
            self.started.set()
            await self.release.wait()
        else:
            self.draining.set()
            await asyncio.Event().wait()

    async def dispatch(self, *args, **kwargs):
        if self.owner_kind == "dispatch":
            self.started.set()
            await self.release.wait()

    async def say(self, message, text):
        self.notices.append((int(message.activity_id), text))
        if self.notice_error:
            raise OSError("notice unavailable")

    async def arrive(self, message):
        if self.platform == "discord":
            await self.obj.on_message(message)
        elif self.platform == "slack":
            await self.obj._orchestrate(
                message,
                team_id="W",
                channel="C",
                event_ts=message["ts"],
                web_client=self.client,
                tenant_id=TENANT,
            )
        else:
            await self.obj._orchestrate(message, TENANT)

    async def owner(self, kind):
        self.owner_kind = kind
        if kind == "mention":
            await self.arrive(self.message(0))
        elif self.platform == "discord":
            await self.obj.dispatch_continuations_in_thread(
                tenant_id=TENANT, thread=SimpleNamespace(id=self.key), guild_id="123"
            )
        elif self.platform == "slack":
            await self.obj.dispatch_continuations_in_thread(
                tenant_id=TENANT,
                web_client=self.client,
                channel="C",
                thread_id=self.key,
                account_id=UUID(int=1),
                team_id="W",
            )
        else:
            await self.obj.dispatch_after_input(TENANT, self.key, "https://region.example")


@pytest.mark.parametrize("platform", CLASSES)
@pytest.mark.parametrize("owner_kind", ["mention", "dispatch"])
@pytest.mark.parametrize("coalesced", [False, True])
@pytest.mark.parametrize("notice_error", [False, True])
async def test_cancelled_owner_notifies_already_popped_authors(
    platform, owner_kind, coalesced, notice_error, monkeypatch
):
    probe = CancellationProbe(platform, monkeypatch, notice_error)
    owner = asyncio.create_task(probe.owner(owner_kind))
    try:
        await asyncio.wait_for(probe.started.wait(), 3)
        # False reproduces Sol's exact scenario: initial turn, queue A/B,
        # suspend drained A, queue C, cancel owner. True adds A/B coalescing
        # and a second unstarted author to check notice order and uniqueness.
        batch = [(1, "A"), (2, "B")]
        if coalesced:
            batch += [(3, "A"), (4, "D"), (5, "B")]
        for number, author in batch:
            await probe.arrive(probe.message(number, author))
        probe.release.set()
        await asyncio.wait_for(probe.draining.wait(), 3)
        assert probe.key not in probe.obj._pending, "the entire batch has been popped"
        late = 6 if coalesced else 3
        await probe.arrive(probe.message(late, "C"))
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
    finally:
        if not owner.done():
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)

    first = 3 if coalesced and platform == "teams" else 1
    expected_turns = [(first, "1\n\n3" if coalesced else "1")]
    if owner_kind == "mention":
        expected_turns.insert(0, (0, "0"))
    assert probe.turns == expected_turns, "cancelled or unstarted turns must never be retried"
    expected_notices = [5 if coalesced and platform == "teams" else 2]
    if coalesced:
        expected_notices.append(4)
    if platform == "slack":
        notified = [
            int(event["ts"])
            for call in probe.obj._notify_undrained_mentions.await_args_list
            for event in call.args[0]
        ]
        assert notified == [*expected_notices, late]
        assert probe.client.chat_postMessage.await_count == len(notified)
        assert all(
            call.kwargs["text"] == "Sorry, something went wrong handling that — please try again."
            for call in probe.client.chat_postMessage.await_args_list
        )
    else:
        if platform == "teams":
            expected_notices.append(late)
        assert [number for number, _ in probe.notices] == expected_notices
        expected_copy = (
            "Sorry, something went wrong handling that. Please try again."
            if platform == "teams"
            else "Sorry, something went wrong handling that — please try again."
        )
        assert all(text == expected_copy for _, text in probe.notices)
    assert probe.obj._processing == set()
    assert probe.obj._inflight == {}
    assert probe.obj._global_inflight == 0
    if platform == "discord" and owner_kind == "dispatch":
        assert list(probe.obj._pending) == [probe.key]
        assert len(probe.obj._pending[probe.key]) == 1
        assert probe.obj._pending[probe.key][0].id == late
    else:
        assert probe.obj._pending == {}
