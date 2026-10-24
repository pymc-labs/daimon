"""Every watch decision uses REST proof; poisoned gateway state is unreachable."""

from unittest.mock import MagicMock

import discord
import pytest
from daimon.adapters.discord.watch_access import require_watch_thread_access
from daimon.core.stores import watches
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.turn.errors import AdmissionDenied

from .watch_fixtures import WatchDiscordRest, enrolled_page_watch


class PoisonCache:
    def __getattribute__(self, name):
        raise AssertionError(f"Watch authorization read gateway cache attribute: {name}")


def gateway_with_stale_owner():
    client = discord.Client(intents=discord.Intents.none())
    state = client._connection
    guild = discord.Guild(
        data={
            "id": "710000099",
            "name": "cached",
            "owner_id": "555",
            "roles": [
                {"id": "710000099", "name": "@everyone", "permissions": "0"},
                {
                    "id": "77",
                    "name": "stale admin",
                    "permissions": str(discord.Permissions.all().value),
                },
            ],
        },
        state=state,
    )
    member = discord.Member(
        data={
            "user": {"id": "555", "username": "cached", "discriminator": "0", "avatar": None},
            "roles": ["77"],
            "flags": 0,
        },
        guild=guild,
        state=state,
    )
    guild._members[555] = member
    state._guilds[guild.id] = guild
    assert member.guild_permissions.administrator  # The former-owner repro.
    return client


@pytest.mark.fresh_schema
@pytest.mark.parametrize(
    "mode",
    [
        "former_owner_removed",
        "former_owner_hidden",
        "former_owner_locked",
        "stale_history",
        "stale_manager",
        "stale_admin",
        "locked_manager",
        "fresh_manager",
        "poison_public",
        "poison_private",
        "poison_manager",
        "guild_failed",
        "roles_failed",
        "member_failed",
        "thread_failed",
        "parent_failed",
        "membership_failed",
        "parent_overwrite",
        "member_timeout",
        "current_owner",
    ],
)
async def test_watch_authority_is_entirely_rest(db_session_factory, monkeypatch, mode):
    factory, _, _, _, _, watch, key = await enrolled_page_watch(db_session_factory, monkeypatch)
    async with factory() as db:
        row = await get_continuation(db, idempotency_key=key)
    client = gateway_with_stale_owner()
    rest = WatchDiscordRest(client)
    rest.private = True
    denied = discord.NotFound(MagicMock(status=404, reason="gone"), "gone")
    if mode.startswith("poison_"):
        # Any read of a cached guild, role, member, user, parent or thread --
        # even cache lookup machinery -- fails the test instead of granting access.
        client._connection = PoisonCache()
        client.get_guild = PoisonCache()
        client.get_channel = PoisonCache()
        client.get_user = PoisonCache()
        rest.private = mode != "poison_public"
    if mode in {
        "former_owner_removed",
        "stale_manager",
        "stale_admin",
        "fresh_manager",
        "poison_manager",
    }:
        rest.membership_error = denied
    if mode == "former_owner_hidden":
        rest.permissions.view_channel = False
    if mode == "stale_history":
        rest.permissions.read_message_history = False
    if mode in {"former_owner_locked", "locked_manager"}:
        rest.locked = True
    if mode in {"locked_manager", "fresh_manager", "poison_manager"}:
        rest.permissions.manage_threads = True
    if mode == "parent_overwrite":
        rest.overwrites = [
            {
                "id": "77",
                "type": 0,
                "allow": "0",
                "deny": str(discord.Permissions(view_channel=True).value),
            }
        ]
    if mode == "member_timeout":
        member_payload = rest.member(710000099, 555)
        member_payload["communication_disabled_until"] = "2099-01-01T00:00:00+00:00"
        rest.http.get_member.side_effect = lambda *_: member_payload
    if mode == "current_owner":
        rest.owner_id = "555"
        rest.permissions = discord.Permissions.none()
        rest.locked = True
        rest.membership_error = denied
    failures = {
        "guild_failed": "guild_error",
        "roles_failed": "roles_error",
        "member_failed": "member_error",
        "thread_failed": "channel_error",
        "parent_failed": "parent_error",
        "membership_failed": "membership_error",
    }
    if mode in failures:
        setattr(
            rest,
            failures[mode],
            discord.Forbidden(MagicMock(status=403, reason="denied"), "denied"),
        )
    allowed = mode in {
        "locked_manager",
        "fresh_manager",
        "poison_public",
        "poison_private",
        "poison_manager",
        "current_owner",
    }
    if allowed:
        access = await require_watch_thread_access(factory, row, client)
        member = access.member
        assert member.guild.owner_id == int(rest.owner_id)
        assert member.display_name == "owner"  # Never the cached user named "cached".
        if rest.permissions.manage_threads or mode == "current_owner" or not rest.private:
            rest.http.get_thread_member.assert_not_awaited()
        else:
            rest.http.get_thread_member.assert_awaited_once_with(4242, 555)
    else:
        with pytest.raises(AdmissionDenied):
            await require_watch_thread_access(factory, row, client)
        async with factory() as db:
            paused = await watches.get_watch(db, watch.id)
        assert paused.status == "paused"
        assert paused.source_state["reason"].startswith("Watch paused:")
        if mode in {"former_owner_removed", "stale_manager", "stale_admin"}:
            rest.http.get_thread_member.assert_awaited_once_with(4242, 555)
    rest.http.get_guild.assert_awaited_once_with(710000099, with_counts=False)


@pytest.mark.fresh_schema
@pytest.mark.parametrize(
    "failure", [503, 403, 404, "reset", "timeout", "parent_changed", "member_changed"]
)
async def test_watch_transport_failure_only_pauses_proved_denial(
    db_session_factory, monkeypatch, failure
):
    from daimon.core.turn.output import OutputAuthorization

    factory, _, _, _, _, watch, key = await enrolled_page_watch(db_session_factory, monkeypatch)
    async with factory() as db:
        row = await get_continuation(db, idempotency_key=key)
    client = discord.Client(intents=discord.Intents.none())
    rest = WatchDiscordRest(client)
    if failure in {"parent_changed", "member_changed"}:
        error = ValueError("binding changed")
    elif failure == "reset":
        error = OSError("connection reset")
    elif failure == "timeout":
        error = TimeoutError()
    else:
        exception = {403: discord.Forbidden, 404: discord.NotFound}.get(
            failure, discord.HTTPException
        )
        error = exception(MagicMock(status=failure, reason="failed"), "failed")
    if failure == "parent_changed":
        channel = rest.channel

        def moved_channel(channel_id):
            payload = channel(channel_id)
            if channel_id == 4242:
                payload["parent_id"] = "9999"
            return payload

        rest.http.get_channel.side_effect = moved_channel
    elif failure == "member_changed":
        member = rest.member(710000099, 555)
        member["user"]["id"] = "9999"
        rest.http.get_member.side_effect = lambda *_: member
    else:
        rest.guild_error = error

    async def authorize():
        await require_watch_thread_access(factory, row, client)

    delivery = OutputAuthorization(authorize)
    proved_denial = failure in {403, 404, "parent_changed", "member_changed"}
    with pytest.raises(AdmissionDenied if proved_denial else type(error)):
        await delivery.check()
    async with factory() as db:
        current = await watches.get_watch(db, watch.id)
    assert (current.status == "paused") == proved_denial
    assert delivery.failed == proved_denial
    if not proved_denial:
        # Refusing this output must not poison the final answer or next tick.
        rest.guild_error = None
        await delivery.check()
