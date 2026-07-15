"""AST lint over discord.ui.Modal subclasses.

Catches Discord-API violations before they reach send_modal: TextInput labels
longer than 45 codepoints, Modals containing more than 5 components, Modals
containing Select-style components, and TextInputs missing a label kwarg.
stdlib-only; CI-shaped (default path: packages/adapters/discord/daimon).
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Hard limits set by the Discord API.
MAX_LABEL_CHARS = 45  # codepoints
MAX_LABEL_BYTES = 45  # defensive — some validators count bytes
MAX_COMPONENTS_PER_MODAL = 5


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    rule: str
    message: str

    def format(self) -> str:
        return f"{self.file}:{self.line}: {self.rule}: {self.message}"


_SELECT_NAMES = {
    "Select",
    "UserSelect",
    "RoleSelect",
    "ChannelSelect",
    "MentionableSelect",
}


def _is_modal_base(base: ast.expr) -> bool:
    """Match ``discord.ui.Modal`` or ``Modal`` in the bases tuple."""
    if isinstance(base, ast.Attribute) and base.attr == "Modal":
        return True
    return isinstance(base, ast.Name) and base.id == "Modal"


def _is_text_input(node: ast.expr) -> bool:
    """Match ``discord.ui.TextInput(...)`` or ``TextInput(...)`` call."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "TextInput":
        return True
    return isinstance(func, ast.Name) and func.id == "TextInput"


def _is_select(node: ast.expr) -> bool:
    """Match ``discord.ui.Select(...)`` or ``Select(...)`` call."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _SELECT_NAMES:
        return True
    return isinstance(func, ast.Name) and func.id in _SELECT_NAMES


def _label_kwarg(call: ast.Call) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == "label":
            return kw.value
    # positional label is index 0 for discord.ui.TextInput
    if call.args:
        return call.args[0]
    return None


def _literal_str(node: ast.expr) -> str | None:
    """Return the string value if ``node`` is a literal str, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _walk_modal_class(cls: ast.ClassDef, source_path: str) -> Iterator[Finding]:
    """Yield findings for every TextInput / Select inside this Modal class.

    Counts components as ``TextInput(...)`` constructor calls that appear
    inside the class body (either bare or assigned to ``self.<name>``).
    """
    text_inputs: list[tuple[ast.Call, int]] = []

    for node in ast.walk(cls):
        if isinstance(node, ast.Call):
            if _is_text_input(node):
                text_inputs.append((node, getattr(node, "lineno", 0)))
            elif _is_select(node):
                yield Finding(
                    file=source_path,
                    line=getattr(node, "lineno", 0),
                    rule="discord/no-select-in-modal",
                    message=(
                        f"{cls.name}: Discord modals cannot contain Select-style "
                        f"components — only TextInput. Use a follow-up View instead."
                    ),
                )

    # Component count
    if len(text_inputs) > MAX_COMPONENTS_PER_MODAL:
        yield Finding(
            file=source_path,
            line=cls.lineno,
            rule="discord/too-many-components",
            message=(
                f"{cls.name}: has {len(text_inputs)} TextInputs — Discord limit is "
                f"{MAX_COMPONENTS_PER_MODAL}."
            ),
        )

    # Label length per TextInput
    for call, lineno in text_inputs:
        label_node = _label_kwarg(call)
        if label_node is None:
            yield Finding(
                file=source_path,
                line=lineno,
                rule="discord/missing-label",
                message=f"{cls.name}: TextInput call has no `label=` kwarg",
            )
            continue
        label = _literal_str(label_node)
        if label is None:
            yield Finding(
                file=source_path,
                line=lineno,
                rule="discord/non-literal-label",
                message=(
                    f"{cls.name}: TextInput label is not a string literal; lint cannot "
                    f"verify length. Add `# noqa: discord-lint` if intentional."
                ),
            )
            continue
        n_chars = len(label)
        n_bytes = len(label.encode("utf-8"))
        if n_chars > MAX_LABEL_CHARS or n_bytes > MAX_LABEL_BYTES:
            yield Finding(
                file=source_path,
                line=lineno,
                rule="discord/label-too-long",
                message=(
                    f"{cls.name}: TextInput label={label!r} is "
                    f"{n_chars} codepoints / {n_bytes} bytes; Discord rejects > "
                    f"{MAX_LABEL_CHARS}."
                ),
            )


def lint_file(path: Path) -> list[Finding]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return [
            Finding(
                file=str(path),
                line=e.lineno or 0,
                rule="syntax-error",
                message=str(e),
            )
        ]
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(_is_modal_base(b) for b in node.bases):
            findings.extend(_walk_modal_class(node, str(path)))
    return findings


def lint_tree(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for py in root.rglob("*.py"):
        findings.extend(lint_file(py))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lint discord.ui.Modal subclasses for Discord-API violations.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="packages/adapters/discord/daimon",
        help="Root directory to walk (default: packages/adapters/discord/daimon).",
    )
    args = parser.parse_args(argv)
    findings = lint_tree(Path(args.path))
    failing_rules = {
        "discord/label-too-long",
        "discord/too-many-components",
        "discord/no-select-in-modal",
        "discord/missing-label",
        "syntax-error",
    }
    failures = [f for f in findings if f.rule in failing_rules]
    informational = [f for f in findings if f.rule not in failing_rules]
    for f in failures:
        print(f.format(), file=sys.stderr)
    if informational:
        print(f"# {len(informational)} informational finding(s) (not failures):", file=sys.stderr)
        for f in informational:
            print(f"#   {f.format()}", file=sys.stderr)
    if failures:
        print(f"\n{len(failures)} failing finding(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
