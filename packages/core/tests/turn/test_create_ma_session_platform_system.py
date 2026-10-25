"""Ordinary agent operation and configuration paths, using offline providers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from daimon.core.agent_guidance import apply_credential_guidance
from daimon.core.session_snapshot import hash_system
from daimon.core.turn.prepare import create_ma_session
from daimon.testing.ma_models import ma_agent, ma_environment
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_create_ma_session_cleanup import _Upstream  # pyright: ignore[reportPrivateUsage]
from .test_run_prepared_turn import _admission  # pyright: ignore[reportPrivateUsage]

from daimon.testing.factories import make_account, make_tenant  # isort: skip

_DEFAULTS = Path(__file__).resolve().parents[4] / "defaults"
_WORKER_EXTRA = "WORKER BRIEF: only edit the docs folder, and report each file you touch."


def _worker_system() -> str:
    spec = yaml.safe_load((_DEFAULTS / "agents" / "daimon.yaml").read_text())
    return apply_credential_guidance(spec["system"]) + "\n\n" + _WORKER_EXTRA


@pytest.mark.parametrize(
    ("platform", "others"),
    [("teams", "discord|slack"), ("discord", "slack|teams"), ("slack", "discord|teams")],
)
async def test_a_workers_session_gets_its_platforms_variant_of_the_workers_own_system(
    platform: str,
    others: str,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    upstream = _Upstream()
    worker = ma_agent(id="ag_worker", tenant_id=tenant.id, system=_worker_system())
    admission = _admission(
        account_id=account.id, agent=worker, env=ma_environment(id="env_1", tenant_id=tenant.id)
    )

    created = await create_ma_session(
        upstream.deps(db_session_factory), admission, tenant_id=tenant.id, platform=platform
    )

    (body,) = upstream.session_bodies
    agent_argument = cast("dict[str, Any]", body["agent"])
    sent = agent_argument["system"]
    assert isinstance(sent, str)
    assert _WORKER_EXTRA in sent, "the worker's extra instructions are kept"
    assert re.search(others, sent, re.IGNORECASE) is None
    assert sent != worker.system
    assert created.snapshot.system_sha256 == hash_system(sent), (
        "the recorded snapshot hashes what was sent, so the next turn finds no drift"
    )


async def test_a_session_with_no_chat_platform_keeps_the_agents_own_system(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    upstream = _Upstream()
    agent = ma_agent(id="ag_1", tenant_id=tenant.id, system=_worker_system())
    admission = _admission(
        account_id=account.id, agent=agent, env=ma_environment(id="env_1", tenant_id=tenant.id)
    )

    await create_ma_session(
        upstream.deps(db_session_factory), admission, tenant_id=tenant.id, platform="cli"
    )

    (body,) = upstream.session_bodies
    assert body["agent"] == "ag_1", "a bare agent id: no system override is sent"


def test_the_variant_is_derived_at_creation_not_carried_on_the_admission() -> None:
    """A persisted root carries no system text: it would go stale, and a root retained
    before the variant existed would lack the key."""
    from daimon.core.turn.admission import Admission

    assert "system_override" not in Admission.__dataclass_fields__
