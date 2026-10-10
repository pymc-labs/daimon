"""Real default inputs and driver limits: honest F1 deferral before I/O."""

from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from mux.conformance.default_capability import (
    DEFAULT_SKILLS,
    DEFAULT_TOOLS,
    DefaultManifest,
    NamedSkill,
    skill_text,
)
from mux.conformance.recording import Tape
from mux.conformance.runner import (
    ConformanceFailure,
    PendingKind,
    replay_default_capability,
    run_default_capability,
)
from mux.contracts.ids import ModelRef
from mux.contracts.resources import AgentSpec, MCPConnection, SkillUpload, SkillUploadFile, ToolSpec
from mux.drivers.gemini.bundles import MAX_BUNDLE_BYTES
from mux.drivers.gemini.core import compile_agent
from mux.drivers.gemini.default_capability import PENDING, PendingTransport, adapter
from mux.errors import UnsupportedCapability
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[5]


class SkillName(BaseModel):
    skill_id: str


class ToolName(BaseModel):
    name: str


class Toolset(BaseModel):
    configs: tuple[ToolName, ...]


class AuthoredDefault(BaseModel):
    name: str
    system: str
    skills: tuple[SkillName, ...]
    tools: tuple[Toolset, ...]


@pytest.fixture
def manifest() -> DefaultManifest:
    authored = AuthoredDefault.model_validate(
        yaml.safe_load((ROOT / "defaults/agents/daimon.yaml").read_text())
    )
    skills: list[NamedSkill] = []
    for ref in authored.skills:
        directory = ROOT / "defaults/skills" / ref.skill_id
        skills.append(
            NamedSkill(
                ref.skill_id,
                SkillUpload(
                    files=tuple(
                        SkillUploadFile(
                            path=str(path.relative_to(directory)), content=path.read_bytes()
                        )
                        for path in sorted(directory.rglob("*"))
                        if path.is_file()
                    )
                ),
            )
        )
    return DefaultManifest(
        name=authored.name,
        system=authored.system,
        skills=tuple(skills),
        builtin_tools=tuple(tool.name for toolset in authored.tools for tool in toolset.configs),
        mcp=MCPConnection(
            name="daimon-mcp",
            url="https://daimon.invalid/mcp",
            credential_ref="f1-account-mcp",
        ),
    )


@pytest.mark.asyncio
async def test_full_authored_default_is_pending_before_provisioning(
    manifest: DefaultManifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert tuple(skill.name for skill in manifest.skills) == DEFAULT_SKILLS
    assert manifest.builtin_tools == DEFAULT_TOOLS
    assert skill_text(manifest) == (ROOT / "defaults/skills/file-handling/SKILL.md").read_text()
    instance = adapter()

    async def forbidden_provision(*args: object, **kwargs: object) -> None:
        raise AssertionError("pending F1 reached skill provisioning")

    monkeypatch.setattr(instance.driver.skills, "create", forbidden_provision)
    result = await run_default_capability(manifest, instance)
    assert result.fixture_id == "F1" and result.status == "pending"
    assert result.pending_reason == PENDING
    assert result.pending_reason is not None
    assert result.pending_reason.kind == PendingKind.ADAPTER_DEPENDENCY
    assert isinstance(instance.transport, PendingTransport)
    instance.transport.assert_consumed()
    assert instance.transport.skill_uploads == ()
    with pytest.raises(ConformanceFailure, match="no upstream default deployment"):
        _ = instance.transport.deployed_agent


@pytest.mark.asyncio
async def test_removing_pending_does_not_fake_a_default_pass(manifest: DefaultManifest) -> None:
    instance = adapter()
    # A complete mapping alone cannot unblock the full binary/authenticated
    # default. The other genuine preflight/provisioning gaps remain explicit.
    result = await run_default_capability(
        manifest,
        replace(
            instance,
            pending=None,
            builtin_mapping={name: ToolSpec(name=name, kind="builtin") for name in DEFAULT_TOOLS},
        ),
    )
    assert result.status == "fail"
    instance.transport.assert_consumed()


@pytest.mark.asyncio
async def test_full_binary_default_skill_is_rejected_without_provider_io(
    manifest: DefaultManifest,
) -> None:
    instance = adapter()
    bundle = next(s.upload for s in manifest.skills if s.name == "pymc-artifact-style")
    assert sum(len(file.content) for file in bundle.files) > MAX_BUNDLE_BYTES
    assert any(file.path.endswith(".ttf") for file in bundle.files)
    with pytest.raises(ValueError, match="inline size limit"):
        await instance.driver.skills.create(instance.scope, bundle, key="binary-default")
    instance.transport.assert_consumed()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [name for name in DEFAULT_SKILLS if name != "pymc-artifact-style"])
async def test_complete_text_default_skill_is_feasible_offline(
    manifest: DefaultManifest, name: str
) -> None:
    instance = adapter()
    bundle = next(s.upload for s in manifest.skills if s.name == name)
    skill = await instance.driver.skills.create(instance.scope, bundle, key="supported-skill")
    assert skill.latest_version is not None and skill.latest_version.version != "latest"
    assert await instance.driver.skills.retrieve(instance.scope, skill.id) == skill
    instance.transport.assert_consumed()


def test_authenticated_mcp_gap_is_driver_specific_not_native_mcp_unavailable() -> None:
    public = MCPConnection(name="daimon-mcp", url="https://daimon.invalid/mcp")
    spec = AgentSpec(name="daimon", model=ModelRef(provider="gemini", id="gemini-3.5-flash-lite"))
    request = compile_agent(spec.model_copy(update={"mcp_servers": (public,)}))
    assert request["tools"] == [
        {"type": "mcp_server", "name": "daimon-mcp", "url": "https://daimon.invalid/mcp"}
    ]
    private = public.model_copy(update={"credential_ref": "f1-account-mcp"})
    with pytest.raises(UnsupportedCapability):
        compile_agent(spec.model_copy(update={"mcp_servers": (private,)}))


def test_each_adapter_has_fresh_resources_and_transport() -> None:
    first, second = adapter(), adapter()
    assert first.driver is not second.driver and first.transport is not second.transport
    assert first.model == second.model
    assert first.model.id == "gemini-3.5-flash-lite"
    assert first.pending == second.pending == PENDING


def test_pending_transport_rejects_unconsumed_script_evidence() -> None:
    transport = PendingTransport()
    transport.responses.append({"id": "not-observed"})
    with pytest.raises(ConformanceFailure, match="unconsumed script evidence"):
        transport.assert_consumed()


@pytest.mark.asyncio
async def test_incomplete_f1_tape_cannot_be_certified_by_the_pending_adapter(
    manifest: DefaultManifest, tmp_path: Path
) -> None:
    tape = Tape(
        fixture_id="F1",
        provider="gemini",
        model="gemini-3.5-flash-lite",
        complete=False,
        batches=(),
    )
    path = tmp_path / "incomplete.json"
    path.write_text(tape.model_dump_json())
    result = await replay_default_capability(path, manifest, adapter)
    assert result.status == "fail"
    assert result.evidence == ("replay refused: RecordingError",)
