"""Helper tools: who may be asked, by which turn, and how replies come back."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import helpers as tool
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.core.anthropic_guard import without_anthropic
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import helper_asks
from daimon.core.stores.agent_helpers import set_helpers
from daimon.core.stores.domain import Role, TurnOriginRow
from daimon.core.stores.selfhosted_resources import SelfHostedAgents
from daimon.core.stores.turn_origins import create_origin, mark_serving_helpers
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from fastmcp.exceptions import ToolError
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import AgentSpec
from mux.profiles.selfhosted import LOOP
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class _World:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker
        self.agents: list[dict[str, object]] = []

    def runtime(self) -> McpRuntime:
        router = MARouter()
        router.add("GET", r"/v1/agents", lambda _r, _m: list_response(self.agents))
        return McpRuntime(
            session_factory=self.sessionmaker,
            client=build_fake_anthropic(router.dispatch),
            settings=Settings(
                database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://test/test")),
                anthropic=AnthropicSettings(api_key=SecretStr("test")),
            ),
            deployment_default=DeploymentDefault(),
        )


async def _setup(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    serving: bool = True,
    helper_of: uuid.UUID | None = None,
) -> tuple[_World, AuthIdentity, TurnOriginRow]:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    now = datetime.now(UTC)
    origin = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="slack",
        parent_channel_id="C1",
        thread_id="1.2",
        responder_ma_agent_id="ag_lead",
        responder_name="lead",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
        expires_at=now + timedelta(minutes=10),
        now=now,
        chat_session_id="test-session",
        helper_of=helper_of,
    )
    if serving:
        await mark_serving_helpers(db_session, origin_id=origin.id)
    await set_helpers(
        db_session, tenant_id=tenant.id, agent_id="ag_lead", helper_ids=["ag_argon", "ag_gone"]
    )
    await db_session.commit()
    world = _World(sessionmaker)
    for agent_id, name in (("ag_lead", "lead"), ("ag_argon", "argon"), ("ag_other", "other")):
        world.agents.append(
            ma_agent(
                id=agent_id,
                name=name,
                description="Reads papers" if name == "argon" else None,
                metadata={MA_METADATA_KEY_TENANT: str(tenant.id), MA_METADATA_KEY_NAME: name},
            ).model_dump(mode="json")
        )
    auth = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="slack",
        chat_session_id="test-session",
    )
    return world, auth, origin


async def test_the_roster_lists_live_helpers_only(
    db_session: AsyncSession, committing_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world, auth, origin = await _setup(db_session, committing_sessionmaker)

    listed = await tool._list_helpers_impl(  # pyright: ignore[reportPrivateUsage]
        world.runtime(), auth, origin_context_id=str(origin.id)
    )

    assert [(h.agent_id, h.name, h.purpose) for h in listed.helpers] == [
        ("ag_argon", "argon", "Reads papers")
    ], "an archived or deleted helper and an agent not on the list never show"


async def test_a_self_hosted_agent_lists_its_self_hosted_helpers(
    db_session: AsyncSession, committing_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world, auth, origin = await _setup(db_session, committing_sessionmaker)
    scope = Scope(
        tenant_id=str(auth.tenant_id),
        account_id=str(auth.account_id),
        principal_id="operator",
        authorization_id="test",
    )
    agents = SelfHostedAgents(committing_sessionmaker)
    ids = [
        (
            await agents.create(
                scope,
                AgentSpec(name=name, model=ModelRef(provider=LOOP.provider, id="gpt-6-luna")),
                key=name,
            )
        ).ref.id
        for name in ("lead-sh", "argon-sh")
    ]
    async with committing_sessionmaker.begin() as session:
        await set_helpers(session, tenant_id=auth.tenant_id, agent_id=ids[0], helper_ids=ids[1:])
    origin = origin.model_copy(update={"responder_ma_agent_id": ids[0]})

    with without_anthropic():
        roster = await tool._roster(world.runtime(), auth, origin)  # pyright: ignore[reportPrivateUsage]

    assert [(h.agent_id, h.name) for h in roster] == [(ids[1], "argon-sh")], (
        "a self-hosted conversation reads the tenant's own agents, never Anthropic"
    )


async def test_an_ask_waits_for_the_helpers_reply(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool, "WAIT_S", 0.0)
    world, auth, origin = await _setup(db_session, committing_sessionmaker)
    runtime = world.runtime()

    started = await tool._ask_helper_impl(  # pyright: ignore[reportPrivateUsage]
        runtime,
        auth,
        origin_context_id=str(origin.id),
        helper="argon",
        message="Summarise the paper",
        wait=True,
    )
    assert started.status == "queued", "a wait that times out says the ask is still open"
    async with committing_sessionmaker.begin() as session:
        await helper_asks.finish_ask(
            session,
            ask_id=uuid.UUID(started.ask_id),
            status="done",
            reply="x" * (tool.MAX_REPLY_CHARS + 5),
            now=datetime.now(UTC),
        )
    waited = await tool._wait_for_helpers_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, auth, origin_context_id=str(origin.id), ask_ids=[started.ask_id]
    )

    [answer] = waited.answers
    assert (answer.helper, answer.status) == ("argon", "done")
    assert answer.reply is not None and answer.reply.endswith("[reply cut]")


@pytest.mark.parametrize(
    ("serving", "helper", "words"),
    [
        (False, "argon", "can't be asked from this conversation"),
        (True, "other", "Your helpers: argon"),
        (True, "ag_gone", "Your helpers: argon"),
    ],
)
async def test_an_ask_is_refused_outside_a_serving_turn_or_the_roster(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    serving: bool,
    helper: str,
    words: str,
) -> None:
    world, auth, origin = await _setup(db_session, committing_sessionmaker, serving=serving)

    with pytest.raises(ToolError, match=words):
        await tool._ask_helper_impl(  # pyright: ignore[reportPrivateUsage]
            world.runtime(),
            auth,
            origin_context_id=str(origin.id),
            helper=helper,
            message="hi",
            wait=False,
        )


async def test_a_turn_asks_helpers_a_bounded_number_of_times(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool, "MAX_ASKS", 1)
    world, auth, origin = await _setup(db_session, committing_sessionmaker)
    runtime = world.runtime()

    async def ask() -> object:
        return await tool._ask_helper_impl(  # pyright: ignore[reportPrivateUsage]
            runtime,
            auth,
            origin_context_id=str(origin.id),
            helper="argon",
            message="hi",
            wait=False,
        )

    await ask()
    with pytest.raises(ToolError, match="Answer now"):
        await ask()


async def test_a_turn_reads_only_its_own_asks(
    db_session: AsyncSession, committing_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    world, auth, origin = await _setup(db_session, committing_sessionmaker)
    async with committing_sessionmaker.begin() as session:
        theirs = await helper_asks.create_ask(
            session,
            tenant_id=auth.tenant_id,
            origin_id=(await _setup(db_session, committing_sessionmaker))[2].id,
            helper_agent_id="ag_argon",
            helper_name="argon",
            message="secret",
            now=datetime.now(UTC),
        )

    with pytest.raises(ToolError, match="ask ids ask_helper gave you"):
        await tool._wait_for_helpers_impl(  # pyright: ignore[reportPrivateUsage]
            world.runtime(), auth, origin_context_id=str(origin.id), ask_ids=[str(theirs.id)]
        )


async def test_a_helpers_turn_cannot_change_the_conversation(
    db_session: AsyncSession, committing_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Hand-offs, fresh starts, setup, timers and asking helpers refuse; other tools run."""
    world, auth, origin = await _setup(db_session, committing_sessionmaker, helper_of=uuid.uuid4())
    runtime = world.runtime()

    assert (await require_turn_origin(runtime, auth, str(origin.id))).id == origin.id
    with pytest.raises(ToolError, match="You are helping another agent"):
        await tool._list_helpers_impl(  # pyright: ignore[reportPrivateUsage]
            runtime, auth, origin_context_id=str(origin.id)
        )
