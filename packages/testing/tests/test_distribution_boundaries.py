"""Built wheels and package roots cannot hide QA files through build rules."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "distribution_boundaries", ROOT / "scripts/lint_distribution_boundaries.py"
)
assert SPEC is not None and SPEC.loader is not None
boundary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(boundary)


def project(root: Path, *, name: str = "daimon-mux") -> Path:
    directory = root / "packages" / name
    directory.mkdir(parents=True)
    (directory / "pyproject.toml").write_text(
        f'[project]\nname="{name}"\n[tool.hatch.build.targets.wheel]\npackages=["mux"]\n'
    )
    package = directory / "mux"
    package.mkdir()
    return package


def test_external_tests_are_allowed_but_packaged_tests_are_not(tmp_path: Path) -> None:
    package = project(tmp_path)
    (package / "ports.py").write_text("from mux.contracts import events")
    external = package.parent / "tests"
    external.mkdir()
    (external / "test_ports.py").write_text("pass")
    assert boundary.source_violations(tmp_path) == []
    (package / "test_hidden.py").write_text("pass")
    assert boundary.source_violations(tmp_path)


def test_testing_support_is_allowed_without_packaging_tests(tmp_path: Path) -> None:
    package = project(tmp_path, name="daimon-testing")
    (package / "conformance").mkdir()
    (package / "conformance/budget.py").write_text("pass")
    assert boundary.source_violations(tmp_path) == []
    (package / "test_leak.py").write_text("pass")
    assert boundary.source_violations(tmp_path)


def test_missing_configured_package_is_not_a_silent_skip(tmp_path: Path) -> None:
    package = project(tmp_path)
    package.rmdir()
    assert any(
        "missing wheel package" in message for message in boundary.source_violations(tmp_path)
    )


def wheel(root: Path, entries: dict[str, str], *, name: str = "daimon-mux") -> Path:
    path = root / "fixture.whl"
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "fixture.dist-info/METADATA", f"Metadata-Version: 2.3\nName: {name}\nVersion: 1\n"
        )
        for member, content in entries.items():
            archive.writestr(member, content)
    return path


@pytest.mark.parametrize(
    "member",
    [
        "mux/conformance/budget.py",
        "mux/testing_extras/fake.py",
        "mux/drivers/gemini/fake.py",
        "mux/drivers/gemini/round_runner.py",
        "mux/drivers/gemini/live-budget.example.json",
        "mux/drivers/gemini/certification/receipt.json",
        "mux/drivers/openai/offline_sdk.py",
        "mux/drivers/openai/default_capability.py",
        "daimon/adapters/discord/backend_conformance.py",
        "daimon/core/test_secret.py",
        "daimon/core/conftest.py",
        "mux/drivers/gemini/PRICING.md",
        "daimon/testing/factories.py",
    ],
)
def test_built_product_wheel_is_checked_independently_of_source(
    tmp_path: Path, member: str
) -> None:
    assert boundary.wheel_violations(wheel(tmp_path, {member: ""}))


def test_mux_testing_package_ships_in_the_mux_wheel_but_not_its_tests(tmp_path: Path) -> None:
    allowed = wheel(
        tmp_path,
        {
            "mux/testing/conformance/budget.py": "pass",
            "mux/testing/drivers/gemini/fake.py": "pass",
            "mux/testing/drivers/gemini/live_cert.py": "pass",
        },
    )
    assert boundary.wheel_violations(allowed) == []
    leaked = wheel(tmp_path, {"mux/testing/test_budget.py": "pass"})
    assert boundary.wheel_violations(leaked)


def test_testing_wheel_contains_helpers_but_not_tests(tmp_path: Path) -> None:
    archive = wheel(
        tmp_path, {"daimon/testing/conformance/budget.py": "pass"}, name="daimon-testing"
    )
    assert boundary.wheel_violations(archive) == []
    archive = wheel(
        tmp_path, {"daimon/testing/conformance/test_budget.py": "pass"}, name="daimon-testing"
    )
    assert boundary.wheel_violations(archive)


def test_missing_distribution_metadata_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "broken.whl"
    with ZipFile(path, "w") as archive:
        archive.writestr("mux/ports.py", "pass")
    assert boundary.wheel_violations(path)


def test_empty_wheel_directory_fails_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "sys.argv", ["boundary", "--root", str(tmp_path), "--wheels", str(tmp_path)]
    )
    assert boundary.main() == 1
