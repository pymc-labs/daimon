"""Hub admins read sealed conversations; nobody continues one from the hub.

Driven through the registered hub tools over HTTP, auth middleware included.
Admins are trusted (docs/architecture.md, "Trust model"): the hub runs
headless and its output reaches only the admin, so an admin may list and
read any sealed conversation of the agent, anyone's. A follow-up from the
hub would join the channel's own conversation, so continuing a sealed
channel session stays refused for admins too, and sends nothing. A channel
admin holds the same hub read, limited to the channels they administer.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from daimon.adapters.mcp.hub.app import build_hub_app
from daimon.adapters.mcp.hub.claims import encode_hub_claims
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.defaults.metadata import MA_METADATA_KEY_PRIVATE_DM
from daimon.core.hub_identity import HubTenant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.rule_views import routine_origin
from daimon.core.session_seal import origin_stamp
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.channel_admins import delete_channel_admins, set_channel_admins
from daimon.core.stores.domain import Role, RoutineDestinationKind, RoutineRow
from daimon.testing import ma_agent, ma_session
from daimon.testing.factories import make_ledger_entry, make_platform_principal, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_turn_parity import _TOKEN, _call_tool, _runtime  # pyright: ignore[reportPrivateUsage]

_AGENT = "ag_acme"
_SEALED = "chan-acme"
_TOPIC = "the acme merger terms"


class _Hub:
    def __init__(
        self,
        db: AsyncSession,
        sessionmaker: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        account_id: uuid.UUID,
    ) -> None:
        self.db = db
        self.sessionmaker = sessionmaker
        self.tenant_id = tenant_id
        self.account_id = account_id
        self.sessions: dict[str, dict[str, Any]] = {}
        self.sent: list[str] = []

    def add_session(
        self, session_id: str, *, account: uuid.UUID | None = None, **stamps: str
    ) -> None:
        self.sessions[session_id] = ma_session(
            id=session_id,
            agent_id=_AGENT,
            status="idle",
            metadata={"daimon_account": str(account or self.account_id), **stamps},
        ).model_dump(mode="json")

    async def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        router = MARouter()
        agent = ma_agent(
            id=_AGENT,
            name="acme-project",
            metadata={"daimon_tenant": str(self.tenant_id), "daimon_name": "acme-project"},
        ).model_dump(mode="json")
        router.add("GET", r"/v1/agents", lambda _r, _m: list_response([agent]))
        router.add(
            "GET", r"/v1/sessions$", lambda _r, _m: list_response(list(self.sessions.values()))
        )
        router.add(
            "GET",
            r"/v1/sessions/([^/?]+)/events",
            lambda _r, _m: list_response(
                [
                    {
                        "id": "sevt_1",
                        "type": "user.message",
                        "content": [{"type": "text", "text": _TOPIC}],
                        "processed_at": "2026-09-30T10:00:00Z",
                    }
                ]
            ),
        )

        def send(_r: httpx.Request, m: re.Match[str]) -> httpx.Response:
            self.sent.append(m.group(1))
            return list_response(
                [
                    {
                        "id": "sevt_sent",
                        "type": "user.message",
                        "content": [{"type": "text", "text": "next"}],
                        "processed_at": None,
                    }
                ]
            )

        router.add("POST", r"/v1/sessions/([^/?]+)/events", send)
        router.add(
            "GET",
            r"/v1/sessions/([^/?]+)$",
            lambda _r, m: httpx.Response(200, json=self.sessions[m.group(1)]),
        )
        runtime = _runtime(build_fake_anthropic(router.dispatch), self.sessionmaker)
        hub_tenant = HubTenant(
            tenant_id=self.tenant_id,
            account_id=self.account_id,
            workspace_id="g1",
            workspace_name="PyMC",
        )
        claims = encode_hub_claims(platform="discord", platform_user_id="u1", tenants=[hub_tenant])
        auth = StaticTokenVerifier(
            tokens={_TOKEN: {"sub": "u1", "client_id": "c", "upstream_claims": claims}}
        )
        mcp = build_hub_app(platform="discord", runtime=runtime, auth=auth, billing_config=None)
        daimon_id = str(derive_agent_uuid(tenant_id=self.tenant_id, ma_agent_id=_AGENT))
        result = await _call_tool(
            mcp.http_app(path="/mcp", stateless_http=True, json_response=True),
            "/mcp",
            _TOKEN,
            name=name,
            arguments={"daimon_id": daimon_id, **arguments},
        )
        return result["result"]


@pytest.fixture
async def hub(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> _Hub:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="u1", tenant=tenant
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await set_access_policy(
        db_session, tenant_id=tenant.id, policy=TenantAccessPolicy(sealed_channel_ids=(_SEALED,))
    )
    await db_session.commit()
    return _Hub(db_session, db_session_factory, tenant.id, principal.account_id)


async def _as(hub: _Hub, role: Role) -> None:
    await set_role(hub.db, hub.account_id, role)
    await hub.db.commit()


async def test_an_admin_lists_and_reads_any_sealed_conversation_from_the_hub(hub: _Hub) -> None:
    await _as(hub, Role.ADMIN)
    someone_else = uuid.uuid4()
    hub.add_session("ses_mine_sealed", daimon_channel=_SEALED, daimon_thread="thr-1")
    hub.add_session("ses_theirs_sealed", account=someone_else, daimon_channel=_SEALED)

    listed = await hub.call("list_my_sessions")
    assert not listed.get("isError"), listed
    assert "ses_mine_sealed" in str(listed) and "ses_theirs_sealed" in str(listed)

    for handle in ("ses_mine_sealed", "ses_theirs_sealed"):
        session = await hub.call("get_session", handle=handle)
        assert not session.get("isError"), session
        events = await hub.call("list_events", handle=handle)
        assert not events.get("isError"), events
        assert _TOPIC in str(events)
    assert hub.sent == []


@pytest.mark.parametrize("tool", ["continue_turn", "ask"])
@pytest.mark.parametrize("role", [Role.ADMIN, Role.USER])
async def test_nobody_continues_a_sealed_channel_conversation_from_the_hub(
    hub: _Hub, tool: str, role: Role
) -> None:
    await _as(hub, role)
    hub.add_session("ses_sealed", daimon_channel=_SEALED, daimon_thread="thr-1")
    before = dict(hub.sessions["ses_sealed"]["metadata"])

    result = await hub.call(tool, handle="ses_sealed", message="private personnel note")

    assert result.get("isError"), result
    if role is Role.ADMIN:
        assert "continue it in its channel" in str(result)
    assert hub.sent == [], "a refused follow-up must never reach the session"
    assert hub.sessions["ses_sealed"]["metadata"] == before


async def test_an_admin_continues_their_own_unsealed_hub_conversation(hub: _Hub) -> None:
    await _as(hub, Role.ADMIN)
    hub.add_session("ses_headless")

    result = await hub.call("continue_turn", handle="ses_headless", message="next")

    assert not result.get("isError"), result
    assert hub.sent == ["ses_headless"]


async def test_a_member_never_reads_a_sealed_conversation_from_the_hub(hub: _Hub) -> None:
    hub.add_session("ses_sealed", daimon_channel=_SEALED, daimon_thread="thr-1")
    hub.add_session("ses_theirs", account=uuid.uuid4(), daimon_channel="chan-open")

    listed = await hub.call("list_my_sessions")
    assert "ses_sealed" not in str(listed) and "ses_theirs" not in str(listed)
    events = await hub.call("list_events", handle="ses_sealed")
    assert events.get("isError") and _TOPIC not in str(events)
    theirs = await hub.call("list_events", handle="ses_theirs")
    assert theirs.get("isError") and _TOPIC not in str(theirs)


async def test_a_demoted_admin_loses_hub_access_when_their_stored_role_changes(
    hub: _Hub,
) -> None:
    """The stored role decides; a platform turn records the live one."""
    hub.add_session("ses_sealed", daimon_channel=_SEALED, daimon_thread="thr-1")
    await _as(hub, Role.ADMIN)
    assert _TOPIC in str(await hub.call("list_events", handle="ses_sealed"))

    await _as(hub, Role.USER)
    events = await hub.call("list_events", handle="ses_sealed")
    assert events.get("isError") and _TOPIC not in str(events)


async def _map_thread(hub: _Hub, session_id: str, thread_id: str) -> None:
    from daimon.core.stores import thread_sessions

    await thread_sessions.create_thread_session(
        hub.db,
        tenant_id=hub.tenant_id,
        platform="discord",
        thread_id=thread_id,
        account_id=hub.account_id,
        ma_session_id=session_id,
        ma_agent_id=_AGENT,
    )
    await hub.db.commit()


async def test_an_admin_reads_another_members_legacy_sealed_conversation(hub: _Hub) -> None:
    """A session from before the channel stamp is found by its thread mapping."""
    hub.add_session("ses_legacy", account=uuid.uuid4())
    await _map_thread(hub, "ses_legacy", "thr-legacy")
    await _as(hub, Role.ADMIN)

    listed = await hub.call("list_my_sessions")
    assert not listed.get("isError") and "ses_legacy" in str(listed), listed
    session = await hub.call("get_session", handle="ses_legacy")
    assert not session.get("isError"), session
    events = await hub.call("list_events", handle="ses_legacy")
    assert not events.get("isError") and _TOPIC in str(events), events
    assert hub.sent == []


@pytest.mark.parametrize("tool", ["continue_turn", "ask"])
async def test_an_admin_never_continues_another_members_legacy_conversation(
    hub: _Hub, tool: str
) -> None:
    hub.add_session("ses_legacy", account=uuid.uuid4())
    await _map_thread(hub, "ses_legacy", "thr-legacy")
    await _as(hub, Role.ADMIN)

    result = await hub.call(tool, handle="ses_legacy", message="private personnel note")

    assert result.get("isError"), result
    assert hub.sent == []


async def test_a_member_never_reads_another_members_legacy_conversation(hub: _Hub) -> None:
    hub.add_session("ses_legacy", account=uuid.uuid4())
    await _map_thread(hub, "ses_legacy", "thr-legacy")

    listed = await hub.call("list_my_sessions")
    assert "ses_legacy" not in str(listed)
    events = await hub.call("list_events", handle="ses_legacy")
    assert events.get("isError") and _TOPIC not in str(events)


async def test_an_admin_never_reads_another_members_legacy_dm_scope(hub: _Hub) -> None:
    hub.add_session("ses_dm", account=uuid.uuid4())
    await _map_thread(hub, "ses_dm", "dm:00000000-0000-0000-0000-000000000001")
    await _as(hub, Role.ADMIN)

    listed = await hub.call("list_my_sessions")
    assert "ses_dm" not in str(listed)
    events = await hub.call("list_events", handle="ses_dm")
    assert events.get("isError") and _TOPIC not in str(events)


async def test_an_admin_never_reads_another_members_teams_personal_chat(hub: _Hub) -> None:
    """Stamped or legacy, a Teams 1:1 chat is private: no admin reads it."""
    hub.add_session(
        "ses_teams_dm",
        account=uuid.uuid4(),
        daimon_channel="a:1personalchat",
        daimon_thread="a:1personalchat",
    )
    hub.add_session("ses_teams_legacy", account=uuid.uuid4())
    await _map_thread(hub, "ses_teams_legacy", "a:1personalchat")
    hub.add_session("ses_slack_im", account=uuid.uuid4(), daimon_channel="D0SOMEONE")
    await _as(hub, Role.ADMIN)

    listed = await hub.call("list_my_sessions")
    for handle in ("ses_teams_dm", "ses_teams_legacy", "ses_slack_im"):
        assert handle not in str(listed)
        events = await hub.call("list_events", handle=handle)
        assert events.get("isError") and _TOPIC not in str(events), (handle, events)
    assert hub.sent == []


async def _grant(hub: _Hub, channel_id: str) -> None:
    await set_channel_admins(
        hub.db,
        tenant_id=hub.tenant_id,
        platform="discord",
        channel_id=channel_id,
        role_ids=[],
        user_ids=["u1"],
        actor_account_id=None,
    )
    await hub.db.commit()


async def test_a_channel_admin_reads_sealed_conversations_of_their_channels_only(
    hub: _Hub,
) -> None:
    """Anyone's sealed conversation in a channel they administer; never another channel's, a
    private DM, or once the grant is gone."""
    await set_access_policy(
        hub.db,
        tenant_id=hub.tenant_id,
        policy=TenantAccessPolicy(sealed_channel_ids=(_SEALED, "chan-ops")),
    )
    await _grant(hub, _SEALED)
    someone_else = uuid.uuid4()
    hub.add_session("ses_mine", daimon_channel=_SEALED, daimon_thread="thr-1")
    hub.add_session("ses_theirs", account=someone_else, daimon_channel=_SEALED)
    hub.add_session("ses_mine_ops", daimon_channel="chan-ops")
    hub.add_session("ses_theirs_ops", account=someone_else, daimon_channel="chan-ops")
    hub.add_session(
        "ses_theirs_dm", account=someone_else, daimon_channel=_SEALED, daimon_private_dm="x"
    )
    hub.add_session("ses_legacy", account=someone_else)
    await _map_thread(hub, "ses_legacy", _SEALED)

    listed = str(await hub.call("list_my_sessions"))
    assert "ses_mine" in listed and "ses_theirs" in listed, "their channel's are listed"
    for handle in ("ses_mine", "ses_theirs"):
        events = await hub.call("list_events", handle=handle)
        assert not events.get("isError") and _TOPIC in str(events), (handle, events)
    for handle in ("ses_mine_ops", "ses_theirs_ops", "ses_theirs_dm", "ses_legacy"):
        assert handle not in listed, f"{handle} is outside the channels they administer"
        events = await hub.call("list_events", handle=handle)
        assert events.get("isError") and _TOPIC not in str(events), (handle, events)

    await delete_channel_admins(
        hub.db, tenant_id=hub.tenant_id, platform="discord", channel_id=_SEALED
    )
    await hub.db.commit()
    events = await hub.call("list_events", handle="ses_theirs")
    assert events.get("isError") and _TOPIC not in str(events), "the stored grant decides"
    assert hub.sent == []


@pytest.mark.parametrize("tool", ["continue_turn", "ask"])
async def test_a_channel_admin_never_continues_a_sealed_conversation_from_the_hub(
    hub: _Hub, tool: str
) -> None:
    await _grant(hub, _SEALED)
    hub.add_session("ses_sealed", daimon_channel=_SEALED, daimon_thread="thr-1")

    result = await hub.call(tool, handle="ses_sealed", message="private personnel note")

    assert result.get("isError") and "continue it in its channel" in str(result), result
    assert hub.sent == [], "a refused follow-up must never reach the session"


async def test_a_channel_admin_reads_only_sessions_sealed_inside_their_channels(
    hub: _Hub,
) -> None:
    """Every seal on the session must lie in an administered channel: the channel itself,
    its thread sealed on its own, or a Slack-style channel:ts. A seal inherited from
    another channel keeps it closed."""
    await set_access_policy(
        hub.db,
        tenant_id=hub.tenant_id,
        policy=TenantAccessPolicy(sealed_channel_ids=(_SEALED, "chan-ops", "thr-own")),
    )
    await _grant(hub, _SEALED)
    await _grant(hub, "chan-open")
    theirs = uuid.uuid4()
    hub.add_session(
        "ses_thread_sealed",
        account=theirs,
        daimon_channel="chan-open",
        daimon_thread="thr-own",
        daimon_sealed="thr-own",
    )
    hub.add_session(
        "ses_mixed",
        account=theirs,
        daimon_channel=_SEALED,
        daimon_thread="thr-1",
        daimon_sealed=f"{_SEALED},thr-1,{_SEALED}:171.2",
    )
    hub.add_session(
        "ses_inherited",
        account=theirs,
        daimon_channel=_SEALED,
        daimon_thread="thr-1",
        daimon_sealed=f"{_SEALED},chan-ops",
    )
    hub.add_session(
        "ses_inherited_thread",
        account=theirs,
        daimon_channel="chan-open",
        daimon_thread="thr-2",
        daimon_sealed="thr-own",
    )

    listed = str(await hub.call("list_my_sessions"))
    for handle in ("ses_thread_sealed", "ses_mixed"):
        assert handle in listed, handle
        events = await hub.call("list_events", handle=handle)
        assert not events.get("isError") and _TOPIC in str(events), (handle, events)
    for handle in ("ses_inherited", "ses_inherited_thread"):
        assert handle not in listed, f"{handle} carries a seal from outside their channels"
        events = await hub.call("list_events", handle=handle)
        assert events.get("isError") and _TOPIC not in str(events), (handle, events)
    assert hub.sent == []


_ROUTINE_CHANNEL = "chan-routine"


def _routine_row(
    kind: RoutineDestinationKind | None, destination: str | None, channel: str | None
) -> RoutineRow:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return RoutineRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by_user_id="u-owner",
        agent_id=_AGENT,
        agent_name="acme-project",
        cron_expr="0 * * * *",
        timezone="UTC",
        trigger_message="hi",
        enabled=True,
        next_fire_at=None,
        last_fired_at=None,
        last_error=None,
        last_result_tail=None,
        destination_kind=kind,
        destination_id=destination,
        channel_id=channel,
        created_at=now,
        updated_at=now,
    )


_ROUTINE_SHAPES = {
    "channel": _routine_row("channel", _ROUTINE_CHANNEL, _ROUTINE_CHANNEL),
    "thread": _routine_row("thread", "thr-routine", _ROUTINE_CHANNEL),
    # Made in a DM started from the channel: its saved channel is the DM's source.
    "dm": _routine_row(None, None, _ROUTINE_CHANNEL),
    "no-destination": _routine_row(None, None, None),
    "legacy-thread": _routine_row("thread", "thr-legacy", None),
    "legacy-channel": _routine_row("channel", _ROUTINE_CHANNEL, None),
}


def _routine_stamp(row: RoutineRow) -> dict[str, str]:
    """What `run_turn` stamps on the routine's session (`create_session`)."""
    origin = routine_origin(TenantAccessPolicy(), row, platform="discord")
    if origin is None:
        return {}
    return {
        **origin_stamp(channel_id=origin.channel_id, thread_id=origin.thread_id),
        MA_METADATA_KEY_PRIVATE_DM: origin.private_dm_id,
    }


