"""AST lint over Alembic migration downgrade-safety markers.

Every file under ``packages/core/alembic/versions/`` must declare a
``downgrade: safe|destructive|unsupported`` marker line inside its module
docstring, and the declared value is cross-checked by AST against the shape
of the migration's ``downgrade()`` body. stdlib-only; CI-shaped (default
path: packages/core/alembic/versions).
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_MARKER_RE = re.compile(r"^downgrade:\s*(?P<value>.+?)\s*$")
_VALID_MARKER_VALUES = {"safe", "destructive", "unsupported"}


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    rule: str
    message: str

    def format(self) -> str:
        return f"{self.file}:{self.line}: {self.rule}: {self.message}"


def _find_markers(docstring_node: ast.Constant, docstring: str) -> list[tuple[int, str]]:
    """Return (line, value) for every marker-shaped line in the docstring."""
    matches: list[tuple[int, str]] = []
    for offset, line in enumerate(docstring.splitlines()):
        m = _MARKER_RE.match(line.strip())
        if m:
            matches.append((docstring_node.lineno + offset, m.group("value")))
    return matches


def _is_raise_not_implemented(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.Raise) or stmt.exc is None:
        return False
    exc = stmt.exc
    if isinstance(exc, ast.Call):
        exc = exc.func
    return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"


def _is_trivial_stmt(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.Pass):
        return True
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Constant):
        return False
    return stmt.value.value is Ellipsis


def _body_without_docstring(func: ast.FunctionDef) -> list[ast.stmt]:
    body = func.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _classify_downgrade_body(func: ast.FunctionDef) -> str:
    """Classify the downgrade() body as 'raises', 'trivial', or 'real'."""
    body = _body_without_docstring(func)
    if not body:
        return "trivial"
    if len(body) == 1 and _is_raise_not_implemented(body[0]):
        return "raises"
    if all(_is_trivial_stmt(stmt) for stmt in body):
        return "trivial"
    return "real"


def lint_file(path: Path) -> list[Finding]:
    source_path = str(path)
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=source_path)
    except SyntaxError as e:
        return [
            Finding(
                file=source_path,
                line=e.lineno or 0,
                rule="syntax-error",
                message=str(e),
            )
        ]

    findings: list[Finding] = []

    docstring_node = tree.body[0] if tree.body and isinstance(tree.body[0], ast.Expr) else None
    docstring_value = (
        docstring_node.value
        if docstring_node is not None and isinstance(docstring_node.value, ast.Constant)
        else None
    )
    docstring: str | None = None
    if docstring_value is not None and isinstance(docstring_value.value, str):
        docstring = docstring_value.value

    markers: list[tuple[int, str]] = []
    if docstring is not None and docstring_value is not None:
        markers = _find_markers(docstring_value, docstring)

    marker_value: str | None = None
    if not markers:
        findings.append(
            Finding(
                file=source_path,
                line=docstring_value.lineno if docstring_value is not None else 1,
                rule="migrations/missing-marker",
                message=(
                    "no `downgrade: safe|destructive|unsupported` marker found "
                    "in the module docstring"
                ),
            )
        )
    elif len(markers) > 1:
        for line, _value in markers:
            findings.append(
                Finding(
                    file=source_path,
                    line=line,
                    rule="migrations/duplicate-marker",
                    message="more than one `downgrade:` marker line found",
                )
            )
    else:
        line, value = markers[0]
        if value not in _VALID_MARKER_VALUES:
            findings.append(
                Finding(
                    file=source_path,
                    line=line,
                    rule="migrations/invalid-marker",
                    message=f"`downgrade: {value}` is not one of safe|destructive|unsupported",
                )
            )
        else:
            marker_value = value

    downgrade_func: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            downgrade_func = node
            break

    if downgrade_func is None:
        findings.append(
            Finding(
                file=source_path,
                line=1,
                rule="migrations/missing-downgrade",
                message="no module-level `def downgrade` found",
            )
        )
    elif marker_value is not None:
        shape = _classify_downgrade_body(downgrade_func)
        if marker_value == "unsupported" and shape != "raises":
            findings.append(
                Finding(
                    file=source_path,
                    line=downgrade_func.lineno,
                    rule="migrations/marker-body-mismatch",
                    message=(
                        f"marker declares `unsupported` but downgrade() does not solely "
                        f"raise NotImplementedError (body is {shape})"
                    ),
                )
            )
        elif marker_value in {"safe", "destructive"} and shape != "real":
            findings.append(
                Finding(
                    file=source_path,
                    line=downgrade_func.lineno,
                    rule="migrations/marker-body-mismatch",
                    message=(
                        f"marker declares `{marker_value}` but downgrade() body is {shape}, "
                        f"not a real reversal"
                    ),
                )
            )

    return findings


def lint_tree(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for py in sorted(root.glob("*.py")):
        findings.extend(lint_file(py))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lint Alembic migrations for downgrade-safety markers.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="packages/core/alembic/versions",
        help="Root directory to walk (default: packages/core/alembic/versions).",
    )
    args = parser.parse_args(argv)
    findings = lint_tree(Path(args.path))
    for f in findings:
        print(f.format(), file=sys.stderr)
    if findings:
        print(f"\n{len(findings)} failing finding(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
