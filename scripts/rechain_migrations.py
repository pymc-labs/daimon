"""Keep Alembic migrations in one linear chain; renumber a branch's new ones onto main.

Default: the migrations present in the working tree but absent from `--base` are this
branch's. Number them after the base's newest one, in the order their own
`down_revision` links give, rewrite their ids (and every file the branch changed that
names them), and write `packages/core/alembic/head.txt`. Idempotent; never touches a
file that exists on the base.

`--check` needs no git and no database: one head, `head.txt` equal to it, file stem
equal to revision id, ids within `alembic_version`'s 32 characters, and every revision
after the preview merge with one parent that has a lower number. stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

VERSIONS = Path("packages/core/alembic/versions")
HEAD_FILE = Path("packages/core/alembic/head.txt")
MERGE = "0168_merge_main_preview"
MAX_ID = 32  # alembic_version.version_num is varchar(32)
_NUMBERED = re.compile(r"^(\d+)([a-z]?)_")

Links = dict[str, tuple[str, ...]]  # revision -> parents


def _links(source: str, name: str) -> tuple[str, tuple[str, ...]]:
    """The module's `revision` and `down_revision` (a str, None or tuple of str)."""
    values: dict[str, object] = {}
    for node in ast.parse(source, filename=name).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        else:
            continue
        if target in {"revision", "down_revision"} and value is not None:
            values[target] = ast.literal_eval(value)
    revision, down = values.get("revision"), values.get("down_revision")
    if not isinstance(revision, str):
        raise SystemExit(f"{name}: no string `revision`")
    if down is None:
        return revision, ()
    if isinstance(down, str):
        return revision, (down,)
    if isinstance(down, tuple) and all(isinstance(p, str) for p in down):
        return revision, tuple(down)
    raise SystemExit(f"{name}: unreadable `down_revision`")


def _number(stem: str) -> tuple[int, str]:
    m = _NUMBERED.match(stem)
    return (int(m.group(1)), m.group(2)) if m else (-1, "")


