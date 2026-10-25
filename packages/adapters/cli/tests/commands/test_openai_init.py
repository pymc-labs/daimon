"""`daimon openai init` provisions real OpenAI resources and writes one
runtime-manifest row that the mux admission path can actually load."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path

import httpx
import pytest
from daimon.adapters.cli.commands.openai import _skill_bundle, openai_init_command
from daimon.core.turn.openai_deployment import OpenAIDeployment
from mux.drivers.openai.transport import SDKTransport
from mux.testing.isolated_sdk import build_async_openai

TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def packed(skill_dir: Path) -> bytes:
    bundle = _skill_bundle(skill_dir)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in bundle.files:
            archive.writestr("wrapper/" + file.path, file.content)
    return output.getvalue()


def fake_transport_factory(calls: list[httpx.Request], skill_dirs: list[Path] | None = None):
    skill_count = 0
    created_by_id: dict[str, Path] = {}
    ordered_dirs = list(skill_dirs or [])

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal skill_count
        calls.append(request)
        path = request.url.path
        body = (
            json.loads(request.content or b"{}")
            if request.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        if path == "/v1/skills":
            skill_dir = ordered_dirs[skill_count]
            skill_id = f"skill-{skill_count + 1}"
            created_by_id[skill_id] = skill_dir
            skill_count += 1
            return httpx.Response(
                200,
                json={
                    "id": skill_id,
                    "name": "bundle",
                    "latest_version": "1",
                    "created_at": 1700000000,
                },
            )
        if path.startswith("/v1/skills/") and path.endswith("/versions/1"):
            skill_id = path.removeprefix("/v1/skills/").removesuffix("/versions/1")
            return httpx.Response(200, json={"skill_id": skill_id, "version": "1"})
        if path.startswith("/v1/skills/") and path.endswith("/versions/1/content"):
            skill_id = path.removeprefix("/v1/skills/").removesuffix("/versions/1/content")
            return httpx.Response(200, content=packed(created_by_id[skill_id]))
        if path == "/v1/agents":
            return httpx.Response(
                200,
                json={
                    "id": "agent-123",
                    "model": body["model"],
                    "name": body.get("name", ""),
                    "metadata": body.get("metadata", {}),
                    "created_at": 1700000000,
                },
            )
        if path == "/v1/agents/environments/templates":
            return httpx.Response(
                200, json={"id": "env-123", "name": body.get("name", ""), "created_at": 1700000000}
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    def factory(*, api_key: str, project: str):
        del api_key, project
        return SDKTransport(build_async_openai(transport=httpx.MockTransport(handle)))

    return factory


def write_skill(
    defaults_root: Path, name: str, *, extra_files: dict[str, bytes] | None = None
) -> Path:
    skill_dir = defaults_root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name}.\n---\n\nbody\n")
    for rel_path, content in (extra_files or {}).items():
        path = skill_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return skill_dir


@pytest.fixture
def manifest_path(tmp_path: Path) -> Path:
    return tmp_path / "manifest.json"


def test_init_creates_agent_and_environment_and_writes_one_row(
    monkeypatch: pytest.MonkeyPatch, manifest_path: Path
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "sk-test-not-a-real-key")
    calls: list[httpx.Request] = []
    monkeypatch.setattr(
        "daimon.adapters.cli.commands.openai.configured_transport", fake_transport_factory(calls)
    )

    openai_init_command(
        tenant_id=TENANT_ID,
        project="proj_123",
        model="gpt-6-luna",
        manifest=manifest_path,
        defaults_root=manifest_path.parent / "no-defaults",
    )

    rows = json.loads(manifest_path.read_text())
    assert len(rows) == 1
    deployment = OpenAIDeployment.model_validate(rows[0])
    assert deployment.agent_id == "agent-123"
    assert deployment.environment_id == "env-123"
    assert deployment.platform == "*"
    assert deployment.channel_id == "*"
    assert deployment.account_id == "*"
    assert deployment.tenant_id == str(TENANT_ID)
    assert deployment.api_key_env == "DAIMON_CHANNEL_OPENAI_KEY"
    # Never the key value itself, anywhere in the written file.
    assert "sk-test-not-a-real-key" not in manifest_path.read_text()
    assert [c.url.path for c in calls] == ["/v1/agents", "/v1/agents/environments/templates"]


def test_init_is_idempotent_and_does_not_reprovision(
    monkeypatch: pytest.MonkeyPatch, manifest_path: Path
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "sk-test-not-a-real-key")
    calls: list[httpx.Request] = []
    monkeypatch.setattr(
        "daimon.adapters.cli.commands.openai.configured_transport", fake_transport_factory(calls)
    )

    for _ in range(2):
        openai_init_command(
            tenant_id=TENANT_ID,
            project="proj_123",
            model="gpt-6-luna",
            manifest=manifest_path,
            defaults_root=manifest_path.parent / "no-defaults",
        )

    rows = json.loads(manifest_path.read_text())
    assert len(rows) == 1
    # The second call reused the existing row instead of calling OpenAI again.
    assert len(calls) == 2


def test_init_seeds_every_default_skill_onto_a_persistent_workspace_agent(
    monkeypatch: pytest.MonkeyPatch, manifest_path: Path, tmp_path: Path
) -> None:
    defaults_root = tmp_path / "defaults"
    alpha = write_skill(defaults_root, "alpha")
    beta = write_skill(defaults_root, "beta", extra_files={"assets/logo.png": b"\x89PNG"})
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "sk-test-not-a-real-key")
    calls: list[httpx.Request] = []
    monkeypatch.setattr(
        "daimon.adapters.cli.commands.openai.configured_transport",
        fake_transport_factory(calls, [alpha, beta]),
    )

    openai_init_command(
        tenant_id=TENANT_ID,
        project="proj_123",
        model="gpt-6-luna",
        manifest=manifest_path,
        defaults_root=defaults_root,
    )

    rows = json.loads(manifest_path.read_text())
    deployment = OpenAIDeployment.model_validate(rows[0])
    assert sorted(deployment.skill_ids) == ["skill-1", "skill-2"]
    paths = [c.url.path for c in calls]
    assert paths.count("/v1/skills") == 2
    # Both skills (and their post-create pin verification) happen before the
    # agent that attaches them exists.
    last_skill_call = max(i for i, p in enumerate(paths) if p.startswith("/v1/skills"))
    assert paths.index("/v1/agents") > last_skill_call


def test_init_leaves_out_a_skill_a_default_agent_limits_to_platforms(
    monkeypatch: pytest.MonkeyPatch, manifest_path: Path, tmp_path: Path
) -> None:
    """A Teams-only default skill is added per session on Anthropic, never to an OpenAI agent."""
    defaults_root = tmp_path / "defaults"
    write_skill(defaults_root, "alpha")
    beta = write_skill(defaults_root, "beta")
    (defaults_root / "agents").mkdir()
    (defaults_root / "agents" / "daimon.yaml").write_text(
        "name: daimon\nmodel: claude-sonnet-4-6\nskills:\n"
        "  - {type: custom, skill_id: alpha, platforms: [teams]}\n"
        "  - {type: custom, skill_id: beta}\n"
    )
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "sk-test-not-a-real-key")
    calls: list[httpx.Request] = []
    monkeypatch.setattr(
        "daimon.adapters.cli.commands.openai.configured_transport",
        fake_transport_factory(calls, [beta]),
    )

    openai_init_command(
        tenant_id=TENANT_ID,
        project="proj_123",
        model="gpt-6-luna",
        manifest=manifest_path,
        defaults_root=defaults_root,
    )

    deployment = OpenAIDeployment.model_validate(json.loads(manifest_path.read_text())[0])
    assert list(deployment.skill_ids) == ["skill-1"], "only the unlimited skill is attached"
    assert [c.url.path for c in calls].count("/v1/skills") == 1, (
        "the Teams-only one is not uploaded"
    )


def test_init_skip_default_skills_flag_creates_a_bare_agent(
    monkeypatch: pytest.MonkeyPatch, manifest_path: Path, tmp_path: Path
) -> None:
    defaults_root = tmp_path / "defaults"
    write_skill(defaults_root, "alpha")
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "sk-test-not-a-real-key")
    calls: list[httpx.Request] = []
    monkeypatch.setattr(
        "daimon.adapters.cli.commands.openai.configured_transport", fake_transport_factory(calls)
    )

    openai_init_command(
        tenant_id=TENANT_ID,
        project="proj_123",
        model="gpt-6-luna",
        manifest=manifest_path,
        defaults_root=defaults_root,
        skip_default_skills=True,
    )

    rows = json.loads(manifest_path.read_text())
    deployment = OpenAIDeployment.model_validate(rows[0])
    assert deployment.skill_ids == ()
    assert "/v1/skills" not in [c.url.path for c in calls]


def test_init_reuses_the_same_skill_idempotency_key_across_different_channels(
    monkeypatch: pytest.MonkeyPatch, manifest_path: Path, tmp_path: Path
) -> None:
    """Two non-wildcard `init` runs for different channels (step 3's
    per-channel alternative) must send the same skill-create idempotency
    key, so the provider's own idempotency reuses one skill set instead of
    uploading a fresh duplicate copy per channel."""
    defaults_root = tmp_path / "defaults"
    alpha = write_skill(defaults_root, "alpha")
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "sk-test-not-a-real-key")
    calls: list[httpx.Request] = []
    monkeypatch.setattr(
        "daimon.adapters.cli.commands.openai.configured_transport",
        fake_transport_factory(calls, [alpha, alpha]),
    )

    openai_init_command(
        tenant_id=TENANT_ID,
        project="proj_123",
        model="gpt-6-luna",
        manifest=tmp_path / "manifest-a.json",
        defaults_root=defaults_root,
        platform="discord",
        channel_id="channel-a",
        account_id="account-a",
    )
    openai_init_command(
        tenant_id=TENANT_ID,
        project="proj_123",
        model="gpt-6-luna",
        manifest=tmp_path / "manifest-b.json",
        defaults_root=defaults_root,
        platform="discord",
        channel_id="channel-b",
        account_id="account-b",
    )

    skill_create_keys = [c.headers["idempotency-key"] for c in calls if c.url.path == "/v1/skills"]
    assert len(skill_create_keys) == 2
    assert skill_create_keys[0] == skill_create_keys[1]
    assert str(TENANT_ID) in skill_create_keys[0]
    assert "channel-a" not in skill_create_keys[0]
    assert "channel-b" not in skill_create_keys[0]


def test_init_warns_when_reusing_a_row_with_a_different_api_key_env(
    monkeypatch: pytest.MonkeyPatch, manifest_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY_2", "sk-test-not-a-real-key-2")
    calls: list[httpx.Request] = []
    monkeypatch.setattr(
        "daimon.adapters.cli.commands.openai.configured_transport", fake_transport_factory(calls)
    )

    openai_init_command(
        tenant_id=TENANT_ID,
        project="proj_123",
        model="gpt-6-luna",
        manifest=manifest_path,
        defaults_root=manifest_path.parent / "no-defaults",
    )
    capsys.readouterr()
    openai_init_command(
        tenant_id=TENANT_ID,
        project="proj_123",
        model="gpt-6-luna",
        manifest=manifest_path,
        defaults_root=manifest_path.parent / "no-defaults",
        api_key_env="DAIMON_CHANNEL_OPENAI_KEY_2",
    )

    out = capsys.readouterr().out
    assert "was not applied" in out
    assert "DAIMON_CHANNEL_OPENAI_KEY_2" in out
    rows = json.loads(manifest_path.read_text())
    assert len(rows) == 1
    assert rows[0]["api_key_env"] == "DAIMON_CHANNEL_OPENAI_KEY"


def test_init_manifest_write_leaves_no_temp_file_behind(
    monkeypatch: pytest.MonkeyPatch, manifest_path: Path
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(
        "daimon.adapters.cli.commands.openai.configured_transport", fake_transport_factory([])
    )

    openai_init_command(
        tenant_id=TENANT_ID,
        project="proj_123",
        model="gpt-6-luna",
        manifest=manifest_path,
        defaults_root=manifest_path.parent / "no-defaults",
    )

    assert [p.name for p in manifest_path.parent.iterdir()] == [manifest_path.name]
