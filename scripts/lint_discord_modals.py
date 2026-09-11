"""AST lint over discord.ui.Modal subclasses.

Catches Discord-API violations before they reach send_modal: TextInput labels
longer than 45 codepoints, Modals containing more than 5 top-level components,
and TextInputs missing a label kwarg.
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
MAX_TEXT_INPUT_LENGTH = 4000  # Discord rejects the whole modal with 50035 above this


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

# Every constructor Modal.add_item accepts as a top-level child. Matches
# discord.ui.modal.Modal.add_item's flat `len(self._children) >= 5` check —
# there is no separate cap per component type, and a bare top-level Select is
# a legal child in discord.py 2.7.1 (Modal.to_components auto-wraps it in an
# ActionRow exactly as it does a bare TextInput).
_MODAL_CHILD_NAMES = {
    "TextInput",
    "Label",
    "RadioGroup",
    "CheckboxGroup",
    "FileUpload",
    "TextDisplay",
    *_SELECT_NAMES,
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


def _is_modal_child(node: ast.expr) -> bool:
    """Match a call to any constructor ``Modal.add_item`` treats as a child:
    ``discord.ui.X(...)`` or ``X(...)`` where ``X`` is in ``_MODAL_CHILD_NAMES``.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _MODAL_CHILD_NAMES:
        return True
    return isinstance(func, ast.Name) and func.id in _MODAL_CHILD_NAMES


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


def _max_length_kwarg(call: ast.Call) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == "max_length":
            return kw.value
    return None


def _literal_int(node: ast.expr) -> int | None:
    """Return the int value if ``node`` is a literal int, else None.

    `bool` is an `int` subclass; exclude it so `max_length=True` is reported as
    non-literal rather than silently read as 1.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        if isinstance(node.value, bool):
            return None
        return node.value
    return None


def _module_int_constants(tree: ast.Module) -> dict[str, int]:
    """Module-level ``NAME = <int literal>`` bindings, for resolving a
    ``max_length=_SOME_CAP`` that is defined in the same file. An *imported*
    constant stays unresolvable on purpose — its value is not visible here, and
    that invisibility is what let a byte cap pose as a character cap."""
    constants: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        value = _literal_int(node.value)
        if value is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value
    return constants


def _collect_modal_children(cls: ast.ClassDef) -> list[ast.Call]:
    """Every top-level modal-child constructor call in the class body,
    counted once each.

    Matches ``Modal.add_item``'s flat ``len(self._children) >= 5`` check
    (discord/ui/modal.py): the count is over top-level children only,
    whatever their type. A candidate call nested inside another candidate's
    arguments — e.g. the ``Select`` inside ``Label(component=Select(...))`` —
    is *not* counted separately, because it is not a separate item passed to
    ``add_item``; it is consumed by the outer ``Label``, which itself counts
    as one child. ``ast.walk`` is depth-first and would find both calls with
    no way to tell they nest, so this walks with explicit ancestor tracking
    instead and skips any candidate whose nearest enclosing candidate has
    already been recorded.
    """
    candidates: list[ast.Call] = []

    def visit(node: ast.AST, ancestor_is_candidate: bool) -> None:
        is_candidate = isinstance(node, ast.Call) and _is_modal_child(node)
        if is_candidate and not ancestor_is_candidate:
            assert isinstance(node, ast.Call)
            candidates.append(node)
        for child in ast.iter_child_nodes(node):
            visit(child, ancestor_is_candidate or is_candidate)

    visit(cls, False)
    return candidates


def _walk_modal_class(
    cls: ast.ClassDef, source_path: str, int_constants: dict[str, int]
) -> Iterator[Finding]:
    """Yield findings for every top-level child / TextInput inside this
    Modal class.
    """
    text_inputs: list[tuple[ast.Call, int]] = [
        (node, getattr(node, "lineno", 0))
        for node in ast.walk(cls)
        if isinstance(node, ast.Call) and _is_text_input(node)
    ]

    # Component count — every top-level child, matching Modal.add_item.
    children = _collect_modal_children(cls)
    if len(children) > MAX_COMPONENTS_PER_MODAL:
        yield Finding(
            file=source_path,
            line=cls.lineno,
            rule="discord/too-many-components",
            message=(
                f"{cls.name}: has {len(children)} components — Discord limit is "
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

    # max_length per TextInput. An imported byte-cap constant reads as a
    # plausible character cap, which is how a 4096 reached send_modal and made
    # the modal unopenable; a non-literal is therefore reported, not trusted.
    for call, lineno in text_inputs:
        max_length_node = _max_length_kwarg(call)
        if max_length_node is None:
            continue
        max_length = _literal_int(max_length_node)
        if max_length is None and isinstance(max_length_node, ast.Name):
            max_length = int_constants.get(max_length_node.id)
        if max_length is None:
            yield Finding(
                file=source_path,
                line=lineno,
                rule="discord/non-literal-max-length",
                message=(
                    f"{cls.name}: TextInput max_length is not an int literal; lint "
                    f"cannot verify it against Discord's {MAX_TEXT_INPUT_LENGTH} cap. "
                    f"Inline the number."
                ),
            )
            continue
        if max_length > MAX_TEXT_INPUT_LENGTH:
            yield Finding(
                file=source_path,
                line=lineno,
                rule="discord/max-length-too-large",
                message=(
                    f"{cls.name}: TextInput max_length={max_length}; Discord rejects "
                    f"the whole modal with 50035 above {MAX_TEXT_INPUT_LENGTH}."
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
    int_constants = _module_int_constants(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(_is_modal_base(b) for b in node.bases):
            findings.extend(_walk_modal_class(node, str(path), int_constants))
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
        "discord/missing-label",
        "discord/max-length-too-large",
        "discord/non-literal-max-length",
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
