"""Changelog fragments: validate them, and fold them into CHANGELOG.md.

A user-visible change adds ``changes/<topic>.md`` (one ``- **Bold lead.**``
paragraph) instead of editing ``CHANGELOG.md``, which every branch used to
touch. ``check`` is the CI lint; ``fold`` prepends the fragments to
``## [Unreleased]``, newest first by the commit that added them, and removes
them. stdlib-only. Run: python scripts/changelog.py check | fold [--until SHA]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_LEAD_RE = re.compile(r"^- \*\*[^*\n]+\*\*")
_HEADING = "## [Unreleased]"


def fragments(root: Path) -> list[Path]:
    return sorted(p for p in (root / "changes").glob("*") if p.name != "README.md")


def problems(path: Path) -> list[str]:
    """Why a fragment is not a single ``- **``-led paragraph; empty when it is."""
    if path.suffix != ".md":
        return ["fragments are .md files"]
    text = path.read_text()
    lines = text.rstrip("\n").split("\n")
    if not _LEAD_RE.match(lines[0]):
        return ["must start with '- **Bold lead.**'"]
    if any(not line.strip() for line in lines):
        return ["must be one paragraph, with no blank lines"]
    if any(not line.startswith("  ") for line in lines[1:]):
        return ["wrapped lines must be indented two spaces"]
    return [] if text.endswith("\n") else ["must end with a newline"]


def check(root: Path) -> int:
    failed = [(p, why) for p in fragments(root) for why in problems(p)]
    for path, why in failed:
        print(f"{path.relative_to(root)}: {why}", file=sys.stderr)
    return 1 if failed else 0


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if done.returncode:
        sys.exit(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def ordered(root: Path, until: str | None) -> list[Path]:
    """Fragments newest first, limited to those added in ancestors of ``until``.

    Without ``until``, uncommitted fragments count as newest; with it they are
    skipped, like fragments added after ``until``.
    """
    present = {p.relative_to(root).as_posix(): p for p in fragments(root)}
    log = _git(
        root, "log", until or "HEAD", "--diff-filter=A", "--name-only", "--format=", "--", "changes"
    )
    seen = [name for name in dict.fromkeys(log.split("\n")) if name in present]
    if until is None:
        seen = [name for name in present if name not in seen] + seen
    return [present[name] for name in seen]


def fold(root: Path, until: str | None) -> int:
    chosen = ordered(root, until)
    if not chosen:
        return 0
    changelog = root / "CHANGELOG.md"
    text = changelog.read_text()
    head, found, rest = text.partition(f"{_HEADING}\n")
    if not found:
        sys.exit(f"CHANGELOG.md has no '{_HEADING}' heading")
    block = "\n".join(f"{p.read_text().rstrip(chr(10))}\n" for p in chosen)
    changelog.write_text(f"{head}{found}\n{block}{rest}")
    tracked = _git(root, "ls-files", "--", *[str(p) for p in chosen]).split("\n")
    for path in chosen:
        if str(path.relative_to(root)) in tracked:
            _git(root, "rm", "-q", "-f", "--", str(path))
        else:
            path.unlink()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Changelog fragments.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="fail on a malformed fragment")
    folder = sub.add_parser("fold", help="move fragments into CHANGELOG.md [Unreleased]")
    folder.add_argument("--until", metavar="SHA", help="only fragments added up to this commit")
    args = parser.parse_args(argv)
    return check(REPO_ROOT) if args.command == "check" else fold(REPO_ROOT, args.until)


if __name__ == "__main__":
    sys.exit(main())
