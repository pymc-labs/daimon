"""The Managed Agents target: repins, no duplicate publish, managed agents."""

from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.core.defaults.metadata import MA_METADATA_KEY_TENANT
from daimon.core.server_skills import anthropic as target_module
from daimon.core.server_skills.anthropic import ManagedAgentsTarget
from daimon.core.server_skills.gate import manifest_digest
from daimon.core.skills.ingest import bundle_from_files
from daimon.testing.mcp_skills import skill_md

FILES = {"SKILL.md": skill_md("notes")}
DIGEST = manifest_digest({p: hashlib.sha256(b).hexdigest() for p, b in FILES.items()})
MIRROR: Any = SimpleNamespace(
    id=uuid.uuid4(), tenant_id=uuid.uuid4(), skill_id="skill_1", digest=DIGEST
)


async def test_a_repin_sends_only_the_new_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repin, on any provider the agent runs on, rewrites only that skill's pin."""
    sent: list[Any] = []
    held = SimpleNamespace(type="custom", skill_id="skill_1", version="1")
    fresh = SimpleNamespace(
        id="agent",
        version=3,
        archived_at=None,
        metadata={"daimon_account": "a", MA_METADATA_KEY_TENANT: str(MIRROR.tenant_id)},
        skills=[held],
    )

    async def retry(_client: object, _agent_id: str, apply: Any, **_k: object) -> Any:
        return await apply(fresh)

    async def update(_client: object, _agent_id: str, **kwargs: Any) -> Any:
        sent.append(kwargs["payload"]["skills"])
        return fresh

    monkeypatch.setattr(target_module, "update_agent_with_version_retry", retry)
    monkeypatch.setattr(target_module, "update_agent", update)
    await ManagedAgentsTarget(client=Any).repin(MIRROR, "agent", "2")  # pyright: ignore[reportArgumentType]
    assert sent == [[{"type": "custom", "skill_id": "skill_1", "version": "2"}]], (
        "the skill moves to the new version and nothing else changes"
    )


def _versions(*listed: tuple[str, str]) -> Any:
    async def list_versions(*_a: object, **_k: object) -> Any:
        for version, created in listed:
            yield SimpleNamespace(version=version, created_at=created)

    return list_versions


@pytest.mark.parametrize("stale", [False, True])
async def test_a_lost_publish_is_reused_not_repeated(
    monkeypatch: pytest.MonkeyPatch, stale: bool
) -> None:
    """The newest version already holding the files is the publish a crash lost."""
    sent: list[str] = []
    held = {**FILES, "old.md": b"Old.\n"} if stale else FILES
    zipped = bundle_from_files(held).zip_bytes

    async def download(*_a: object, **_k: object) -> bytes:
        return zipped

    async def publish(*_a: object, **_k: object) -> Any:
        sent.append("new")
        return SimpleNamespace(version="9")

    monkeypatch.setattr(target_module, "list_skill_versions", _versions(("3", "a"), ("4", "b")))
    monkeypatch.setattr(target_module, "download_skill_version", download)
    monkeypatch.setattr(target_module, "publish_skill_version", publish)
    version = await ManagedAgentsTarget(client=Any).publish(  # pyright: ignore[reportArgumentType]
        MIRROR, bundle_from_files(FILES)
    )
    assert sent == (["new"] if stale else []), "published only when nothing holds the files"
    assert version == ("9" if stale else "4"), "the newest version is reused"


async def test_agents_managed_by_settings_are_never_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = SimpleNamespace(type="custom", skill_id="skill_1", version="1")

    def agent(agent_id: str, metadata: dict[str, str]) -> SimpleNamespace:
        return SimpleNamespace(id=agent_id, name=agent_id, metadata=metadata, skills=[skill])

    async def agents(*_a: object, **_k: object) -> list[SimpleNamespace]:
        return [
            agent("owned", {"daimon_account": "a"}),
            agent("seeded", {"daimon_account": "a", "daimon_managed": "true"}),
            agent("system", {}),
        ]

    monkeypatch.setattr(target_module, "list_agents_by_tenant", agents)
    attached = await ManagedAgentsTarget(client=Any).attached(MIRROR)  # pyright: ignore[reportArgumentType]
    assert [a.agent_id for a in attached] == ["owned"]
