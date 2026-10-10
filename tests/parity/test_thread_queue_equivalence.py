"""Replay interleavings through real frozen base methods and the current adapters."""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
from pathlib import Path
from types import MethodType, SimpleNamespace
from uuid import UUID

import pytest
from daimon.adapters.discord import bot as discord
from daimon.adapters.slack import app as slack
from daimon.adapters.teams import app as teams
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.errors import DaimonError
from daimon.core.turn.thread_queue import ThreadQueue
from daimon.core.turn_queue import TurnQueue

TENANT = UUID(int=1)
MODULES = {"discord": discord, "slack": slack, "teams": teams}
CLASSES = {"discord": discord.DaimonBot, "slack": slack.SlackApp, "teams": teams.TeamsApp}


def base(platform):
    namespace = dict(vars(MODULES[platform]))
    # The frozen base predates the turn queue and calls the old pure gate.
    namespace["should_admit_turn"] = lambda *, current_in_flight, cap: current_in_flight < cap
    path = Path(__file__).with_name("thread_queue_base") / f"{platform}.txt"
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    return namespace["Base"], namespace


class Replay:
    def __init__(self, platform, old, fault="ok", cap=3, draining=False):
        self.platform, self.fault, self.cap = platform, fault, cap
        self.old = old
        self.effects, self.resumes, self.cleared = [], [], []
        self.rows = ["handoff"]
        cls, namespace = base(platform) if old else (CLASSES[platform], vars(MODULES[platform]))
        self.namespace = namespace
        self.obj = SimpleNamespace(
            _processing=set(),
            _pending={},
            _queued_reactions=set(),
            user=SimpleNamespace(id=999),
            _deferred_dispatch={},
            _inflight={},
            turn_queue=TurnQueue(
                platform=platform,
                global_cap=None,
                max_queued_per_tenant=50,
                max_queued=500,
                max_wait_s=300,
            ),
            _last_message_at={},
            _recovery=None,
            _participation=None,
            draining=draining,
            runtime=SimpleNamespace(sessionmaker=None),
            _processing_tasks={},
            _track_processing_task=lambda thread_id: None,
        )
        self.obj._thread_queue = ThreadQueue(self.obj._processing, self.obj._pending)
        names = {
            "discord": [
                "_queue_behind_inflight_turn",
                "_drain_pending_mentions",
                "_release_thread",
                "dispatch_continuations_in_thread",
            ],
            "slack": [
                "_drain_pending_mentions",
                "_release_thread",
                "dispatch_continuations_in_thread",
            ],
            "teams": ["_orchestrate", "_holding", "_run_turns", "_release", "dispatch_after_input"],
        }[platform]
        if platform == "discord" and not old:
            names += ["_remove_mention_reaction", "_clear_queued_reactions"]
        for name in names:
            setattr(self.obj, name, MethodType(getattr(cls, name), self.obj))
        self.obj._dispatch_continuations = self.dispatch
        self.obj._wait_for_orphan_recovery = self.barrier
        self.obj._handle_mention = self.discord_turn
        self.obj._run_thread_turn = self.slack_turn
        self.obj._run_turn_guarded = self.teams_turn
        self.obj._turn_cap = self.turn_cap
        self.obj._supersede_batch = lambda item: self.effects.append(
            ("supersede", item.activity_id)
        )
        self.obj._say = self.say
        self.obj._spawn = self.spawn
        self.obj.spawn = self.spawn
        # The base never removes ⌛, so removals stay out of the compared effects.
        self.client = SimpleNamespace(chat_postMessage=self.post, reactions_remove=self.unreact)
        self.obj._notify_undrained_mentions = MethodType(
            slack.SlackApp._notify_undrained_mentions, self.obj
        )
        self.obj._clear_pending_reactions = MethodType(
            slack.SlackApp._clear_pending_reactions, self.obj
        )
        self.key = 7 if platform == "discord" else "chat"
        self.thread = SimpleNamespace(id=self.key)
        self.paused, self.proceed = asyncio.Event(), asyncio.Event()
        self.pause_at = None
        self.turn_count = 0

    @property
    def state_key(self):
        # The frozen Slack base owns a bare timestamp; the current adapter
        # scopes ownership to its workspace conversation.
        if self.platform == "slack" and not self.old:
            return ("team", "channel", self.key)
        return self.key

    def base_key(self, key):
        if self.platform == "slack" and not self.old:
            return key[2]
        return key

    def spawn(self, coroutine, **kwargs):
        # Record exactly what release schedules, without running a second owner yet.
        frame = coroutine.cr_frame
        self.resumes.append({k: v for k, v in frame.f_locals.items() if k != "self"})
        coroutine.close()

    async def barrier(self):
        self.effects.append(("recovery",))

    async def turn_cap(self, tenant):
        self.effects.append(("cap", self.cap))
        return self.cap

    async def post(self, **kwargs):
        self.effects.append(("response", kwargs))

    async def unreact(self, **kwargs):
        self.cleared.append(kwargs["timestamp"])

    async def say(self, item, text):
        self.effects.append(("response", item.activity_id, text))

    async def stop(self, where):
        if self.pause_at == where:
            self.pause_at = None
            self.paused.set()
            await self.proceed.wait()

    async def dispatch(self, *args, **kwargs):
        self.effects.append(("dispatch", bool(self.rows)))
        await self.stop("dispatch")
        if self.fault == "dispatch":
            raise RuntimeError("dispatch failed")
        if self.rows:
            self.effects.append(("write", "settle", tuple(self.rows)))
            self.rows.clear()

    async def record_turn(self, author, carrier, content, files, ids=()):
        self.turn_count += 1
        self.effects.append(("admit", author, carrier, content, files, ids))
        await self.stop("turn")
        if self.turn_count == 1 and self.fault in {"expected", "unexpected"}:
            if self.fault == "expected":
                raise DaimonError("turn failed")
            raise RuntimeError("unexpected failure")
        self.effects.append(("write", "turn", carrier))

    async def discord_turn(self, item, *args, **kwargs):
        try:
            await self.record_turn(
                item.author.id, item.id, kwargs["content_override"], kwargs["attachments_override"]
            )
        except DaimonError:
            # Discord's existing turn callback renders expected errors internally.
            self.effects.append(("response", item.id, "turn failed"))

    async def slack_turn(self, item, **kwargs):
        await self.record_turn(
            item.get("user"), item["ts"], kwargs["content_override"], kwargs["files"]
        )

    async def teams_turn(self, item, tenant):
        try:
            await self.record_turn(
                item.user_id, item.activity_id, item.text, list(item.files), item.message_ids
            )
        except DaimonError:
            self.effects.append(("response", item.activity_id, "turn failed"))

    def message(self, author, number):
        text = f"message {number}"
        if self.platform == "discord":

            async def react(emoji):
                self.effects.append(("wait", number, emoji))
                await self.stop("reaction")

            async def unreact(emoji, user):
                self.cleared.append(number)

            return SimpleNamespace(
                author=SimpleNamespace(id=author, display_name=str(author)),
                id=number,
                content=text,
                attachments=[number],
                add_reaction=react,
                remove_reaction=unreact,
                guild=SimpleNamespace(me=self.obj.user),
            )
        if self.platform == "slack":
            return dict(
                user=str(author) if author is not None else "",
                ts=str(number),
                text=text,
                channel="channel",
                files=[dict(id=f"F{number}")],
            )
        return TeamsInbound(
            kind="dm",
            entra_tenant_id=str(TENANT),
            user_id=str(author),
            conversation_id="chat",
            channel_id="chat",
            activity_id=str(number),
            text=text,
            service_url="regional",
            files=(number,),
        )

    async def arrive(self, author, number):
        item = self.message(author, number)
        if self.platform == "discord":
            await self.obj._queue_behind_inflight_turn(self.key, item)
        elif self.platform == "slack":
            # Slack's busy front door is unchanged and appends before its wait response.
            self.obj._pending.setdefault(self.state_key, []).append(item)
            self.effects.append(("wait", number, "⌛"))
        else:
            await self.obj._orchestrate(item, TENANT)

    async def external(self, url="regional", capped=False, thread=None):
        if self.platform == "discord":
            await self.obj.dispatch_continuations_in_thread(
                tenant_id=TENANT, thread=self.thread, guild_id="guild"
            )
        elif self.platform == "slack":
            await self.obj.dispatch_continuations_in_thread(
                web_client=self.client,
                tenant_id=TENANT,
                channel="channel",
                thread_id=self.key,
                account_id=TENANT,
                team_id="team",
            )
        else:
            await self.obj.dispatch_after_input(TENANT, thread or self.key, url, capped=capped)

    def pending_message(self, item):
        if self.platform == "discord":
            return item.author.id, item.id, item.content, item.attachments
        if self.platform == "teams":
            return dataclasses.asdict(item)
        return item

    def hold_slot(self):
        """Another turn holds one of TENANT's slots: the base's counter, the queue's ticket."""
        if self.old:
            self.obj._inflight[TENANT] = 1
        else:
            self.obj.turn_queue.claim(TENANT)

    def in_flight(self):
        """Slots held per tenant, in the base's dict shape for either version."""
        if self.old:
            return self.obj._inflight
        queue = self.obj.turn_queue
        return {TENANT: queue.in_flight(TENANT)} if queue.in_flight(TENANT) else {}

    def result(self, error=None):
        # Client identity differs across the two executions; retain all other resume facts.
        resumes = [
            {
                k: ("client" if k == "web_client" else v.id if k == "thread" else v)
                for k, v in request.items()
            }
            for request in self.resumes
        ]
        return (
            self.effects,
            resumes,
            {self.base_key(key) for key in self.obj._processing},
            {
                self.base_key(key): [self.pending_message(m) for m in batch]
                for key, batch in self.obj._pending.items()
            },
            {self.base_key(key): request for key, request in self.obj._deferred_dispatch.items()},
            self.in_flight(),
            self.rows,
            error,
        )


