"""The agent's keys file on an OpenAI or Gemini reply (`provider_env`)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.core.credential_env import assemble_env_bytes
from daimon.core.features import snapshot
from daimon.core.self_helpers import self_helper_thread_key
from daimon.core.session_compat import ReplaceSession
from daimon.core.session_snapshot import hash_env_bytes
from daimon.core.stores.domain import AgentFileRow
from daimon.core.turn import provider_env
from daimon.core.turn.replace_binding import BindingFacts, binding_decision
from daimon.core.turn_keys import answering_env_sha256

TENANT, AGENT = uuid.uuid4(), uuid.uuid4()


def row(key: str, content: str) -> AgentFileRow:
    now = datetime.now(UTC)
    return AgentFileRow(
        tenant_id=TENANT, agent_id=AGENT, key=key, content=content, created_at=now, updated_at=now
    )


class Sessions:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


@pytest.mark.asyncio
async def test_the_file_is_claudes_bytes_and_hash_without_github_tokens(monkeypatch) -> None:
    rows = [row("API_KEY", "s3cret"), row("GH_TOKEN", "gh"), row("GITHUB_TOKEN", "gh")]

    async def listed(_session, *, tenant_id, agent_id):
        assert (tenant_id, agent_id) == (TENANT, AGENT)
        return rows

    monkeypatch.setattr(provider_env, "list_agent_files", listed)
    env = await provider_env.agent_env_file(cast(Any, Sessions), tenant_id=TENANT, agent_id=AGENT)
    assert env is not None and env.body == assemble_env_bytes(rows[:1])
    assert env.sha256 == hash_env_bytes(env.body)
    assert "s3cret" not in repr(env), "the value never reaches a log line"
    rows[:] = rows[1:]
    assert (
        await provider_env.agent_env_file(cast(Any, Sessions), tenant_id=TENANT, agent_id=AGENT)
        is None
    )


def request(*, env_file: bool = True, thread: str = "th") -> Any:
    return SimpleNamespace(
        admission=SimpleNamespace(toolset=SimpleNamespace(env_file=env_file)),
        thread_id=thread,
        deps=SimpleNamespace(settings=object()),
        answers_on=("discord",),
        platform="discord",
    )


@pytest.mark.parametrize(
    ("case", "carried"),
    [("on", True), ("env_file_off", False), ("self_helper", False), ("feature_off", False)],
)
def test_keys_go_where_claude_mounts_them_unless_an_admin_turns_them_off(
    monkeypatch, case, carried
) -> None:
    on = case != "feature_off"
    monkeypatch.setattr(snapshot, "deployment_on_everywhere", lambda _s, key, _p: on)
    built = request(
        env_file=case != "env_file_off",
        thread=self_helper_thread_key("th", "a") if case == "self_helper" else "th",
    )
    assert provider_env.carries_keys(built) is carried


def test_a_provider_session_names_keys_by_the_file_it_holds() -> None:
    live = cast(Any, SimpleNamespace(effective_config=SimpleNamespace(env_sha256="claude")))
    claude = cast(Any, SimpleNamespace(session_ref=None, env_sha256=None))
    provider = cast(Any, SimpleNamespace(session_ref=object(), env_sha256="openai"))
    assert answering_env_sha256(claude, live) == "claude"
    assert answering_env_sha256(claude, None) is None
    assert answering_env_sha256(provider, live) == "openai"


def test_a_changed_keys_file_replaces_an_openai_session() -> None:
    facts = BindingFacts(
        profile="openai.persistent_workspace",
        provider="openai",
        config_revision=1,
        agent="a",
        account_scope="s",
        model="m",
        plan_digest="p",
        env_sha256="old",
    )
    assert not isinstance(binding_decision(facts, facts), ReplaceSession)
    changed = binding_decision(facts, replace(facts, env_sha256="new"))
    assert isinstance(changed, ReplaceSession)


@pytest.mark.asyncio
def test_the_guidance_names_where_the_provider_file_is() -> None:
    from daimon.core.agent_guidance import CREDENTIAL_GUIDANCE_BLOCK

    assert (
        f"On OpenAI or Gemini the same file is at {provider_env.PATH}" in CREDENTIAL_GUIDANCE_BLOCK
    )