@pytest.mark.parametrize("shape", list(_ROUTINE_SHAPES))
@pytest.mark.parametrize("caller", ["owner", "server-admin", "channel-admin", "outsider"])
async def test_a_routine_transcript_reads_only_for_its_owner_from_the_hub(
    hub: _Hub, shape: str, caller: str
) -> None:
    """As before routine sessions carried a channel: the owner reads every shape,
    and no server admin, channel admin of its channel or other member reads one."""
    if caller == "server-admin":
        await _as(hub, Role.ADMIN)
    if caller == "channel-admin":
        for channel in (_ROUTINE_CHANNEL, "thr-legacy"):
            await _grant(hub, channel)
    account = hub.account_id if caller == "owner" else uuid.uuid4()
    hub.add_session("ses_routine", account=account, **_routine_stamp(_ROUTINE_SHAPES[shape]))

    listed = str(await hub.call("list_my_sessions"))
    events = await hub.call("list_events", handle="ses_routine")

    if caller == "owner":
        assert "ses_routine" in listed, "the owner lists their routine's session"
        assert not events.get("isError") and _TOPIC in str(events), events
    else:
        assert "ses_routine" not in listed, f"a {caller} never lists another's routine"
        assert events.get("isError") and _TOPIC not in str(events), (caller, events)
    assert hub.sent == []


async def test_a_sealed_channels_routine_transcript_stays_inside_it_for_its_owner(
    hub: _Hub,
) -> None:
    """The private stamp leaves the seal binding: the owner's hub read, outside the
    channel, is refused for a routine that fires into a sealed channel."""
    row = _routine_row("channel", _SEALED, _SEALED)
    origin = routine_origin(
        TenantAccessPolicy(sealed_channel_ids=(_SEALED,)), row, platform="discord"
    )
    assert origin is not None
    hub.add_session(
        "ses_routine",
        **origin_stamp(channel_id=origin.channel_id, thread_id=None, seal=origin.seal_ids),
        daimon_private_dm=origin.private_dm_id,
    )

    events = await hub.call("list_events", handle="ses_routine")

    assert events.get("isError") and _TOPIC not in str(events), events
