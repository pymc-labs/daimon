"""Integration tests for notebook_host.lifecycle.validate_notebook.

These spawn a REAL `marimo export` subprocess (via `uv run --with marimo`), so
they are opt-in — set ``DAIMON_NOTEBOOK_VALIDATION_IT=1`` to run them. They stay
out of the default suite to keep CI fast and avoid a heavy subprocess on the
constrained GH runner. The handler-level behaviour is covered by the stub-
validator tests in test_admin.py; these confirm the real marimo export actually
flags a cell collision and that the output parser catches it.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from notebook_host.lifecycle import validate_notebook

pytestmark = pytest.mark.skipif(
    os.environ.get("DAIMON_NOTEBOOK_VALIDATION_IT") != "1" or shutil.which("uv") is None,
    reason="set DAIMON_NOTEBOOK_VALIDATION_IT=1 (and have uv on PATH) to run validation IT",
)

# marimo-only notebooks (no pandas/numpy) so the isolated `uv run --with marimo`
# env is enough — we're testing the dataflow check, not library availability.
_CLEAN = """
import marimo

app = marimo.App()

@app.cell
def _():
    import marimo as mo
    mo.md("# ok")
    return (mo,)
"""

# `i` is defined as a loop variable in two cells → MultipleDefinitionError.
_COLLISION = """
import marimo

app = marimo.App()

@app.cell
def _():
    for i in range(3):
        pass
    return ()

@app.cell
def _():
    for i in range(3):
        pass
    return ()
"""


def test_validate_notebook_passes_clean_notebook(tmp_path: Path) -> None:
    path = tmp_path / "clean.py"
    path.write_text(_CLEAN)
    result = validate_notebook("clean", path, timeout_s=120.0)
    assert result.ok, f"a clean notebook should validate; got errors: {result.errors}"


def test_validate_notebook_flags_loop_variable_collision(tmp_path: Path) -> None:
    path = tmp_path / "collision.py"
    path.write_text(_COLLISION)
    result = validate_notebook("collision", path, timeout_s=120.0)
    assert not result.ok, "a cross-cell loop-variable collision must fail validation"
    assert any("MultipleDefinitionError" in e for e in result.errors), (
        f"the collision should be reported as MultipleDefinitionError; got: {result.errors}"
    )


# `cowsay` is tiny, pure-python, dependency-free, and not in the host's baked
# set — a faithful stand-in for fastf1 without the heavy telemetry stack.
_SANDBOX_DEP = """
# /// script
# requires-python = ">=3.12"
# dependencies = ["marimo", "cowsay"]
# ///
import marimo

app = marimo.App()

@app.cell
def _():
    import cowsay
    return (cowsay,)
"""


def test_validate_notebook_sandbox_installs_declared_dependency(tmp_path: Path) -> None:
    """A PEP 723 dep outside the baked set imports cleanly under --sandbox."""
    path = tmp_path / "sandbox.py"
    path.write_text(_SANDBOX_DEP)
    result = validate_notebook("sandbox", path, timeout_s=180.0, sandbox=True)
    assert result.ok, (
        f"a declared dependency must be installed and importable under --sandbox; "
        f"got errors: {result.errors}"
    )


def test_validate_notebook_without_sandbox_cannot_import_undeclared_dependency(
    tmp_path: Path,
) -> None:
    """Control: the same import fails on the baked env — sandbox is what fixes it."""
    path = tmp_path / "no_sandbox.py"
    path.write_text(_SANDBOX_DEP)
    result = validate_notebook("no_sandbox", path, timeout_s=120.0, sandbox=False)
    assert not result.ok, (
        "cowsay is not baked into the host env; the import must fail without sandbox"
    )