def check(root: Path) -> list[str]:
    errors: list[str] = []
    links: Links = {}
    stems: dict[str, str] = {}
    for path in sorted((root / VERSIONS).glob("*.py")):
        revision, parents = _links(path.read_text(encoding="utf-8"), str(path))
        links[revision], stems[revision] = parents, path.stem
        if len(revision) > MAX_ID:
            errors.append(f"{path.name}: id is over {MAX_ID} characters")
    parents_of_any = {p for parents in links.values() for p in parents}
    heads = sorted(set(links) - parents_of_any)
    if len(heads) != 1:
        return [*errors, f"expected one head, found {len(heads)}: {', '.join(heads)}"]
    declared = (root / HEAD_FILE).read_text(encoding="utf-8").strip()
    if declared != heads[0]:
        errors.append(f"{HEAD_FILE} says {declared!r} but the head is {heads[0]!r}")
    after = {r for r, parents in links.items() if MERGE in parents}
    queue = list(after)
    while queue:
        parent = queue.pop()
        for child in [c for c, parents in links.items() if parent in parents]:
            if child not in after:
                after.add(child)
                queue.append(child)
    for revision in sorted(after):
        if stems[revision] != revision:
            errors.append(f"{stems[revision]}: file stem differs from revision {revision!r}")
        parents = links[revision]
        if len(parents) != 1:
            errors.append(f"{stems[revision]}: needs exactly one parent after {MERGE}")
        elif _number(stems[parents[0]]) >= _number(stems[revision]):
            errors.append(f"{stems[revision]}: parent {parents[0]} is not numbered lower")
    return errors


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if done.returncode:
        raise SystemExit(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def _branch_chain(root: Path, base: str) -> tuple[str, list[Path]]:
    """The base's newest migration id, and this branch's new files in link order."""
    base_stems = [
        Path(p).stem
        for p in _git(root, "ls-tree", "--name-only", base, f"{VERSIONS}/").split()
        if p.endswith(".py")
    ]
    if not base_stems:
        raise SystemExit(f"{base} has no migrations under {VERSIONS}")
    # head.txt names the head exactly; a base from before it existed falls back to numbers.
    done = subprocess.run(
        ["git", "show", f"{base}:{HEAD_FILE}"], cwd=root, capture_output=True, text=True
    )
    base_head = done.stdout.strip() if done.returncode == 0 else ""
    if base_head not in base_stems:
        base_head = max(base_stems, key=lambda s: (_number(s), s))
    new = [p for p in sorted((root / VERSIONS).glob("*.py")) if p.stem not in base_stems]
    by_revision: dict[str, Path] = {}
    parent_of: dict[str, str] = {}
    for path in new:
        revision, parents = _links(path.read_text(encoding="utf-8"), path.name)
        if len(parents) != 1:
            raise SystemExit(f"{path.name}: merge and root revisions are not supported")
        by_revision[revision], parent_of[revision] = path, parents[0]
    roots = [r for r, parent in parent_of.items() if parent not in by_revision]
    if len(roots) > 1:
        raise SystemExit(f"new migrations branch: {', '.join(sorted(roots))}")
    ordered: list[Path] = []
    revision = roots[0] if roots else None
    while revision is not None:
        ordered.append(by_revision[revision])
        children = [r for r, parent in parent_of.items() if parent == revision]
        if len(children) > 1:
            raise SystemExit(f"new migrations branch after {revision}")
        revision = children[0] if children else None
    return base_head, ordered


def _changed_files(root: Path, base: str) -> list[Path]:
    fork = _git(root, "merge-base", base, "HEAD").strip()
    names = _git(root, "diff", "--name-only", fork).split()
    names += _git(root, "ls-files", "--others", "--exclude-standard").split()
    return [root / n for n in sorted(set(names)) if (root / n).is_file()]


def _replace(text: str, mapping: dict[str, str]) -> str:
    if not mapping:
        return text
    pattern = "|".join(re.escape(old) for old in sorted(mapping, key=len, reverse=True))
    return re.sub(
        rf"(?<![A-Za-z0-9_])(?:{pattern})(?![A-Za-z0-9_])", lambda m: mapping[m.group(0)], text
    )


def rechain(root: Path, base: str) -> dict[str, str]:
    base_head, files = _branch_chain(root, base)
    number = _number(base_head)[0]
    mapping: dict[str, str] = {}
    renames: list[tuple[Path, str, str, str]] = []  # file, old parent, new parent, new id
    parent = base_head
    for path in files:
        old, parents = _links(path.read_text(encoding="utf-8"), path.name)
        number += 1
        if "_" not in path.stem:
            raise SystemExit(f"{path.name}: no `<id>_<slug>` name to renumber")
        new = f"{number:04d}_{path.stem.split('_', 1)[1]}"
        if len(new) > MAX_ID:
            raise SystemExit(f"{new}: over {MAX_ID} characters; shorten the slug")
        target = path.with_name(f"{new}.py")
        if target != path and target.exists():  # checked before any file is written
            raise SystemExit(f"{path.name} -> {target.name}: that file exists; rename one slug")
        mapping[old] = new
        renames.append((path, parents[0], parent, new))
        parent = new
    for path, old_parent, new_parent, _ in renames:
        local = {**mapping, old_parent: new_parent}
        text = path.read_text(encoding="utf-8")
        path.write_text(_replace(text, local), encoding="utf-8")
    for path in _changed_files(root, base):
        if path.parent == root / VERSIONS and path.suffix == ".py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if (replaced := _replace(text, mapping)) != text:
            path.write_text(replaced, encoding="utf-8")
    for path, _, _, new in renames:
        target = path.with_name(f"{new}.py")
        if target == path:
            continue
        tracked = _git(root, "ls-files", str(path.relative_to(root))).strip()
        if tracked:
            _git(root, "mv", str(path.relative_to(root)), str(target.relative_to(root)))
        else:
            path.rename(target)
    (root / HEAD_FILE).write_text(f"{parent}\n", encoding="utf-8")
    return {old: new for old, new in mapping.items() if old != new}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--base", default="origin/main", help="ref holding the main chain")
    parser.add_argument("--check", action="store_true", help="validate the chain; no git, no DB")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    if args.check:
        errors = check(root)
        for error in errors:
            print(error, file=sys.stderr)
        return 1 if errors else 0
    for old, new in rechain(root, args.base).items():
        print(f"{old} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
