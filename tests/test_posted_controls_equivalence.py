"""Run real base handlers and refactored callers against the same effects."""

from __future__ import annotations

import asyncio
import importlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import discord
import pytest
from daimon.core.confirmation import ApprovedConfirmation, ConfirmationPrompt, PendingConfirmations
from daimon.core.posted_controls import lifecycle
from daimon.core.posted_controls.confirmation import NO_LONGER_PENDING_MESSAGE, NOT_YOURS_MESSAGE
from slack_sdk.errors import SlackApiError

BASE = Path(__file__).with_name("posted_controls_base")
AT = datetime(2026, 10, 3, tzinfo=UTC)
TOKEN = "tok_abcdefgh"
TENANT = UUID(int=1)
ROUTINE = UUID(int=2)
USER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return AT


def _base(module, name):
    scope = dict(
        vars(module),
        datetime=Clock,
        UTC=UTC,
        PendingConfirmations=PendingConfirmations,
        NOT_YOURS_MESSAGE=NOT_YOURS_MESSAGE,
        NO_LONGER_PENDING_MESSAGE=NO_LONGER_PENDING_MESSAGE,
    )
    if name == "slack_confirm":
        posted_card = scope["_PostedCard"]

        def legacy_posted_card(prompt, client, channel, ts):
            return posted_card(prompt, client, channel, ts, thread_ts=None)

        scope["_PostedCard"] = legacy_posted_card
    exec("from __future__ import annotations\n" + (BASE / f"{name}.txt").read_text(), scope)
    return SimpleNamespace(**scope)


