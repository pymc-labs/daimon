"""A feature only a sign-in flow may turn on is refused on every other path that writes features.

`github_follow_org` hands an agent every repo of a GitHub org, so only the connect page, which
proves the signer is an admin of the org, may turn it on. Chat, the agent's tools, the web
app, the settings panel, an agent's spec and the web and MCP surfaces all write through
`change_feature` or `AgentSpec`; the operator CLI may set it and is trusted.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from typing import get_args

import pytest
from daimon.core.admin_actions import DelegatedAction
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.features.registry import DEFAULT, FEATURES
from daimon.core.features.service import CONNECT_ONLY, FeatureActor, change_feature
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.specs import AgentSpec
from daimon.core.stores.domain import ChangeSurface
from daimon.testing.factories import make_tenant
from daimon.testing.shared_helpers import settings as _settings
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

KEY = "github_follow_org"
_ROOT = Path(__file__).resolve().parents[4]

#: Every file that calls `set_agent_feature`, which takes the admin's word for it. Each has
#: proved the person itself before the call.
_MAY_SET_AGENT_FEATURE = {"adapters/mcp/daimon/adapters/mcp/oauth_github.py"}


def _actor(tenant_id: uuid.UUID, surface: ChangeSurface, *, operator: bool = False) -> FeatureActor:
    async def admin_for(_: DelegatedAction | None) -> bool:
        return True

    return FeatureActor(tenant_id, None, surface, None, admin_for, operator=operator)


def test_the_connect_only_features_are_listed() -> None:
    assert {key for key, spec in FEATURES.items() if spec.connect_only} == {KEY}


@pytest.mark.parametrize("surface", get_args(ChangeSurface))
async def test_no_surface_but_the_operator_cli_turns_it_on(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    surface: ChangeSurface,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    settings = _settings()
    place = FeaturePlace(tenant.id, "discord", agent_id="agent_1")

    with pytest.raises(ChannelChangeRefused) as refused:
        await change_feature(
            db_session_factory,
            settings,
            _actor(tenant.id, surface),
            key=KEY,
            tier="agent",
            value="on",
            place=place,
        )
    assert refused.value.reason == "connect_only"
    assert str(refused.value) == CONNECT_ONLY
    assert "connect page" in CONNECT_ONLY, "the refusal says where to go"
    assert not (await feature_in(db_session, settings, place, KEY)).on

    await change_feature(
        db_session_factory,
        settings,
        _actor(tenant.id, "cli", operator=True),
        key=KEY,
        tier="agent",
        value="on",
        place=place,
    )
    assert (await feature_in(db_session, settings, place, KEY)).on

    for value in ("off", DEFAULT):  # turning it off stays open to anyone who may change it
        await change_feature(
            db_session_factory,
            settings,
            _actor(tenant.id, surface),
            key=KEY,
            tier="agent",
            value=value,
            place=place,
        )
        assert not (await feature_in(db_session, settings, place, KEY)).on


def test_an_agent_spec_cannot_turn_it_on() -> None:
    base = {"name": "a", "model": "claude-sonnet-4-5"}
    with pytest.raises(ValidationError, match="connect page"):
        AgentSpec.model_validate({**base, "features": {KEY: "on"}})
    assert AgentSpec.model_validate({**base, "features": {KEY: "off"}}).features == {KEY: "off"}


def test_only_the_connect_page_writes_an_agent_feature_on_its_own_word() -> None:
    callers = {
        path.relative_to(_ROOT / "packages").as_posix()
        for base in (
            _ROOT / "packages/core/daimon",
            *(_ROOT / "packages/adapters").glob("*/daimon"),
        )
        for path in base.rglob("*.py")
        if any(
            isinstance(node, ast.Call)
            and (getattr(node.func, "id", None) or getattr(node.func, "attr", ""))
            == "set_agent_feature"
            for node in ast.walk(ast.parse(path.read_text()))
        )
    }
    assert callers == _MAY_SET_AGENT_FEATURE
