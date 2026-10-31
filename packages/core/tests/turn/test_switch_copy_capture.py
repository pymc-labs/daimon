"""A copy made in a conversation for its switch reads as that conversation's responder
only while it awaits the switch: made here, of this responder, on its provider (J1)."""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.core.defaults.metadata import forked_in
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import ScopeContext
from daimon.core.turn import admission

TENANT = uuid.uuid4()
HERE = ScopeContext(
    tenant_id=TENANT,
    account_id=uuid.uuid4(),
    platform="discord",
    channel_id="channel",
    thread_id="thread",
)
COPY = derive_agent_uuid(tenant_id=TENANT, ma_agent_id="agent_copy")
ROOT = derive_agent_uuid(tenant_id=TENANT, ma_agent_id="agent_root")


def _copy(**stamp: str) -> Any:
    metadata = {
        "daimon_forked_from": "agent_root",
        "daimon_forked_in": forked_in("discord", "channel", "thread"),
        **stamp,
    }
    return SimpleNamespace(
        agent_id=COPY, source=SimpleNamespace(spec=SimpleNamespace(metadata=metadata))
    )


@pytest.fixture
def providers(monkeypatch):
    backends: dict[uuid.UUID, str | None] = {COPY: None, ROOT: None}

    async def current(_db, tenant_id, agent_id):
        assert tenant_id == TENANT
        backend = backends[agent_id]
        return None if backend is None else SimpleNamespace(backend=backend)

    monkeypatch.setattr(admission.agent_backends, "current", current)
    return backends


async def _awaits(source: Any, responder: str | None = "agent_root") -> bool:
    @contextlib.asynccontextmanager
    async def session():
        yield None

    deps = cast(Any, SimpleNamespace(sessionmaker=session))
    return await admission._awaits_its_switch_here(  # pyright: ignore[reportPrivateUsage]
        deps, source, HERE, responder_ma_agent_id=responder
    )


async def test_a_copy_made_here_awaits_its_switch(providers):
    assert await _awaits(_copy())


async def test_a_copy_already_on_another_provider_does_not(providers):
    providers[COPY] = "gemini"
    assert not await _awaits(_copy())


async def test_both_on_one_other_provider_still_awaits(providers):
    providers[COPY] = providers[ROOT] = "openai"
    assert await _awaits(_copy())


@pytest.mark.parametrize(
    "stamp",
    (
        {"daimon_forked_in": forked_in("discord", "channel", "other-thread")},
        {"daimon_forked_from": "agent_other"},
    ),
)
async def test_a_copy_made_elsewhere_or_of_another_agent_does_not(providers, stamp):
    assert not await _awaits(_copy(**stamp))


async def test_an_unknown_responder_never_matches(providers):
    assert not await _awaits(_copy(), responder=None)


async def test_the_cards_capture_names_its_turns_responder(monkeypatch):
    """The card's check reads the copy as the MCP preview did: its capture names the
    admitted turn's responder, the one a place's config may not pin yet."""
    seen: dict[str, object] = {}

    async def capture(_deps, _source, **kwargs):
        seen.update(kwargs)
        return _source

    monkeypatch.setattr(admission, "capture_backend_source", capture)
    account = uuid.uuid4()
    admitted = SimpleNamespace(
        account_id=account,
        agent=SimpleNamespace(id="agent_root"),
        grant=SimpleNamespace(is_dm=False, subject=object(), run_place=object()),
        private_dm_id=None,
        slack_turn_context_id=None,
        origin_channel_id="channel",
        origin_thread_id="thread",
    )
    await admission.capture_admitted_backend_source(
        cast(Any, None),
        _copy(),
        admission=cast(Any, admitted),
        tenant_id=TENANT,
        account_id=account,
        platform="discord",
    )
    assert seen["responder_ma_agent_id"] == "agent_root"