@pytest.fixture(autouse=True)
def route_without_io(monkeypatch):
    async def route(_factory, item, _tenant):
        return item

    monkeypatch.setattr(teams, "route_to_setup", route)


async def replay(platform, old, authors, pause_at, fault, cancel, draining):
    run = Replay(platform, old, fault=fault, draining=draining)
    if platform == "teams" and draining:
        await run.external()
        return run.result()
    # Both owners see a pre-existing batch, then more arrivals at a controlled await.
    run.obj._pending[run.state_key] = [run.message(a, n) for n, a in enumerate(authors, 1)]
    run.pause_at = pause_at
    task = asyncio.create_task(run.external())
    await asyncio.wait_for(run.paused.wait(), 3)
    await run.arrive(2, 8)
    await run.arrive(1, 9)
    run.rows.append("private input")
    await run.external(url=None, capped=True)
    await run.external(url="new region", capped=False)
    if cancel:
        task.cancel()
    else:
        run.proceed.set()
    error = None
    try:
        await task
    except (RuntimeError, asyncio.CancelledError) as exc:
        error = type(exc).__name__
    return run.result(error)


@pytest.mark.parametrize("platform", MODULES)
@pytest.mark.parametrize("authors", sorted(set(itertools.permutations((1, 1, 2)))))
@pytest.mark.parametrize(
    "pause_at,fault,cancel",
    [
        ("dispatch", "ok", False),
        ("turn", "ok", False),
        ("dispatch", "dispatch", False),
        ("turn", "expected", False),
        ("turn", "unexpected", False),
        ("dispatch", "ok", True),
        ("turn", "ok", True),
    ],
)
@pytest.mark.parametrize("draining", [False, True])
async def test_scheduled_arrivals_errors_and_cancels_match_base(
    platform, authors, pause_at, fault, cancel, draining
):
    before = await replay(platform, True, authors, pause_at, fault, cancel, draining)
    after = await replay(platform, False, authors, pause_at, fault, cancel, draining)
    assert after == before


