from __future__ import annotations

import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest
from daimon.core.defaults.loader import load_agent_specs
from daimon.core.skill_zip import build_skill_zip

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("skill", "script", "fixture_request"),
    [
        ("workspace-write", "workspace_write", {"operation": "auth", "body": {}}),
        (
            "slides-workflow",
            "slides_workflow",
            {"operation": "delete", "body": {"object_ids": ["missing"]}},
        ),
        ("toggl-edits", "toggl_edits", {"operation": "stop", "workspace_id": 1, "entry_ids": [2]}),
        (
            "image-workflow",
            "image_workflow",
            {"operation": "generate", "provider": "gemini", "prompt": "fixture"},
        ),
    ],
)
def test_delivered_skill_runs_independently_and_refuses_without_network(
    skill, script, fixture_request, tmp_path
):
    package = build_skill_zip(ROOT / "defaults" / "skills" / skill)
    try:
        with zipfile.ZipFile(package.path) as archive:
            assert not any("__pycache__" in name for name in archive.namelist())
            archive.extractall(tmp_path)
        request_path = tmp_path / "request.json"
        request_path.write_text(json.dumps(fixture_request))
        environment = {**os.environ, "PYTHONPATH": "", "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(
            [
                os.sys.executable,
                str(tmp_path / skill / "scripts" / f"{script}.py"),
                str(request_path),
            ],
            input="synthetic-token",
            capture_output=True,
            text=True,
            env=environment,
            cwd=tmp_path,
            timeout=10,
            check=False,
        )
        assert result.returncode == 1 and not result.stderr
        assert json.loads(result.stdout)["ok"] is False
        assert "synthetic-token" not in result.stdout
    finally:
        package.path.unlink()


def test_optional_agents_have_skills_but_default_has_no_workflow_credentials():
    specs = load_agent_specs(ROOT / "defaults" / "agents-optional")
    expected = {
        "workspace-writer": {"workspace-write", "slides-workflow"},
        "time-editor": {"toggl-edits"},
        "image-maker": {"image-workflow"},
    }
    for name, skills in expected.items():
        spec = next(spec for spec in specs if spec.name == name)
        assert skills <= {reference.skill_id for reference in spec.skills}
    defaults = load_agent_specs(ROOT / "defaults" / "agents")
    assert not ({spec.name for spec in defaults} & set(expected))


def test_all_four_packaged_runtimes_are_byte_identical():
    root = ROOT / "defaults" / "skills"
    canonical = (root / "workspace-write" / "scripts" / "workflow_io.py").read_bytes()
    for name in ("slides-workflow", "toggl-edits", "image-workflow"):
        assert (root / name / "scripts" / "workflow_io.py").read_bytes() == canonical


def test_runtime_check_fails_when_expected_skill_folder_is_missing(tmp_path, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "sync_workflow_runtime", ROOT / "scripts" / "sync_workflow_runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "workspace-write" / "scripts" / "workflow_io.py"
    source.parent.mkdir(parents=True)
    source.write_text("# synthetic runtime\n")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "SOURCE", source)
    monkeypatch.setattr("sys.argv", ["sync_workflow_runtime.py", "--check"])
    assert module.main() == 1
    assert "missing skill scripts folder: slides-workflow/scripts" in capsys.readouterr().out
