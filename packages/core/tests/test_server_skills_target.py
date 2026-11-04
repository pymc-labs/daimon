"""The Managed Agents target: rollback of unconsented repins, no duplicate publish, managed agents."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.core.defaults.metadata import MA_METADATA_KEY_TENANT
from daimon.core.server_skills import anthropic as target_module
from daimon.core.server_skills.anthropic import ManagedAgentsTarget
from daimon.core.server_skills.gate import manifest_digest
from daimon.core.server_skills.sync import NeedsReconfirm, TargetError
from daimon.core.skills.ingest import bundle_from_files
from daimon.testing.mcp_skills import skill_md

FILES = {"SKILL.md": skill_md("notes")}
DIGEST = manifest_digest({p: hashlib.sha256(b).hexdigest() for p, b in FILES.items()})
MIRROR: Any = SimpleNamespace(
    id=uuid.uuid4(), tenant_id=uuid.uuid4(), skill_id="skill_1", digest=DIGEST
)


@dataclass(frozen=True)
class _Pins(ManagedAgentsTarget):
    pins: list[str] = field(default_factory=list[str])
    stuck: bool = False
    """The agent can't be moved back to the version it held."""

    async def _pin(self, mirror: Any, agent_id: str, version: str) -> str | None:
        self.pins.append(version)
        if self.stuck and version == "1":
            raise TargetError("down")
        return "1"


class _Keeper:
    def __init__(self, *, fails: Exception | None = None, covers: bool = False) -> None:
        self.fails = fails
        self.covered = covers

    async def check(self, _mirror: Any, _agent_id: str, _version: str) -> object:
        return object()

    async def renew(self, _mirror: Any, _agent_id: str, _consented: object) -> None:
        if self.fails is not None:
            raise self.fails

    async def covers(self, _mirror: Any, _agent_id: str) -> bool:
        return self.covered


def _target(keeper: _Keeper, *, stuck: bool = False) -> _Pins:
    return _Pins(client=Any, consents=keeper, stuck=stuck)  # pyright: ignore[reportArgumentType]


async def test_a_repin_its_consent_no_longer_covers_is_rolled_back() -> None:
    target = _target(_Keeper(fails=NeedsReconfirm("its source or its drops changed")))
    with pytest.raises(NeedsReconfirm):
        await target.repin(MIRROR, "agent", "2")
    assert target.pins == ["2", "1"], "moved, then back to the consented version"


async def test_a_renewed_repin_stays() -> None:
    target = _target(_Keeper())
    await target.repin(MIRROR, "agent", "2")
    assert target.pins == ["2"]


async def test_any_failed_renewal_rolls_back_and_is_retried() -> None:
    """A database or provider error must not leave the agent on a version it has no consent for."""
    target = _target(_Keeper(fails=RuntimeError("database down")))
    with pytest.raises(TargetError):
        await target.repin(MIRROR, "agent", "2")
    assert target.pins == ["2", "1"], "back on the consented version, so the next run retries"


async def test_a_failed_renewal_that_cannot_roll_back_holds_the_agent() -> None:
    target = _target(_Keeper(fails=RuntimeError("database down")), stuck=True)
    with pytest.raises(NeedsReconfirm):
        await target.repin(MIRROR, "agent", "2")
    assert target.pins == ["2", "1"], "the move back was tried"


@pytest.mark.parametrize("fails", [NeedsReconfirm("changed"), RuntimeError("database down")])
async def test_a_source_the_owner_just_confirmed_is_not_rolled_back(fails: Exception) -> None:
    """Re-confirming between the pin and the renewal already covers the new version."""
    target = _target(_Keeper(fails=fails, covers=True))
    await target.repin(MIRROR, "agent", "2")
    assert target.pins == ["2"], "the fresh consent is left alone"


async def test_a_pin_reports_the_version_the_agent_held(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rollback goes back to what the agent held, read from the record it updates."""
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
    previous = await ManagedAgentsTarget(client=Any)._pin(MIRROR, "agent", "2")  # pyright: ignore[reportArgumentType,reportPrivateUsage]
    assert previous == "1"
    assert sent == [[{"type": "custom", "skill_id": "skill_1", "version": "2"}]]


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
