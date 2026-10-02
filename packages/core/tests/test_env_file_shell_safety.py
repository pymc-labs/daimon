"""The serialized `.env` is `source`d by bash in the sandbox; prove that is inert.

Every agent is told to run ``set -a; source /mnt/session/uploads/.env``, and
any member can set a key's value. These tests write what `serialize_env_file`
produces, source it in a real bash exactly as the guidance does, and check two
things for each injection payload: nothing ran (no marker file appeared), and
the exported variable holds the value byte-for-byte.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from daimon.core.env_file import EnvFileRejected, parse_env_file, serialize_env_file

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


def _source_and_print(
    env_path: Path, name: str, cwd: Path, env: dict[str, str] | None = None
) -> bytes:
    """Source like the agent guidance says, then print the variable exactly."""
    script = f'set -a; source "{env_path}"; set +a; printf "%s" "${name}"'
    result = subprocess.run(
        ["bash", "--norc", "--noprofile", "-c", script],
        cwd=cwd,
        capture_output=True,
        check=False,
        timeout=10,
        env=env,
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


@pytest.fixture(scope="module")
def big5_locale(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """An environment running bash in zh_TW.BIG5, where 0x5C can be a trail byte."""
    if shutil.which("localedef") is None:
        pytest.skip("needs localedef to build a BIG5 locale")
    locpath = tmp_path_factory.mktemp("locale")
    built = subprocess.run(
        ["localedef", "-i", "zh_TW", "-f", "BIG5", str(locpath / "zh_TW.BIG5")],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if not (locpath / "zh_TW.BIG5" / "LC_CTYPE").exists():
        pytest.skip(f"could not build zh_TW.BIG5 (localedef exit {built.returncode})")
    return {**os.environ, "LOCPATH": str(locpath), "LC_ALL": "zh_TW.BIG5"}


#: U+4E21 is E4 B8 A1 in UTF-8; read as BIG5, A1 is a lead byte that takes the
#: next byte as its trail, so a backslash escape right after it would be eaten.
_TRAIL_BYTE_PAYLOADS: list[tuple[str, str]] = [
    ("double quote after a lead byte", '\u4e21"; touch {marker}; #'),
    ("single quote after a lead byte", "\u4e21'; touch {marker}; #"),
    ("both quotes, double after a lead byte", "it's \u4e21\"; touch {marker}; #"),
    ("dollar after a lead byte", "\u4e21$(touch {marker})"),
    ("backtick after a lead byte", "\u4e21`touch {marker}`"),
    ("backslash after a lead byte", "\u4e21\\$(touch {marker})"),
]


def test_the_big5_fixture_reproduces_the_trail_byte_injection(
    tmp_path: Path, big5_locale: dict[str, str]
) -> None:
    """Control: backslash-escaping inside double quotes is unsafe in this locale."""
    marker = tmp_path / "pwned"
    env_path = tmp_path / ".env"
    env_path.write_bytes(f'NOTE="\u4e21\\"; touch {marker}; #"\n'.encode())
    _source_and_print(env_path, "NOTE", tmp_path, env=big5_locale)
    assert marker.exists(), "the fixture must be a locale where the attack works"


@pytest.mark.parametrize(
    ("label", "template"), _TRAIL_BYTE_PAYLOADS, ids=[p[0] for p in _TRAIL_BYTE_PAYLOADS]
)
def test_sourcing_in_a_big5_locale_runs_nothing(
    tmp_path: Path, big5_locale: dict[str, str], label: str, template: str
) -> None:
    marker = tmp_path / "pwned"
    env_path = tmp_path / ".env"
    env_path.write_bytes(serialize_env_file([("NOTE", template.format(marker=marker))]))

    _source_and_print(env_path, "NOTE", tmp_path, env=big5_locale)

    assert not marker.exists(), f"{label}: a multibyte locale reopened injection"


@pytest.mark.parametrize("name", ["LC_ALL", "LC_CTYPE", "LANG", "LANGUAGE"])
def test_a_key_cannot_switch_the_sandbox_locale(name: str) -> None:
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file(f"{name}=zh_TW.BIG5\n")
    assert caught.value.rejection == "reserved_name", (
        "a multibyte locale would let a quoted value's backslash join a character"
    )


def _run_bash(script: str, cwd: Path, env: dict[str, str] | None = None) -> int:
    return subprocess.run(
        ["bash", "--norc", "--noprofile", "-c", script],
        cwd=cwd,
        capture_output=True,
        check=False,
        timeout=20,
        env=env,
    ).returncode


@pytest.mark.skipif(shutil.which("tar") is None, reason="needs GNU tar")
def test_tar_options_is_a_real_exec_vector_but_is_refused(tmp_path: Path) -> None:
    """TAR_OPTIONS quotes safely yet makes a later `tar` run a command.

    The value is inert to `source` (the fix from the first commit), so the
    only defence is refusing the NAME. This proves both halves: the control
    shows a hand-written `.env` under this name executes on the next `tar`,
    and `serialize_env_file`/`parse_env_file` never let such a name through.
    """
    from daimon.core.env_file import EnvFileRejected, is_reserved_env_name, parse_env_file

    marker = tmp_path / "tar_pwned"
    (tmp_path / "f.txt").write_text("x")
    # tar splits TAR_OPTIONS on whitespace itself, so the exec target is a
    # space-free helper script rather than a `touch <path>` with a space.
    evil = tmp_path / "evil.sh"
    evil.write_text(f"#!/bin/sh\ntouch {marker}\n")
    evil.chmod(0o755)
    # Control: a raw .env an operator could hand-write, sourced, then tar runs.
    raw_env = tmp_path / "raw.env"
    raw_env.write_bytes(f"TAR_OPTIONS='--checkpoint=1 --checkpoint-action=exec={evil}'\n".encode())
    _run_bash(f'set -a; source "{raw_env}"; set +a; tar -cf /dev/null f.txt', tmp_path)
    if not marker.exists():
        pytest.skip("this tar build does not honour TAR_OPTIONS checkpoint-action")

    # The fix: the name never reaches a stored key or the mounted file.
    assert is_reserved_env_name("TAR_OPTIONS")
    with pytest.raises(EnvFileRejected) as caught:
        parse_env_file(
            "API_KEY=ok\nTAR_OPTIONS=--checkpoint=1 --checkpoint-action=exec=touch /tmp/x\n"
        )
    assert caught.value.rejection == "reserved_name"


def test_assemble_leaves_out_a_legacy_tool_control_row(tmp_path: Path) -> None:
    """Mount-time hard-deny: a TAR_OPTIONS row stored before the rule never mounts."""
    import uuid

    from daimon.core.credential_env import assemble_env_bytes
    from daimon.core.stores.domain import AgentFileRow

    def _row(key: str, content: str) -> AgentFileRow:
        import datetime as dt

        now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        return AgentFileRow(
            tenant_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
            key=key,
            content=content,
            created_at=now,
            updated_at=now,
        )

    assembled = assemble_env_bytes(
        [_row("API_KEY", "ok"), _row("TAR_OPTIONS", "--checkpoint-action=exec=touch /tmp/x")]
    )
    assert assembled == b"API_KEY=ok\n", "a hard-denied legacy name must not be exported"