@pytest.mark.parametrize("platform", MODULES)
@pytest.mark.parametrize("capped,cap", [(False, 0), (True, 0), (True, 1)])
async def test_wake_caps_and_busy_before_cap_match_base(platform, capped, cap):
    results = []
    for old in (True, False):
        run = Replay(platform, old, cap=cap)
        run.hold_slot()
        await run.external(capped=capped)
        run.obj._processing.add(run.state_key)
        await run.external(url="saved region", capped=False)
        await run.external(url=None, capped=True)
        if platform == "teams":
            run.obj._release(run.key)
        else:
            run.obj._release_thread(run.state_key)
        results.append(run.result())
    assert results[0] == results[1]


async def test_discord_enqueue_precedes_suspended_wait_response():
    results = []
    for old in (True, False):
        run = Replay("discord", old)
        run.obj._processing.add(run.key)
        run.pause_at = "reaction"
        arrived = asyncio.create_task(run.arrive(1, 1))
        await asyncio.wait_for(run.paused.wait(), 3)
        await run.obj._drain_pending_mentions(run.key, "guild", TENANT)
        run.obj._release_thread(run.key)
        run.proceed.set()
        await arrived
        results.append(run.result())
    assert results[0] == results[1]
    assert any(effect[0] == "admit" for effect in results[0][0])


