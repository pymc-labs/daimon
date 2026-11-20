"""`daimon evals bundle validate` checks a bundle offline and exits 1 on a problem."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from daimon.adapters.cli.main import app
from typer.testing import CliRunner

EXAMPLE = Path(__file__).parents[4] / "core/tests/evals/example_bundle"


def test_bundle_validate_when_case_edited_then_exits_with_problems(tmp_path: Path) -> None:
    runner = CliRunner()
    ok = runner.invoke(app, ["evals", "bundle", "validate", str(EXAMPLE)])
    assert ok.exit_code == 0 and json.loads(ok.stdout)["ok"], ok.stdout
    broken = shutil.copytree(EXAMPLE, tmp_path / "bundle")
    cases = next(broken.glob("suites/*/cases.jsonl"))
    cases.write_text(cases.read_text().replace('"title": "', '"title": "Edited ', 1))
    failed = runner.invoke(app, ["evals", "bundle", "validate", str(broken)])
    assert failed.exit_code == 1 and json.loads(failed.stdout)["problems"], failed.stdout
