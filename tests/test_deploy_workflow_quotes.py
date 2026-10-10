"""The remote scripts in deploy.yml stay inside one single-quoted argument.

Each VM refresh passes its script as `gcloud compute ssh ... --command '<script>'`.
A single quote anywhere in that script, even in a comment, ends the argument
early and gcloud fails with "unrecognized arguments" (#606 did this with
"asset's", and every staging deploy failed at the worker refresh until fixed).
"""

from __future__ import annotations

import re
from pathlib import Path

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_OPEN = re.compile(r"--command '\n")
_CLOSE = re.compile(r"^\s*'", re.MULTILINE)


def _remote_scripts(text: str) -> list[tuple[int, str]]:
    scripts: list[tuple[int, str]] = []
    for opened in _OPEN.finditer(text):
        closed = _CLOSE.search(text, opened.end())
        assert closed is not None, "a --command script never closes its quote"
        line = text.count("\n", 0, opened.start()) + 1
        scripts.append((line, text[opened.end() : closed.start()]))
    return scripts


def test_no_remote_script_contains_a_single_quote() -> None:
    found = 0
    for workflow in sorted(_WORKFLOWS.glob("*.yml")):
        for line, script in _remote_scripts(workflow.read_text()):
            found += 1
            for offset, text in enumerate(script.splitlines(), start=line + 1):
                assert "'" not in text, (
                    f"{workflow.name}:{offset} has a single quote inside a --command "
                    f"script, which ends the ssh argument early: {text.strip()}"
                )
    assert found >= 3, "expected the deploy workflow's remote scripts"
