"""The configured import checker refuses reintroduced product/harness edges."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
NAME = "Product code must not import test harnesses"


def _contract() -> dict:
    contracts = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["importlinter"][
        "contracts"
    ]
    return next(c for c in contracts if c["name"] == NAME)


def test_mux_source_scope_covers_initializer_and_all_non_testing_siblings() -> None:
    contract = _contract()
    package = ROOT / "packages/mux/mux"
    siblings = {
        "mux." + (p.stem if p.is_file() else p.name)
        for p in package.iterdir()
        if (p.suffix == ".py" or (p.is_dir() and (p / "__init__.py").exists()))
        and p.stem not in {"__init__", "testing"}
    }
    listed = {m for m in contract["source_modules"] if m.startswith("mux.") and "*" not in m}
    assert listed == siblings
    assert "mux" in contract["source_modules"]
    assert contract["as_packages"] is False


def _package(root: Path, module: str) -> Path:
    path = root
    for part in module.split("."):
        path /= part
        path.mkdir(exist_ok=True)
        (path / "__init__.py").touch()
    return path / "__init__.py"


def _check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlinter.cli import lint_imports_command; lint_imports_command()",
            "--config",
            str(root / "boundary.ini"),
            "--no-cache",
        ],
        cwd=root,
        env={"PATH": os.defpath, "PYTHONPATH": str(root), "PYTHONNOUSERSITE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize(
    "source",
    [
        "daimon.core.usage_recording",
        "daimon.adapters.discord.commands",
        "mux",
        "mux.drivers.gemini.core",
        "mux.state.usage_ledger",
    ],
)
@pytest.mark.parametrize(
    "target", ["mux.testing.conformance.budget", "daimon.testing.conformance.budget"]
)
@pytest.mark.parametrize("indirect", [False, True])
def test_real_import_linter_refuses_reintroduced_harness_import(
    tmp_path: Path, source: str, target: str, indirect: bool
) -> None:
    contract = _contract()
    # Reuse the shipped rule verbatim; no second policy or checker in the test.
    options = []
    for key, value in contract.items():
        if isinstance(value, list):
            options.append(key + " =\n" + "\n".join("    " + v for v in value))
        else:
            options.append(key + " = " + (str(value).lower() if isinstance(value, bool) else value))
    (tmp_path / "boundary.ini").write_text(
        "[importlinter]\nroot_packages =\n    daimon\n    mux\n    support\n\n[importlinter:contract:boundary]\n"
        + "\n".join(options)
        + "\n"
    )
    for module in contract["source_modules"] + contract["forbidden_modules"]:
        if "*" not in module:
            _package(tmp_path, module)
    file = _package(tmp_path, source)
    bridge = _package(tmp_path, "support.bridge")
    _package(tmp_path, target)
    baseline = _check(tmp_path)
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    assert NAME + " KEPT" in baseline.stdout, baseline.stdout + baseline.stderr
    if indirect:
        bridge.write_text(f"import {target}\n")
        file.write_text("import support.bridge\n")
    else:
        file.write_text(f"import {target}\n")
    broken = _check(tmp_path)
    assert broken.returncode != 0
    assert NAME + " BROKEN" in broken.stdout, broken.stdout + broken.stderr
    assert source in broken.stdout and target in broken.stdout
    # Removing the illicit import restores the SAME configured boundary.
    file.write_text("")
    repaired = _check(tmp_path)
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
