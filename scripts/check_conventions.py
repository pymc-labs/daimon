"""The `conventions` CI job: fail on any breach of the delivery-flow rules.

Checks the pull request's merge result against its base (CLAUDE.md "Changelog",
"Migrations", "Documentation"; CONTRIBUTING.md). Every failure says what broke,
why the rule exists, how to fix it and which section to read on latest main.
The fragment, chain and marker checks are the existing scripts, run as they are.
stdlib-only. Run: python3 scripts/check_conventions.py [--base REF | --staged]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_BLOB = "https://github.com/pymc-labs/daimon/blob/main"
VERSIONS = "packages/core/alembic/versions/"
HEAD_FILE = "packages/core/alembic/head.txt"
GENERATED = ("docs/configuration.md", "docs/features.md", "docs/mcp-tools.md")


@dataclass(frozen=True)
class Rule:
    what: str
    why: str
    fix: str
    read: str  # "CLAUDE.md" or "CONTRIBUTING.md", then the section

    def text(self, detail: str = "") -> str:
        doc, _, section = self.read.partition(" ")
        slug = section.strip('"').lower().replace(" ", "-")
        head = f"{self.what} ({detail})" if detail else self.what
        return (
            f"{head}\nWhy: {self.why}\nFix: {self.fix}\n"
            f"Read: {doc} section {section} on latest main: {_BLOB}/{doc}#{slug}"
        )


RULES = {
    "changelog-edit": Rule(
        "CHANGELOG.md is edited directly.",
        "two branches that both added a line there conflicted on every merge.",
        "restore it (git checkout <base> -- CHANGELOG.md) and put the entry in "
        "changes/<topic>.md as one '- **Bold lead.**' paragraph; only "
        "`scripts/changelog.py fold` edits CHANGELOG.md, deleting the fragments it folds.",
        'CLAUDE.md "Changelog"',
    ),
    "generated-page": Rule(
        "A generated docs page is committed.",
        "the docs workflow builds it from the code, so a committed copy goes stale "
        "and conflicts between branches.",
        "git rm --cached {path}; it is gitignored. Change the setting description, "
        "tool docstring or feature registry instead.",
        'CLAUDE.md "Documentation"',
    ),
    "snapshot-ambr": Rule(
        "An .ambr snapshot is committed under the MCP tests.",
        "each MCP tool schema is its own JSON file under __snapshots__/ so two changes "
        "to different tools never conflict; one shared .ambr conflicted on every merge.",
        "git rm {path}, then regenerate: uv run pytest "
        "packages/adapters/mcp/tests/test_tool_schema_snapshot.py --snapshot-update "
        "(without -n auto) and commit the JSON files.",
        'CONTRIBUTING.md "Quality gates"',
    ),
    "orm-contract": Rule(
        "pyproject.toml lists the ORM import-linter contract again.",
        "the enumerated source_modules list conflicted whenever two branches added a "
        "core module; the rule is derived from the import graph instead.",
        "delete the contract (forbidden_modules daimon.core._models) from pyproject.toml; "
        "packages/core/tests/test_orm_import_contract.py enforces it with no list to edit.",
        'CONTRIBUTING.md "Repo tour"',
    ),
    "migration-edit": Rule(
        "A migration that is on the base branch is edited, renamed or deleted.",
        "deployed databases already ran it, so changing it makes the chain diverge from "
        "what production applied.",
        "restore it (git checkout <base> -- {path}); fix forward with a new migration "
        "and run scripts/rechain_migrations.py.",
        'CLAUDE.md "Migrations"',
    ),
    "changelog-fragment": Rule(
        "A changelog fragment in changes/ is malformed.",
        "`scripts/changelog.py fold` splices fragments into CHANGELOG.md verbatim, so a "
        "malformed one corrupts the release notes.",
        "make it one '- **Bold lead.**' paragraph, no blank lines, wrapped lines "
        "indented two spaces, ending in a newline; re-run: python3 scripts/changelog.py check",
        'CLAUDE.md "Changelog"',
    ),
    "migration-chain": Rule(
        "The migration chain or head.txt is stale or broken.",
        "migrations form one linear chain and head.txt names its head, so two branches "
        "that add one conflict in git instead of breaking main at deploy.",
        "merge main, run scripts/rechain_migrations.py (add --base upstream/main from a "
        "fork) and commit the result; if head.txt conflicts take main's copy, finish the "
        "merge, then run it.",
        'CLAUDE.md "Migrations"',
    ),
    "migration-markers": Rule(
        "A migration misdeclares its 'downgrade:' or 'schema:' marker.",
        "the markers say whether a deploy can roll back and whether old and new code "
        "both run on the schema (expand) or the change must ship alone (contract).",
        "set both docstring lines to the true value (alembic writes TODO-declare "
        "placeholders); re-run: python3 scripts/lint_migrations.py",
        'CLAUDE.md "Migrations"',
    ),
}

SCRIPTS = (
    ("changelog-fragment", ["scripts/changelog.py", "check"], "changes"),
    ("migration-chain", ["scripts/rechain_migrations.py", "--check"], HEAD_FILE),
    ("migration-markers", ["scripts/lint_migrations.py"], VERSIONS.rstrip("/")),
)


@dataclass(frozen=True)
class Violation:
    rule: str
    file: str
    detail: str = ""
    output: str = ""

    def message(self) -> str:
        path = self.file
        text = RULES[self.rule].text(self.detail).replace("{path}", path)
        return f"{text}\nOutput:\n{self.output}" if self.output else text


def _git(*args: str, root: Path) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if done.returncode:
        sys.exit(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def changed(root: Path, base: str | None, staged: bool) -> list[tuple[str, str]]:
    """(status, path) per file the change touches; renames count as delete + add."""
    if staged:
        args = ["diff", "--cached"]
    elif base:
        args = ["diff", base, "HEAD"]
    else:
        return []
    out = _git(*args, "--name-status", "--no-renames", root=root)
    return [(line[0], line[2:]) for line in out.splitlines() if line]


def check_diff(files: list[tuple[str, str]], *, history: bool) -> list[Violation]:
    found: list[Violation] = []
    paths = {path for _, path in files}
    folded = any(
        s == "D" and p.startswith("changes/") and p.endswith(".md") and p != "changes/README.md"
        for s, p in files
    )
    if "CHANGELOG.md" in paths and not folded:
        found.append(Violation("changelog-edit", "CHANGELOG.md"))
    if history:  # a staged edit may be to a migration this branch added
        found += [
            Violation("migration-edit", p)
            for s, p in files
            if s in "MD" and p.startswith(VERSIONS) and p.endswith(".py")
        ]
    return found


def check_tree(root: Path) -> list[Violation]:
    tracked = _git("ls-files", root=root).splitlines()
    found = [Violation("generated-page", p) for p in tracked if p in GENERATED]
    found += [
        Violation("snapshot-ambr", p)
        for p in tracked
        if p.startswith("packages/adapters/mcp/") and p.endswith(".ambr")
    ]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    contracts = config.get("tool", {}).get("importlinter", {}).get("contracts", [])
    found += [
        Violation("orm-contract", "pyproject.toml", detail=f"contract {c.get('name')!r}")
        for c in contracts
        if "daimon.core._models" in c.get("forbidden_modules", [])
    ]
    return found


def _locate(root: Path, output: str, default: str) -> str:
    """The first file the script's output names, else `default`."""
    for line in output.splitlines():
        name = re.split(r":\d+:|: ", line, maxsplit=1)[0].strip()
        for candidate in (name, VERSIONS + name):
            if name and (root / candidate).is_file():
                return candidate
    return default


