"""The mux-leg ratchet on a synthetic tree: listed failures pass the leg, drift fails it."""

import subprocess
import sys
from pathlib import Path

from daimon.testing.mux_leg_known import read_known

_TESTS = """
def test_passes():
    pass

def test_known_failure():
    assert False

def test_new_failure():
    assert False
"""


def _run(root: Path, *known: str) -> subprocess.CompletedProcess[str]:
    (root / "test_sample.py").write_text(_TESTS)
    (root / "known.txt").write_text("# comment\n" + "\n".join(known) + "\n")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "test_sample.py", "-q", "-p", "no:cacheprovider"]
        + ["-p", "daimon.testing.mux_leg_known", "--mux-leg-known-failures=known.txt"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_read_known_skips_comments_and_blanks(tmp_path: Path) -> None:
    listing = tmp_path / "list.txt"
    listing.write_text("# header\n\na.py::t  # why\n b.py::u[x y] \n")

    assert read_known(listing) == ["a.py::t", "b.py::u[x y]"]


def test_listed_failure_is_expected_and_unlisted_failure_still_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, "test_sample.py::test_known_failure")

    assert result.returncode == 1
    assert "1 failed, 1 passed, 1 xfailed" in result.stdout
    assert "FAILED test_sample.py::test_new_failure" in result.stdout


def test_listed_test_that_passes_fails_the_leg(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "test_sample.py::test_known_failure",
        "test_sample.py::test_new_failure",
        "test_sample.py::test_passes",
    )

    assert result.returncode == 1
    assert "XPASS(strict)" in result.stdout


def test_listed_id_that_no_longer_exists_fails_the_leg(tmp_path: Path) -> None:
    result = _run(tmp_path, "test_sample.py::test_gone")

    assert result.returncode != 0
    assert "no longer exist" in result.stdout + result.stderr


def test_listed_id_in_a_deleted_file_fails_the_leg(tmp_path: Path) -> None:
    result = _run(
        tmp_path, "test_deleted.py::test_known_failure", "test_sample.py::test_known_failure"
    )

    assert result.returncode != 0
    assert "test_deleted.py::test_known_failure" in result.stdout + result.stderr
