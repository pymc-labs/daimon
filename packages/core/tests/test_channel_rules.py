"""Setting channel and agent rules, and the copy that gives a channel its own agent."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
import pytest
import structlog.testing
from anthropic import AsyncAnthropic
from anthropic.types.beta import SkillListResponse
from daimon.core import channel_rules
from daimon.core.access_policy import AgentRule, ChannelReaders, ChannelRule, TenantAccessPolicy
from daimon.core.agent_fork import AgentCopy, fork_agent
from daimon.core.agent_mcp_credentials import save_agent_mcp_credential
from daimon.core.authz import Subject
from daimon.core.channel_environments import LIMITED_OPEN_NETWORK_WARNING
from daimon.core.channel_rules import (
    ChannelRuleRefused,
    RuleChange,
    copy_name,
    render_rule_refusal,
    set_agent_rule,
    set_category_rule,
    set_channel_rule,
)
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    tenant_scoped_display_title,
)
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.permissions import RuleRefused, runs_only_in, thread_rule_key
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.skills.ingest import bundle_from_markdown
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.user_skills import load_user_skill, upsert_user_skill
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import make_tenant
from daimon.testing.ma import (
    FakeMAState,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    list_response,
    make_fake_ma_handler,
)
from daimon.testing.ma_models import ma_agent, ma_environment
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ADMIN = Subject(is_admin=True)
DEFAULT = DeploymentDefault(agent_name="daimon")
OWN = ChannelRule(readers="own", writers="own")
RULES = {"roamer": AgentRule(runs_in=("c6", "c7")), "homebody": AgentRule(runs_in=("c8",))}


def test_copy_name_slugs_the_channel_and_avoids_taken_names() -> None:
    assert copy_name("Team Alpha!", "123", taken=()) == "team-alpha"
    assert copy_name("Team Alpha", "123", taken={"team-alpha"}) == "team-alpha-2"
    assert copy_name(None, "C0ABCDEF12", taken=()) == "channel-cdef12", (
        "no label falls back to the channel id's tail"
    )
    assert copy_name(None, "19:a1b2c3d4e5f6@thread.tacv2", taken=()) == ("channel-d4e5f6"), (
        "a Teams id's tail comes from its id part, never its domain or the colon"
    )


def _client(tenant_id: uuid.UUID, *agents: tuple[str, bool]) -> tuple[AsyncAnthropic, FakeMAState]:
    state = FakeMAState()
    for index, (name, managed) in enumerate(agents):
        metadata = {MA_METADATA_KEY_MANAGED: "true"} if managed else {}
        agent = ma_agent(id=f"agent_{index}", name=name, tenant_id=tenant_id, metadata=metadata)
        state.agents[agent.id] = agent.model_dump(mode="json")
    return build_fake_anthropic(make_fake_ma_handler(state)), state


async def _bind(session: AsyncSession, tenant_id: uuid.UUID, channel_id: str, agent: str) -> None:
    await set_fields(
        session,
        scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id),
        tenant_id=tenant_id,
        agent_name=agent,
        mode="agent",
    )


async def _set_rule(
    client: AsyncAnthropic,
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    channel_id: str,
    *,
    readers: ChannelReaders | None = "own",
    **kwargs: Any,
) -> RuleChange:
    return await set_channel_rule(
        client,
        factory,
        tenant_id=tenant_id,
        platform="discord",
        channel_id=channel_id,
        readers=readers,
        default=DEFAULT,
        actor_account_id=None,
        subject=ADMIN,
        **kwargs,
    )


async def test_limiting_readers_warns_of_the_channels_own_open_environment(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A pick made before readers were limited skipped its network rule, so the change says so;
    never refuses."""
    tenant = await make_tenant(db_session)
    state = FakeMAState()
    agent = ma_agent(id="agent_local", name="local", tenant_id=tenant.id)
    state.agents[agent.id] = agent.model_dump(mode="json")
    environments = [ma_environment(id="env_open", name="open", tenant_id=tenant.id)]

    def list_environments(request: httpx.Request) -> httpx.Response:
        if request.method != "GET" or request.url.path != "/v1/environments":
            raise NotHandled
        return list_response([env.model_dump(mode="json") for env in environments])

    client = build_fake_anthropic(combine_handlers(list_environments, make_fake_ma_handler(state)))
    await _bind(db_session, tenant.id, "c1", "local")
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"),
        tenant_id=tenant.id,
        environment_name="open",
    )
    await db_session.commit()

    change = await _set_rule(client, db_session_factory, tenant.id, "c1")
    assert change.rule.readers == "own", "the warning never refuses"
    assert change.network_warning == LIMITED_OPEN_NETWORK_WARNING, (
        "its own open environment needs a server admin's confirmation"
    )