def check_scripts(root: Path) -> list[Violation]:
    found: list[Violation] = []
    for rule, command, default in SCRIPTS:
        done = subprocess.run([sys.executable, *command], cwd=root, capture_output=True, text=True)
        if done.returncode:
            output = "\n".join((done.stdout + done.stderr).strip().splitlines()[:40])
            found.append(Violation(rule, _locate(root, output, default), output=output))
    return found


def _escape(text: str, *, prop: bool = False) -> str:
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if prop else text


def report(violations: list[Violation]) -> None:
    actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for v in violations:
        if actions:
            title = _escape(f"conventions: {v.rule}", prop=True)
            print(
                f"::error file={_escape(v.file, prop=True)},title={title}::{_escape(v.message())}"
            )
        else:
            print(f"error: {v.file}: {v.message()}\n", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    where = parser.add_mutually_exclusive_group()
    where.add_argument(
        "--base", help="ref the change is measured against (CI: HEAD^1 of the merge ref)"
    )
    where.add_argument(
        "--staged", action="store_true", help="measure the staged change (pre-commit)"
    )
    args = parser.parse_args(argv)
    files = changed(ROOT, args.base, args.staged)
    violations = [
        *check_diff(files, history=bool(args.base)),
        *check_tree(ROOT),
        *check_scripts(ROOT),
    ]
    report(violations)
    if violations:
        print(
            f"conventions: {len(violations)} violation(s); read each message and its section.",
            file=sys.stderr,
        )
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
