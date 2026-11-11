"""Converting a Managed Agents agent to a Daimon record: copy, then delete the MA twin."""

import ast
import inspect
import re

import httpx
import pytest
from daimon.core import agent_convert, agent_lifecycle
from daimon.core.access_policy import AgentRule, TenantAccessPolicy
from daimon.core.agent_convert import convert_agent_to_daimon
from daimon.core.authz import Subject
from daimon.core.defaults.metadata import MA_METADATA_KEY_CONVERTED_FROM
from daimon.core.errors import UserFacingError
from daimon.core.mux_backend import resource_scope
from daimon.core.provider_keys import DEFAULT_KEY_ENV
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.selfhosted_resources import SelfHostedAgents
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture(autouse=True)
def openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DEFAULT_KEY_ENV.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(DEFAULT_KEY_ENV["openai"], "test-key")


async def test_convert_keeps_rules_and_archives_the_twin_raw(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record takes the name and rules; the MA twin is archived, never forgotten."""
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_rules={"analyst": AgentRule(invoker_role_ids=("111",))}),
    )
    await db_session.commit()
    source = ma_agent(id="agent_ma1", name="analyst", tenant_id=tenant.id, system="Analyse.")
    archived: list[str] = []

    def archive(_req: httpx.Request, match: re.Match[str]) -> httpx.Response:
        archived.append(match.group(1))
        body = source.model_dump(mode="json") | {"archived_at": "2026-11-10T00:00:00Z"}
        return httpx.Response(200, json=body)

    async def forget(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        raise AssertionError("convert must not run lifecycle archive")

    monkeypatch.setattr(agent_lifecycle, "forget_archived_agent", forget)
    router = MARouter()
    router.add_agent_list(source)
    router.add("POST", r"/v1/agents/([^/]+)/archive", archive)
    converted = await convert_agent_to_daimon(
        build_fake_anthropic(router.dispatch),
        db_session_factory,
        tenant_id=tenant.id,
        name="analyst",
        subject=Subject(is_admin=True),
    )

    assert converted.agent.id.startswith("dm_agent_"), "the copy is a Daimon record"
    record = await SelfHostedAgents(db_session_factory).resolve(
        resource_scope(tenant_id=str(tenant.id)), "analyst"
    )
    assert record.spec.model.provider == "openai", "the record runs on the keyed provider"
    assert record.spec.system == "Analyse.", "the definition carries over"
    assert record.spec.metadata.get(MA_METADATA_KEY_CONVERTED_FROM) == "agent_ma1", (
        "the record names its old id, so tokens signed for it keep working"
    )
    assert archived == ["agent_ma1"], f"the MA twin is archived once: {archived}"
    db_session.expire_all()
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert "analyst" in policy.agent_rules, "the name's rule survives the conversion"


async def test_convert_is_admin_only(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A non-admin is refused before any provider call."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    with pytest.raises(UserFacingError, match="admin"):
        await convert_agent_to_daimon(
            build_fake_anthropic(MARouter().dispatch),
            db_session_factory,
            tenant_id=tenant.id,
            name="analyst",
            subject=Subject(is_admin=False),
        )


def test_convert_never_reaches_lifecycle_archive() -> None:
    """Contract: convert deletes through the raw port, never the name-keyed lifecycle path."""
    tree = ast.parse(inspect.getsource(agent_convert))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    banned = {"forget_archived_agent", "archive_agent", "archive_agent_everywhere"}
    assert not names & banned, f"convert must not call lifecycle archive: {names & banned}"