def _json(value):
    if isinstance(value, discord.AllowedMentions):
        return value.to_dict()
    if isinstance(value, discord.ui.LayoutView):
        components = value.to_components()
        # Discord generates fresh random ids for live, non-persistent buttons.
        for item in value.walk_children():
            if isinstance(item, discord.ui.Button):
                assert not item._provided_custom_id

        def scrub(item):
            if isinstance(item, dict):
                return {k: "auto" if k == "custom_id" else scrub(v) for k, v in item.items()}
            if isinstance(item, list):
                return [scrub(v) for v in item]
            return item

        return scrub(components)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return value


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
@pytest.mark.parametrize(
    "scenario",
    [
        "approve",
        "deny",
        "stranger",
        "replay",
        "expired",
        "cancel",
        "lost",
        "post_error",
        "timer_after_click",
    ],
)
async def test_confirmation_base_and_new(platform, scenario, monkeypatch):
    module = importlib.import_module(f"daimon.adapters.{platform}.tool_confirmation")
    monkeypatch.setattr(lifecycle, "datetime", Clock)
    monkeypatch.setattr("secrets.token_urlsafe", lambda n: TOKEN)
    old = _base(module, f"{platform}_confirm")

    async def run(implementation):
        trace, posted = [], []
        prompt = ConfirmationPrompt(
            title="Approve a write?",
            detail_lines=("Item: example",),
            requester_platform_user_id=USER,
            expires_at=AT + timedelta(seconds=-1 if scenario == "expired" else 600),
        )

        async def send(**kwargs):
            trace.append(("post", {k: _json(v) for k, v in kwargs.items()}))
            if scenario == "post_error":
                raise SlackApiError("failed", {}) if platform == "slack" else ValueError("failed")
            posted.append(kwargs.get("view"))
            return MagicMock(edit=edit) if platform == "discord" else {"ts": "1700.1"}

        async def edit(**kwargs):
            trace.append(("edit", {k: _json(v) for k, v in kwargs.items()}))

        async def ephemeral(**kwargs):
            trace.append(("refusal", {k: _json(v) for k, v in kwargs.items()}))

        if platform == "discord":
            hook = implementation.discord_confirmation_hook(SimpleNamespace(send=send))
        elif platform == "slack":
            cards = implementation.SlackConfirmationCards()
            client = SimpleNamespace(
                chat_postMessage=send, chat_update=edit, chat_postEphemeral=ephemeral
            )
            hook = cards.hook(client, channel="C1", thread_ts="1699.9")

            async def missing(payload, user, text):
                trace.append(("missing", user, text))

            monkeypatch.setattr(module, "_ephemeral_from_payload", missing)
            if implementation is old:
                cards.handle_click.__func__.__globals__["_ephemeral_from_payload"] = missing
        else:

            async def teams_send(conversation_id, activity, *, service_url):
                trace.append(("send", conversation_id, service_url, _json(activity)))
                if scenario == "post_error":
                    raise ValueError("failed")
                posted.append(activity)
                return SimpleNamespace(id="m-1")

            cards = implementation.TeamsConfirmationCards(SimpleNamespace(send=teams_send))
            hook = cards.hook(conversation_id="conversation", service_url="https://service.invalid")
        gate = asyncio.Event()
        original_wait = asyncio.wait_for

        async def timer_after_click(awaitable, timeout):
            if isinstance(awaitable, asyncio.Future):
                await gate.wait()
                raise TimeoutError
            return await original_wait(awaitable, timeout)

        if scenario == "timer_after_click":
            monkeypatch.setattr(asyncio, "wait_for", timer_after_click)
        waiting = asyncio.create_task(hook(prompt))
        while not trace:
            await asyncio.sleep(0)
        await asyncio.sleep(0)

        async def click(user=USER, answer="approved"):
            if platform == "discord":
                response = SimpleNamespace(
                    send_message=AsyncMock(
                        side_effect=lambda text, **kw: trace.append(
                            ("refusal", text, {k: _json(v) for k, v in kw.items()})
                        )
                    ),
                    edit_message=edit,
                )
                await posted[0]._settle(
                    SimpleNamespace(user=SimpleNamespace(id=user), response=response), answer
                )
            elif platform == "slack":
                await cards.handle_click(
                    {
                        "actions": [
                            {
                                "action_id": f"dcf:{TOKEN}:{'approve' if answer == 'approved' else 'deny'}"
                            }
                        ],
                        "user": {"id": user},
                    }
                )
            else:
                activity = SimpleNamespace(
                    value=SimpleNamespace(
                        action=SimpleNamespace(
                            data={
                                "token": TOKEN,
                                "op": "approve" if answer == "approved" else "deny",
                            }
                        )
                    ),
                    from_=SimpleNamespace(aad_object_id=user.upper(), name="Requester"),
                )
                trace.append(
                    ("response", _json(await cards.on_action(SimpleNamespace(activity=activity))))
                )

        if scenario == "post_error":
            with pytest.raises((ValueError, SlackApiError)):
                await waiting
        elif scenario in ("cancel", "expired"):
            if scenario == "cancel":
                waiting.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiting
            else:
                result = await waiting
                trace.append(
                    (
                        "answer",
                        result.answer if isinstance(result, ApprovedConfirmation) else result,
                    )
                )
            await click()
        else:
            if scenario == "stranger":
                await click(OTHER)
                assert not waiting.done()
            if scenario == "lost":
                if platform == "discord":
                    posted[0]._answer.cancel()
                else:
                    pending = (
                        cards._controls.pending if hasattr(cards, "_controls") else cards._pending
                    )
                    pending.discard(TOKEN)
            await click(answer="denied" if scenario == "deny" else "approved")
            if scenario == "lost":
                if platform != "discord":
                    waiting.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiting
            else:
                gate.set()
                result = await waiting
                trace.append(
                    (
                        "answer",
                        result.answer if isinstance(result, ApprovedConfirmation) else result,
                    )
                )
            if scenario == "replay":
                await click(answer="denied")
        monkeypatch.setattr(asyncio, "wait_for", original_wait)
        if platform != "discord":
            pending = cards._controls.pending if hasattr(cards, "_controls") else cards._pending
            registry = cards._controls.cards if hasattr(cards, "_controls") else cards._cards
            trace.append(("registries", sorted(pending._pending), sorted(registry)))
        return trace

    expected = await run(old)
    actual = await run(module)
    if platform == "slack" and scenario != "post_error":
        # Main #807 current-outcome assertions complement the full trace check below.
        trace = actual
        kinds = [item[0] for item in trace]
        assert kinds.count("post") == 1
        assert trace[-1] == ("registries", [], [])
        if scenario in ("approve", "deny", "stranger", "replay"):
            assert ("answer", "denied" if scenario == "deny" else "approved") in trace
        elif scenario in ("expired", "timer_after_click"):
            assert ("answer", "expired") in trace
        else:
            assert "answer" not in kinds
        if scenario in ("replay", "expired", "cancel"):
            assert kinds.count("missing") == 1, "a late click cannot reopen the card"
        if scenario == "lost":
            assert kinds.count("refusal") == 1, "a lost waiter cannot approve the write"
        if scenario == "timer_after_click":
            edits = [item[1] for item in trace if item[0] == "edit"]
            assert len(edits) == 1 and edits[0]["text"].startswith("Approved")
        if scenario == "stranger":
            refusals = [item[1] for item in trace if item[0] == "refusal"]
            assert len(refusals) == 1 and refusals[0]["thread_ts"] == "1699.9"

    if platform == "slack" and scenario in ("stranger", "lost"):
        # Main 8e4280f0518c3f669a715dab20951a24eaaac43c threads refusal replies.
        # Update only that cited field in the two pre-main expected rows.
        assert expected[1][0] == actual[1][0] == "refusal"
        assert "thread_ts" not in expected[1][1]
        assert actual[1][1]["thread_ts"] == "1699.9"
        expected[1][1]["thread_ts"] = "1699.9"
    assert expected == actual


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
@pytest.mark.parametrize("op", ["pause", "resume"])
@pytest.mark.parametrize(
    "scenario",
    ["creator", "admin", "stranger", "gone", "wrong_tenant", "write_error", "stale", "ownerless"],
)
async def test_routine_base_and_new(platform, op, scenario, monkeypatch):
    suffix = (
        "routines_panel"
        if platform == "teams"
        else "routines_panel." + ("panel" if platform == "discord" else "actions")
    )
    module = importlib.import_module(f"daimon.adapters.{platform}.{suffix}")
    old = _base(module, f"{platform}_routine")
    monkeypatch.setattr(module, "datetime", Clock)

    async def run(implementation):
        trace = []
        row = SimpleNamespace(
            id=ROUTINE,
            updated_at=AT,
            tenant_id=UUID(int=9) if scenario == "wrong_tenant" else TENANT,
            enabled=(op == "pause") != (scenario == "stale"),
            created_by_user_id=(
                None
                if scenario == "ownerless"
                else OTHER
                if scenario in ("stranger", "admin")
                else USER
            ),
        )

        @asynccontextmanager
        async def transaction():
            trace.append("begin")
            try:
                yield session
            except Exception:
                trace.append("rollback")
                raise
            else:
                trace.append("commit")

        session = SimpleNamespace(begin=transaction)
        factory = MagicMock(side_effect=transaction)
        factory.begin = transaction
        runtime = SimpleNamespace(sessionmaker=factory, anthropic=None)

        async def load(session, routine_id, *, tenant_id):
            trace.append(("load", routine_id, tenant_id))
            return None if scenario == "gone" else row

        async def write(session, routine_id, **kwargs):
            trace.append(("write", routine_id, kwargs))
            if scenario == "write_error":
                raise ValueError("failed")

        async def admin(*args, **kwargs):
            trace.append("admin")
            return scenario == "admin"

        def permission(row, *, user_id, is_admin):
            return is_admin or row.created_by_user_id == user_id

        client = SimpleNamespace(
            chat_postEphemeral=AsyncMock(side_effect=lambda **kw: trace.append(("refusal", kw))),
            views_update=AsyncMock(side_effect=lambda **kw: trace.append(("render", kw))),
        )
        scope = {
            "get_routine": load,
            "pause_routine": lambda *a, **k: write(*a, op="pause", **k),
            "resume_routine": lambda *a, **k: write(*a, op="resume", **k),
            "resolve_is_admin": admin,
            "can_manage_routine": permission,
            "derive_tenant_uuid": lambda **kw: TENANT,
            "datetime": Clock,
        }
        if platform == "discord":
            member = MagicMock(spec=discord.Member)
            member.id = USER
            member.guild_permissions.manage_guild = scenario == "admin"
            member.guild_permissions.administrator = False
            interaction = SimpleNamespace(
                user=member,
                guild_id="1",
                guild=SimpleNamespace(owner_id=None),
                response=SimpleNamespace(send_message=client.chat_postEphemeral),
            )
            view = SimpleNamespace(
                state=SimpleNamespace(selected=SimpleNamespace(routine=row)), runtime=runtime
            )

            async def discord_admin(*args, **kwargs):
                return scenario == "admin"

            scope.update(
                checks=SimpleNamespace(is_guild_admin=discord_admin),
                pause_routine_via_panel=scope["pause_routine"],
                resume_routine_via_panel=scope["resume_routine"],
                _rerender=AsyncMock(side_effect=lambda *a: trace.append("render")),
                _send_callback_error=AsyncMock(side_effect=lambda *a: trace.append("error")),
            )
            function = (
                implementation.callback if implementation is old else module._PauseButton.callback
            )
            args = (SimpleNamespace(view=view), interaction)
        elif platform == "slack":
            scope.update(
                resolve_web_client=AsyncMock(return_value=client),
                load_routines=AsyncMock(return_value=([], 0, {})),
                routines_viewer=AsyncMock(return_value=USER),
                build_content_view=lambda *a, **kw: {"view": "routines"},
                surface_command_error=AsyncMock(side_effect=lambda **kw: trace.append("error")),
            )
            function = implementation.handle_routine_action
            args = (
                runtime,
                {
                    "team": {"id": "1"},
                    "user": {"id": USER},
                    "view": {"id": "V1", "private_metadata": "C1"},
                    "actions": [
                        {"action_id": f"routine_action:{ROUTINE}", "selected_option": {"value": op}}
                    ],
                },
            )
        else:
            scope.update(
                card_actor=AsyncMock(
                    return_value=SimpleNamespace(
                        tenant_id=TENANT,
                        user_id=USER,
                        is_admin=scenario == "admin",
                        admin_for=lambda _action: scenario == "admin",
                    )
                ),
                store=SimpleNamespace(
                    get_routine=load,
                    pause_routine=scope["pause_routine"],
                    resume_routine=scope["resume_routine"],
                ),
            )
            panel = SimpleNamespace(_runtime=runtime, _panel=AsyncMock(return_value=MagicMock()))
            scope["replace_card"] = lambda card: "render"
            scope["toast"] = lambda text: text
            function = implementation._act if implementation is old else module.RoutinesPanel._act
            args = (
                panel,
                SimpleNamespace(
                    value=SimpleNamespace(
                        action=SimpleNamespace(data={"op": op, "routine": str(ROUTINE)})
                    )
                ),
            )
        for key, value in scope.items():
            monkeypatch.setitem(function.__globals__, key, value)
        try:
            trace.append(("response", await function(*args)))
        except ValueError:
            trace.append("error")
        return trace

    expected = await run(old)
    actual = await run(module)
    # Routine transitions now pass the revision checked by the permission
    # callback. The archived handlers predate that CAS argument; every other
    # effect (including errors, rollback and rendering) remains equivalent.
    for item in expected:
        if isinstance(item, tuple) and item[0] == "write":
            assert "expected_updated_at" not in item[2]
            item[2]["expected_updated_at"] = AT
    if scenario == "stale" and op == "resume" and platform in ("slack", "teams"):
        # Slot-safe cutover refuses a repeated resume instead of replacing the
        # seeded next slot. The archived handlers wrote and rendered again.
        assert not any(isinstance(item, tuple) and item[0] == "write" for item in actual)
        if platform == "slack":
            refusal = (
                "refusal",
                {"channel": "C1", "user": USER, "text": "This routine is already on."},
            )
            assert refusal in actual
            assert expected[4][0] == "write"
            expected = expected[:4] + [refusal] + expected[5:7] + [("response", None)]
        else:
            assert ("response", "This routine is already on.") in actual
            expected = [
                item for item in expected if not (isinstance(item, tuple) and item[0] == "write")
            ]
            expected[-1] = ("response", "This routine is already on.")
    assert expected == actual