async def test_slack_authorless_events_and_per_author_errors_match_base():
    results = []
    for old in (True, False):
        run = Replay("slack", old, fault="expected")
        run.obj._pending[run.state_key] = [
            run.message(a, n) for n, a in enumerate((None, 1, 2, 1), 1)
        ]
        await run.external()
        results.append(run.result())
    assert results[0] == results[1]
    assert [effect[1] for effect in results[0][0] if effect[0] == "admit"] == ["1", "2"]
    assert sorted(run.cleared) == ["1", "2", "3", "4"]


async def test_teams_setup_threads_resume_in_order_with_merged_url_and_cap():
    results = []
    for old in (True, False):
        run = Replay("teams", old)
        run.obj._processing.add(run.key)
        # Multiple setup threads share a conversation guard and each retains its resume.
        for thread in ("chat;setup=one", "chat;setup=two"):
            await run.external(url="region", capped=False, thread=thread)
            await run.external(url=None, capped=True, thread=thread)
        run.obj._release(run.key)
        results.append(run.result())
    assert results[0] == results[1]


@pytest.mark.parametrize("authors", sorted(set(itertools.permutations((1, 1, 2)))))
def test_teams_last_carrier_content_files_and_message_ids_match_base(authors):
    run = Replay("teams", True)
    queued = [run.message(a, n) for n, a in enumerate(authors, 1)]
    before = run.namespace["_compose_queued"](queued)
    assert [dataclasses.asdict(i) for i in teams._compose_queued(queued)] == [
        dataclasses.asdict(i) for i in before
    ]


@pytest.mark.parametrize("platform", MODULES)
async def test_another_thread_runs_while_the_first_dispatch_waits(platform):
    results = []
    for old in (True, False):
        run = Replay(platform, old)
        run.pause_at = "dispatch"
        first = asyncio.create_task(run.external())
        await asyncio.wait_for(run.paused.wait(), 3)
        run.key = 12 if platform == "discord" else "other chat"
        run.thread = SimpleNamespace(id=run.key)
        item = run.message(2, 5)
        if platform == "teams":
            item = dataclasses.replace(item, conversation_id=run.key)
        run.obj._pending[run.state_key] = [item]
        await run.external()
        assert any(effect[0] == "admit" for effect in run.effects)
        run.proceed.set()
        await first
        results.append(run.result())
    assert results[0] == results[1]
