"""scripts/changelog.py: fragment validation and folding into CHANGELOG.md."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("changelog", _ROOT / "scripts/changelog.py")
assert _spec and _spec.loader
changelog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(changelog)


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _add(repo: Path, topic: str) -> str:
    (repo / "changes" / f"{topic}.md").write_text(f"- **{topic}.**\n  Body.\n")
    _git(repo, "add", "changes")
    _git(repo, "commit", "-qm", topic)
    return _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    ("text", "valid"),
    [
        ("- **Lead.** Body\n  wrapped.\n", True),
        ("Lead. Body\n", False),
        ("- **Lead.** one\n\n- **Lead.** two\n", False),
        ("- **Lead.** one\nunindented.\n", False),
        ("- **Lead.** no newline", False),
    ],
)
def test_check_accepts_only_one_bold_led_paragraph(tmp_path: Path, text: str, valid: bool) -> None:
    fragment = tmp_path / "x.md"
    fragment.write_text(text)
    assert (not changelog.problems(fragment)) is valid, f"{text!r} verdict should be {valid}"


def test_fold_moves_only_fragments_reachable_from_until_newest_first(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "changes").mkdir()
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n- **Old.** kept.\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    _add(tmp_path, "first")
    second = _add(tmp_path, "second")
    _add(tmp_path, "third")

    assert changelog.fold(tmp_path, second) == 0, "fold should succeed"

    text = (tmp_path / "CHANGELOG.md").read_text()
    assert text.index("**second.**") < text.index("**first.**") < text.index("**Old.**"), text
    assert "third" not in text, "a fragment added after --until must stay"
    assert [p.name for p in changelog.fragments(tmp_path)] == ["third.md"], (
        "folded files are removed"
    )


def test_fold_until_counts_a_fragment_renamed_after_it_was_added(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "changes").mkdir()
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")
    _add(tmp_path, "draft")
    _git(tmp_path, "mv", "changes/draft.md", "changes/final.md")
    _git(tmp_path, "commit", "-qm", "rename")

    assert changelog.fold(tmp_path, _git(tmp_path, "rev-parse", "HEAD")) == 0
    assert "**draft.**" in (tmp_path / "CHANGELOG.md").read_text(), "the renamed fragment folds"