async def test_own_readers_keeps_the_channels_agent_there(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    client, _ = _client(
        tenant.id,
        ("local", False),
        ("shared", False),
        ("daimon", True),
        ("roamer", False),
        ("homebody", False),
    )
    await _bind(db_session, tenant.id, "c1", "local")
    await _bind(db_session, tenant.id, "c2", "shared")
    await _bind(db_session, tenant.id, "c3", "shared")
    await _bind(db_session, tenant.id, "c4", "daimon")
    await _bind(db_session, tenant.id, "c6", "roamer")
    await _bind(db_session, tenant.id, "c8", "homebody")
    await _bind(db_session, tenant.id, "c9", "homebody")
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_rules=RULES),
    )
    await db_session.commit()

    async def refusal(channel_id: str) -> str | None:
        try:
            await _set_rule(client, db_session_factory, tenant.id, channel_id)
        except ChannelRuleRefused as exc:
            return exc.reason
        return None

    assert await refusal("c5") == "no_channel_agent", "an unbound channel has no agent of its own"
    assert await refusal("c2") == "shared_channel_agent", "shared answers in c3 too"
    assert await refusal("c4") == "managed_channel_agent", "a built-in agent never belongs"
    assert await refusal("c6") == "agent_runs_elsewhere", "roamer's rule names c7 too"
    assert await refusal("c8") == "shared_ruled_agent", "homebody is bound in c9"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.channel_rules == {}, "a refusal writes nothing"
    assert await refusal("c1") is None, "c1's own agent answers only there"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.channel_rules == {"c1": OWN}
    assert policy.agent_rules["local"] == AgentRule(runs_in=("c1",)), (
        "the own agent's rule is set in the same write"
    )

    again = await _set_rule(client, db_session_factory, tenant.id, "c1")
    assert not again.changed, "repeating is a no-op"
    inside = await _set_rule(client, db_session_factory, tenant.id, "c1", readers="inside")
    assert inside.changed and inside.kept == ("local",), "the agent's rule stays unless asked"
    assert "still run only there" in " ".join(inside.notes), inside.notes
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.channel_rules == {"c1": ChannelRule(readers="inside")}
    assert "local" in policy.agent_rules, "the agent rule stays"
    await _set_rule(client, db_session_factory, tenant.id, "c1")
    released = await _set_rule(
        client, db_session_factory, tenant.id, "c1", readers="any", release_agents=True
    )
    assert released.released == ("local",), "asked, the channel's agents are released"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.channel_rules == {} and policy.agent_rules == RULES, (
        "only the channel's own agents lose their rule"
    )


