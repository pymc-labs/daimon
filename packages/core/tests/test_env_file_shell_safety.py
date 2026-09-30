"""The serialized `.env` is `source`d by bash in the sandbox; prove that is inert.

Every agent is told to run ``set -a; source /mnt/session/uploads/.env``, and
any member can set a key's value. These tests write what `serialize_env_file`
produces, source it in a real bash exactly as the guidance does, and check two
things for each injection payload: nothing ran (no marker file appeared), and
the exported variable holds the value byte-for-byte.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from daimon.core.env_file import serialize_env_file

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

#: `{marker}` is replaced with a path the payload tries to create.
_PAYLOADS: list[tuple[str, str]] = [
    ("command substitution", "$(touch {marker})"),
    ("backticks", "`touch {marker}`"),
    ("semicolon", "x; touch {marker}"),
    ("ampersand", "x & touch {marker}"),
    ("pipe", "x | touch {marker}"),
    ("and-list", "x && touch {marker}"),
    ("newline then command", "x\ntouch {marker}"),
    ("single-quote breakout", "x'; touch {marker}; '"),
    ("double-quote breakout", 'x"; touch {marker}; "'),
    ("quote and substitution", "it's $(touch {marker})"),
    ("quote and backticks", "it's `touch {marker}`"),
    ("process substitution", "<(touch {marker})"),
    ("redirect", "x >{marker}"),
    ("parameter expansion", "${{X:=$(touch {marker})}}"),
    ("arithmetic", "$((0))$(touch {marker})"),
    ("carriage return", "x\r$(touch {marker})"),
    ("backslash then dollar", "\\$(touch {marker})"),
    ("variable reference", "$HOME-{marker}"),
    ("tilde", "~/{marker}"),
    ("glob", "/*{marker}"),
    ("unicode then substitution", "café$(touch {marker})"),
    ("non-breaking space edge", "\u00a0$(touch {marker})\u00a0"),
]


def _source_and_print(env_path: Path, name: str, cwd: Path) -> bytes:
    """Source like the agent guidance says, then print the variable exactly."""
    script = f'set -a; source "{env_path}"; set +a; printf "%s" "${name}"'
    result = subprocess.run(
        ["bash", "--norc", "--noprofile", "-c", script],
        cwd=cwd,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, "sourcing the serialized file must not fail"
    return result.stdout


@pytest.mark.parametrize(("label", "template"), _PAYLOADS, ids=[p[0] for p in _PAYLOADS])
def test_sourcing_a_serialized_value_runs_nothing(
    tmp_path: Path, label: str, template: str
) -> None:
    marker = tmp_path / "pwned"
    value = template.format(marker=marker)
    env_path = tmp_path / ".env"
    env_path.write_bytes(serialize_env_file([("OK", "1"), ("NOTE", value), ("AFTER", "2")]))

    printed = _source_and_print(env_path, "NOTE", tmp_path)

    assert not marker.exists(), f"{label}: sourcing the .env ran the payload"
    # A line break has no one-line representation; bash sees the escape text.
    expected = value.replace("\n", "\\n").replace("\r", "\\r")
    assert printed == expected.encode(), f"{label}: bash must read the value literally"


def test_sourcing_keeps_every_other_line_intact(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_bytes(serialize_env_file([("NOTE", "x'; AFTER=hijacked; '"), ("AFTER", "kept")]))
    assert _source_and_print(env_path, "AFTER", tmp_path) == b"kept", (
        "a value must not be able to assign another variable"
    )