async def test_own_readers_with_a_copy_keeps_the_copy_there(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    client, _ = _client(tenant.id, ("shared", False), ("team-alpha", False))
    await _bind(db_session, tenant.id, "c1", "shared")
    await _bind(db_session, tenant.id, "c2", "shared")
    await db_session.commit()

    with pytest.raises(ChannelRuleRefused) as refused:
        await _set_rule(client, db_session_factory, tenant.id, "c1")
    assert refused.value.reason == "shared_channel_agent", "no copy unless asked"
    change = await _set_rule(
        client, db_session_factory, tenant.id, "c1", channel_label="Team Alpha", copy=True
    )

    assert (change.agent_name, change.copied_from) == ("team-alpha-2", "shared"), (
        "copies the agent answering here, uniquely named"
    )
    scope = await get_scope(db_session, scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"))
    assert scope is not None and scope.agent_name == "team-alpha-2", "the copy answers in c1"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.channel_rules == {"c1": OWN}, "set in the same step"
    assert policy.agent_rules == {"team-alpha-2": AgentRule(runs_in=("c1",))}, (
        "the copy runs only there, not shared"
    )
    again = await _set_rule(client, db_session_factory, tenant.id, "c1", copy=True)
    assert (again.changed, again.copied_from) == (False, None), "repeating never copies twice"

    with pytest.raises(DaimonError, match="limiting where it runs"):
        await _set_rule(
            client, db_session_factory, tenant.id, "c3", copy=True, copy_from="team-alpha-2"
        )


@pytest.mark.parametrize("fork", [False, True], ids=["own-agent", "copy"])
async def test_own_readers_looks_agents_up_before_taking_the_policy_lock(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    fork: bool,
) -> None:
    """No agent lookup waits on the network while the policy lock is held: the
    channel's agent, or the fresh copy, is looked up first and reused under it."""
    tenant = await make_tenant(db_session)
    client, _ = _client(tenant.id, ("local", False), ("shared", False))
    await _bind(db_session, tenant.id, "c1", "shared" if fork else "local")
    await _bind(db_session, tenant.id, "c2", "shared")
    await db_session.commit()
    locked = False
    lookups_under_lock: list[str] = []
    real_lock = channel_rules.lock_access_policy
    real_find = channel_rules.find_agent_by_daimon_tag

    async def lock(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        nonlocal locked
        await real_lock(session, tenant_id=tenant_id)
        locked = True

    async def find(anthropic: AsyncAnthropic, *, tenant_id: uuid.UUID, name: str) -> Any:
        if locked:
            lookups_under_lock.append(name)
        return await real_find(anthropic, tenant_id=tenant_id, name=name)

    async def fork_unlocked(*args: Any, **kwargs: Any) -> AgentCopy:
        nonlocal locked
        locked = False  # the first transaction has ended
        return await fork_agent(*args, **kwargs)

    monkeypatch.setattr(channel_rules, "lock_access_policy", lock)
    monkeypatch.setattr(channel_rules, "find_agent_by_daimon_tag", find)
    monkeypatch.setattr(channel_rules, "fork_agent", fork_unlocked)
    change = await _set_rule(client, db_session_factory, tenant.id, "c1", copy=fork)

    assert change.rule == OWN and change.changed, "the channel is kept to its own agents"
    assert lookups_under_lock == [], "no lookup ran while the policy lock was held"


async def test_a_copy_refused_after_the_fork_is_archived(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copy starting to answer elsewhere while it is made refuses under the
    lock, and the copy no channel got is archived."""
    tenant = await make_tenant(db_session)
    state = FakeMAState()
    shared = ma_agent(id="agent_0", name="shared", tenant_id=tenant.id)
    state.agents[shared.id] = shared.model_dump(mode="json")
    archived: list[str] = []

    def archive(request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or not request.url.path.endswith("/archive"):
            raise NotHandled
        agent_id = request.url.path.split("/")[-2]
        archived.append(agent_id)
        return httpx.Response(200, json=state.agents.pop(agent_id))

    client = build_fake_anthropic(combine_handlers(archive, make_fake_ma_handler(state)))
    await _bind(db_session, tenant.id, "c1", "shared")
    await _bind(db_session, tenant.id, "c2", "shared")
    await db_session.commit()

    async def fork_then_route(*args: Any, **kwargs: Any) -> AgentCopy:
        copy = await fork_agent(*args, **kwargs)
        async with db_session_factory.begin() as session:
            await _bind(session, tenant.id, "c9", kwargs["new_name"])
        return copy

    monkeypatch.setattr(channel_rules, "fork_agent", fork_then_route)
    with pytest.raises(ChannelRuleRefused) as refused:
        await _set_rule(client, db_session_factory, tenant.id, "c1", copy=True)

    assert refused.value.reason == "shared_channel_agent"
    assert len(archived) == 1, "the copy no channel got is archived"
    scope = await get_scope(db_session, scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"))
    assert scope is not None and scope.agent_name == "shared", "the channel keeps its agent"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.channel_rules == {}


async def test_a_copy_whose_rule_write_fails_is_archived(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any failure after the fork, not only a refusal, archives the copy and re-raises."""
    tenant = await make_tenant(db_session)
    state = FakeMAState()
    shared = ma_agent(id="agent_0", name="shared", tenant_id=tenant.id)
    state.agents[shared.id] = shared.model_dump(mode="json")
    archived: list[str] = []

    def archive(request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or not request.url.path.endswith("/archive"):
            raise NotHandled
        agent_id = request.url.path.split("/")[-2]
        archived.append(agent_id)
        return httpx.Response(200, json=state.agents.pop(agent_id))

    client = build_fake_anthropic(combine_handlers(archive, make_fake_ma_handler(state)))
    await _bind(db_session, tenant.id, "c1", "shared")
    await _bind(db_session, tenant.id, "c2", "shared")
    await db_session.commit()

    async def broken_write(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("write failed")

    monkeypatch.setattr(channel_rules, "set_fields", broken_write)
    with pytest.raises(RuntimeError, match="write failed"):
        await _set_rule(client, db_session_factory, tenant.id, "c1", copy=True)

    assert len(archived) == 1 and archived[0] != shared.id, "the orphan copy is archived"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.channel_rules == {}, "no rule was set"


def _archive_recorder(
    state: FakeMAState, archived: list[str], *, status: int = 200
) -> AsyncAnthropic:
    def archive(request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or not request.url.path.endswith("/archive"):
            raise NotHandled
        agent_id = request.url.path.split("/")[-2]
        archived.append(agent_id)
        if status != 200:
            return httpx.Response(status, json={"type": "error", "error": {"type": "api_error"}})
        return httpx.Response(200, json=state.agents.pop(agent_id))

    return build_fake_anthropic(combine_handlers(archive, make_fake_ma_handler(state)))


async def _shared_in_two_channels(db_session: AsyncSession) -> tuple[uuid.UUID, FakeMAState]:
    tenant = await make_tenant(db_session)
    state = FakeMAState()
    shared = ma_agent(id="agent_0", name="shared", tenant_id=tenant.id)
    state.agents[shared.id] = shared.model_dump(mode="json")
    await _bind(db_session, tenant.id, "c1", "shared")
    await _bind(db_session, tenant.id, "c2", "shared")
    await db_session.commit()
    return tenant.id, state


class _LostCommit:
    """A sessionmaker whose transaction, once armed, commits and then reports a
    dropped connection: the ambiguous commit a caller cannot tell from a failure."""

    def __init__(self, inner: async_sessionmaker[AsyncSession]) -> None:
        self.inner = inner
        self.armed = False

    def __call__(self) -> AsyncSession:
        return self.inner()

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        async with self.inner.begin() as session:
            yield session
        if self.armed:
            self.armed = False
            raise OSError("connection lost after commit")


async def test_a_copy_the_channel_already_names_is_kept_when_its_commit_errors(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit the database applied before the error surfaced leaves the channel
    naming the copy: it is kept and logged, never archived under the channel."""
    tenant_id, state = await _shared_in_two_channels(db_session)
    archived: list[str] = []
    client = _archive_recorder(state, archived)
    sessionmaker = _LostCommit(db_session_factory)

    async def fork_then_arm(*args: Any, **kwargs: Any) -> AgentCopy:
        copy = await fork_agent(*args, **kwargs)
        sessionmaker.armed = True
        return copy

    monkeypatch.setattr(channel_rules, "fork_agent", fork_then_arm)
    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(OSError, match="connection lost after commit"),
    ):
        await _set_rule(client, cast(Any, sessionmaker), tenant_id, "c1", copy=True)

    assert archived == [], "the copy the channel names is never archived"
    kept = [log.get("reason") for log in logs if log["event"] == "channel_rules.copy_kept"]
    assert kept == ["channel_points_at_it"], logs
    policy = await load_access_policy(db_session, tenant_id=tenant_id)
    assert policy.channel_rules == {"c1": OWN}, "the commit did apply"


async def test_a_failed_archive_never_hides_the_write_error(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, state = await _shared_in_two_channels(db_session)
    archived: list[str] = []
    client = _archive_recorder(state, archived, status=500)

    async def broken_write(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("write failed")

    monkeypatch.setattr(channel_rules, "set_fields", broken_write)
    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(RuntimeError, match="write failed"),
    ):
        await _set_rule(client, db_session_factory, tenant_id, "c1", copy=True)

    assert archived, "the archive was tried"
    assert any(log["event"] == "channel_rules.copy_archive_failed" for log in logs), logs


async def test_fork_agent_copies_the_source_under_a_new_name(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    client, _ = _client(tenant.id, ("shared", False))

    async def fork(new_name: str) -> None:
        await fork_agent(
            client,
            db_session_factory,
            tenant_id=tenant.id,
            source_name="shared",
            new_name=new_name,
            public_url=None,
            subject=ADMIN,
            default_agent_name=None,
        )

    with pytest.raises(DaimonError, match="Only a workspace or server admin"):
        await fork_agent(
            client,
            db_session_factory,
            tenant_id=tenant.id,
            source_name="shared",
            new_name="nope",
            public_url=None,
            subject=Subject(),
            default_agent_name=None,
        )
    await fork("team-alpha")
    agents = await list_agents_by_tenant(client, tenant_id=tenant.id)
    names = sorted(agent.metadata[MA_METADATA_KEY_NAME] for agent in agents)
    assert names == ["shared", "team-alpha"], "the copy is tagged with its new name"
    with pytest.raises(DaimonError, match="already exists"):
        await fork("team-alpha")
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_rules={"shared": AgentRule(runs_in=("C1",))}),
    )
    await db_session.commit()
    with pytest.raises(DaimonError, match="limiting where it runs"):
        await fork("team-beta")


async def test_fork_agent_leaves_off_credentialed_servers_and_copies_its_own_skills(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The copy a channel gets holds no token, gets the source's own
    uploaded skills as new skills of its own, reaches into no other agent's
    skills, and names every skill it left off, including one that failed to copy."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    source = ma_agent(
        id="agent_src",
        name="shared",
        tenant_id=tenant.id,
        mcp_servers=[
            {"type": "url", "name": "crm", "url": "https://crm.example.com/mcp"},
            {"type": "url", "name": "docs", "url": "https://docs.example.com/mcp"},
        ],
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": name,
                "default_config": {
                    "enabled": True,
                    "permission_policy": {"type": "always_allow"},
                },
                "configs": [],
            }
            for name in ("crm", "docs")
        ],
        skills=[
            {"type": "custom", "skill_id": skill_id, "version": "1"}
            for skill_id in ("skill_own", "skill_broken", "skill_other", "skill_library")
        ],
    )
    state = FakeMAState()
    state.agents[source.id] = source.model_dump(mode="json")

    def skill_row(skill_id: str, title: str) -> dict[str, Any]:
        return SkillListResponse(
            id=skill_id,
            type="custom",
            display_title=title,
            latest_version="1",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            source="custom",
        ).model_dump(mode="json")

    skills = [
        skill_row(skill_id, tenant_scoped_display_title(tenant_id=tenant.id, name=body))
        for skill_id, body in (
            ("skill_own", "shared/notes"),
            ("skill_broken", "shared/broken"),
            ("skill_other", "other/tips"),
            ("skill_library", "notes-lib"),
        )
    ]
    notes = bundle_from_markdown("---\nname: notes\ndescription: Take notes.\n---\nWrite.\n")
    downloads: list[str] = []

    def skills_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/skills":
            return list_response(skills)
        if request.method == "POST" and path == "/v1/skills":
            found = re.search(rb'name="display_title"\r\n\r\n([^\r]+)', request.content)
            assert found is not None, "skills.create must send a display_title"
            created = skill_row(f"sk_new_{len(skills)}", found.group(1).decode())
            skills.append(created)
            return httpx.Response(200, json=created)
        if request.method == "GET" and path.endswith("/versions"):
            return list_response([{"id": f"skill_version_{path.split('/')[3]}", "version": "1"}])
        if request.method == "GET" and path.endswith("/content"):
            if "skills-2025-10-02" in request.headers.get("anthropic-beta", ""):
                return httpx.Response(
                    403,
                    json={
                        "type": "error",
                        "error": {"type": "permission_error", "message": "workspace API key"},
                    },
                )
            assert "/versions/skill_version_" in path, "a download names the version's id"
            downloads.append(path)
            if "skill_own" in path:
                return httpx.Response(200, content=notes.zip_bytes)
            return httpx.Response(
                404, json={"type": "error", "error": {"type": "not_found_error", "message": "x"}}
            )
        raise NotHandled

    client = build_fake_anthropic(combine_handlers(skills_handler, make_fake_ma_handler(state)))
    await save_agent_mcp_credential(
        sessionmaker=db_session_factory,
        fernet=make_fernet(),
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_src"),
        mcp_server_url="https://crm.example.com/mcp",
        plaintext_token="tok",
    )
    await upsert_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_src"),
        agent_name="shared",
        name="notes",
        source_repo_url="",
        source_repo_branch="",
        source_path="",
        content_hash=notes.preview.content_hash,
        anthropic_id="skill_own",
        anthropic_latest_version="1",
        source="upload",
        origin="pasted",
    )
    await db_session.commit()

    copy = await fork_agent(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        source_name="shared",
        new_name="team-alpha",
        public_url=None,
        subject=ADMIN,
        default_agent_name=None,
    )

    assert [server.name for server in copy.agent.mcp_servers] == ["docs"], (
        "a server backed by the source's stored token is left off the copy"
    )
    new_id = skills[-1]["id"]
    assert skills[-1]["display_title"] == tenant_scoped_display_title(
        tenant_id=tenant.id, name="notes", agent_name="team-alpha"
    ), "the source's own skill is uploaded again under the copy's name"
    assert {skill.skill_id for skill in copy.agent.skills} == {"skill_library", new_id}, (
        "the copy keeps library skills and gets a new id for its own; it never shares skill_own"
    )
    assert copy.copied_skills == ("team-alpha/notes",), copy.copied_skills
    assert copy.dropped_skills == ("other/tips", "shared/broken"), (
        "another agent's skill and the one that failed to download are named, not copied"
    )
    assert len(downloads) == 2, "only the source's own skills are downloaded"
    fork_id = copy.agent.id
    row = await load_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=fork_id),
        agent_name="team-alpha",
        name="notes",
    )
    assert row is not None and row.source == "upload" and row.anthropic_id == new_id, (
        "the copy's upload row names the copy, so listing filters see it as its own"
    )
    assert row.origin == "pasted", "the copy keeps where the skill came from"


async def test_fork_agent_returns_the_copy_when_its_final_reread_fails(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The copy exists once created: a failed re-read after its skills are copied
    returns it with what was copied instead of raising and orphaning it."""
    tenant = await make_tenant(db_session)
    source = ma_agent(
        id="agent_src",
        name="shared",
        tenant_id=tenant.id,
        skills=[{"type": "custom", "skill_id": "skill_own", "version": "1"}],
    )
    state = FakeMAState()
    state.agents[source.id] = source.model_dump(mode="json")
    notes = bundle_from_markdown("---\nname: notes\ndescription: Take notes.\n---\nWrite.\n")
    skills = [
        SkillListResponse(
            id="skill_own",
            type="custom",
            display_title=tenant_scoped_display_title(tenant_id=tenant.id, name="shared/notes"),
            latest_version="1",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            source="custom",
        ).model_dump(mode="json")
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "GET" and path == "/v1/skills":
            return list_response(skills)
        if method == "POST" and path == "/v1/skills":
            created = {**skills[0], "id": "sk_new", "display_title": "copy"}
            skills.append(created)
            return httpx.Response(200, json=created)
        if method == "GET" and path.endswith("/versions"):
            return list_response([{"id": "skill_version_own", "version": "1"}])
        if method == "GET" and path.endswith("/content"):
            return httpx.Response(200, content=notes.zip_bytes)
        copy = next((a for i, a in state.agents.items() if i != "agent_src"), None)
        attached = copy is not None and any(
            skill["skill_id"] == "sk_new" for skill in copy.get("skills") or []
        )
        if method == "GET" and copy is not None and path == f"/v1/agents/{copy['id']}" and attached:
            return httpx.Response(
                404, json={"type": "error", "error": {"type": "not_found_error", "message": "x"}}
            )
        raise NotHandled

    await upsert_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_src"),
        agent_name="shared",
        name="notes",
        source_repo_url="",
        source_repo_branch="",
        source_path="",
        content_hash=notes.preview.content_hash,
        anthropic_id="skill_own",
        anthropic_latest_version="1",
        source="upload",
        origin="pasted",
    )
    await db_session.commit()
    client = build_fake_anthropic(combine_handlers(handler, make_fake_ma_handler(state)))

    copy = await fork_agent(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        source_name="shared",
        new_name="team-alpha",
        public_url=None,
        subject=ADMIN,
        default_agent_name=None,
    )

    assert copy.agent.id in state.agents, "the copy that exists is the one returned"
    assert copy.copied_skills == ("team-alpha/notes",), "the copied skill is still reported"


def test_an_agent_with_a_rule_is_never_offered_as_a_copy() -> None:
    """An agent with a rule can't be copied (`authorize(FORK)`), so no refusal offers one."""
    for reason in ("agent_runs_elsewhere", "shared_ruled_agent"):
        text = render_rule_refusal(reason, agent_name="roamer")
        assert "Ask for a copy" not in text, text


async def test_writers_alone_keeps_readers_and_follows_them_to_own(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    client, _ = _client(tenant.id, ("local", False))
    await _bind(db_session, tenant.id, "c1", "local")
    await db_session.commit()

    closed = await _set_rule(
        client, db_session_factory, tenant.id, "c1", readers=None, writers="none"
    )
    assert closed.rule == ChannelRule(writers="none"), "readers stay any"
    own = await _set_rule(client, db_session_factory, tenant.id, "c1", readers=None, writers="own")
    assert own.rule == OWN and own.agent_name == "local", "writers own takes readers own"
    with pytest.raises(ChannelRuleRefused) as refused:
        await _set_rule(
            client, db_session_factory, tenant.id, "c1", readers="inside", writers="own"
        )
    assert refused.value.reason == "own_on_both"


async def test_only_admins_set_rules_and_a_slack_thread_takes_readers_inside_only(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    client, _ = _client(tenant.id)
    with pytest.raises(ChannelRuleRefused) as member:
        await set_channel_rule(
            client,
            db_session_factory,
            tenant_id=tenant.id,
            platform="slack",
            channel_id="C1",
            readers="inside",
            subject=Subject(platform_user_id="u1"),
            default=DEFAULT,
        )
    assert member.value.reason == "admin_required"
    with pytest.raises(ChannelRuleRefused) as thread:
        await _set_rule(
            client, db_session_factory, tenant.id, "C01AB:1700000000.000200", writers="none"
        )
    assert thread.value.reason == "invalid"
    inside = await _set_rule(
        client, db_session_factory, tenant.id, "C01AB:1700000000.000200", readers="inside"
    )
    assert inside.changed


def test_a_thread_rule_names_a_slack_or_discord_thread() -> None:
    assert thread_rule_key("slack", " C01AB:1700000000.000200 ") == "C01AB:1700000000.000200"
    assert thread_rule_key("discord", "123456789012345678") == "123456789012345678"
    for platform, raw in (
        ("slack", "C01AB"),
        ("slack", "C01AB:x"),
        ("discord", "abc"),
        ("teams", "19:a@thread.tacv2;messageid=1"),
    ):
        with pytest.raises(RuleRefused):
            thread_rule_key(platform, raw)


async def test_a_category_takes_writers_none_only(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    with pytest.raises(ChannelRuleRefused) as own:
        await set_category_rule(
            db_session_factory, tenant_id=tenant.id, category_id="900", writers="own", subject=ADMIN
        )
    assert own.value.reason == "invalid"


async def _agent_rule(
    client: AsyncAnthropic,
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    name: str,
    runs_in: tuple[str, ...] | None,
) -> tuple[str, ...] | None:
    change = await set_agent_rule(
        client,
        factory,
        tenant_id=tenant_id,
        platform="discord",
        agent_name=name,
        runs_in=runs_in,
        subject=ADMIN,
        default=DEFAULT,
    )
    return change.runs_in


async def test_set_agent_rule_limits_where_an_agent_runs(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    client, _ = _client(tenant.id, ("local", False), ("shared", False))
    await _bind(db_session, tenant.id, "c1", "local")
    await _bind(db_session, tenant.id, "c2", "shared")
    await _bind(db_session, tenant.id, "c3", "shared")
    await db_session.commit()

    with pytest.raises(ChannelRuleRefused) as missing:
        await _agent_rule(client, db_session_factory, tenant.id, "ghost", ("c1",))
    assert missing.value.reason == "agent_not_found"
    change = await set_agent_rule(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        agent_name="shared",
        runs_in=("c2",),
        subject=ADMIN,
        default=DEFAULT,
    )
    assert change.answers_outside, "shared is still c3's default"
    assert "still set to answer outside" in " ".join(change.notes)
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.agent_rules == {"shared": AgentRule(runs_in=("c2",))}
    assert await _agent_rule(client, db_session_factory, tenant.id, "shared", None) is None
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.agent_rules == {}, "None clears the rule"


async def test_an_agent_rule_naming_an_own_readers_channel_names_it_alone(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    client, _ = _client(tenant.id, ("local", False), ("helper", False), ("shared", False))
    await _bind(db_session, tenant.id, "c1", "local")
    await _bind(db_session, tenant.id, "c2", "shared")
    policy = TenantAccessPolicy(
        channel_rules={"c1": OWN}, agent_rules={"local": AgentRule(runs_in=("c1",))}
    )
    # Set directly: the test factory shares one connection, so a refusal's
    # rollback undoes writes since the last commit here.
    await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    await db_session.commit()

    with pytest.raises(ChannelRuleRefused) as kept:
        await _set_rule(client, db_session_factory, tenant.id, "c1", release_agents=True)
    assert kept.value.reason == "keeps_own_agents", "own readers keep their agents"
    with pytest.raises(ChannelRuleRefused) as alone:
        await _agent_rule(client, db_session_factory, tenant.id, "helper", ("c1", "c2"))
    assert alone.value.reason == "own_channel_alone"
    with pytest.raises(ChannelRuleRefused) as shared:
        await _agent_rule(client, db_session_factory, tenant.id, "shared", ("c1",))
    assert shared.value.reason == "shared_channel_agent", "shared still answers in c2"
    assert await _agent_rule(client, db_session_factory, tenant.id, "helper", ("c1",)) == ("c1",)
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert runs_only_in(policy, "c1") == ("helper", "local"), "helper is c1's own agent too"
    with pytest.raises(ChannelRuleRefused) as home:
        await _agent_rule(client, db_session_factory, tenant.id, "local", None)
    assert home.value.reason == "agent_has_home", "released only through the channel's readers"
