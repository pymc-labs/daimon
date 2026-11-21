"""CEL expressions inside `${{ }}` templates: parsing, references and bounded evaluation.

A string holding one expression and nothing else keeps the value's type; text around
expressions makes a string. Evaluation is bounded three ways, since authors (and the
agents that edit drafts) write expressions that run unattended: source length, tree
depth (checked after parsing, before any recursion over the tree) and a step budget
shared with the sub-evaluators macros create. cel-python has no cost tracking of its
own, so the budget lives in `_BoundedEvaluator`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Final, override

import celpy
import lark
from celpy import celtypes
from celpy.adapter import json_to_cel
from celpy.celparser import CELParseError
from celpy.evaluation import (
    Activation,
    CELEvalError,
    CELFunction,
    CELUnsupportedError,
    Evaluator,
    Result,
)
from pydantic import JsonValue

MAX_SOURCE: Final = 4096
MAX_DEPTH: Final = 200
MAX_STEPS: Final = 50_000
MAX_STRING: Final = 1 << 20
#: Names an expression may start from. `payload` (trigger mappings) and `event` (event
#: matches) exist only where the spec says.
NAMESPACES: Final = frozenset(
    {"inputs", "trigger", "steps", "item", "index", "run", "workflow", "payload", "event"}
)

_EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


class ExpressionError(ValueError):
    """An expression that does not parse, exceeds a bound or fails to evaluate."""


@dataclass(frozen=True)
class Ref:
    """A dotted path an expression reads, such as `("steps", "draft", "output", "body")`."""

    path: tuple[str, ...]
    guarded: bool = False


def expressions_in(text: str) -> list[str]:
    """The CEL sources inside a template string's `${{ }}` markers."""
    return [m.group(1).strip() for m in _EXPR.finditer(text)]


def is_single_expression(text: str) -> bool:
    match = _EXPR.fullmatch(text.strip())
    return match is not None and "${{" not in match.group(1)


@cache
def _environment() -> celpy.Environment:
    # Environment() raises the process recursion limit to 2500 once; one shared copy.
    return celpy.Environment()


@dataclass(frozen=True)
class _Node:
    """A parse tree node with plain types: rule name, then child nodes or token text."""

    data: str
    children: tuple[_Node | str, ...]

    def walk(self) -> list[_Node]:
        """This node and every node under it, outermost first."""
        out: list[_Node] = [self]
        for child in self.children:
            if isinstance(child, _Node):
                out.extend(child.walk())
        return out


def _plain(tree: lark.Tree[str]) -> _Node:
    return _Node(
        str(tree.data),
        tuple(_plain(c) if isinstance(c, lark.Tree) else str(c) for c in tree.children),
    )


def _depth(node: _Node) -> int:
    return 1 + max((_depth(c) for c in node.children if isinstance(c, _Node)), default=0)


@dataclass(frozen=True)
class _Compiled:
    tree: lark.Tree[Result]
    syntax: _Node


@cache
def _compile(source: str) -> _Compiled:
    if len(source) > MAX_SOURCE:
        raise ExpressionError(f"Expression is longer than {MAX_SOURCE} characters")
    try:
        # cel-python types its trees `lark.Tree` with no leaf type; leaves are tokens.
        tree = _environment().compile(source)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        syntax = _plain(tree)  # pyright: ignore[reportUnknownArgumentType]
        too_deep = _depth(syntax) > MAX_DEPTH
    except CELParseError as err:
        first_line = str(err.args[0]).splitlines()[0]
        raise ExpressionError(f"Expression does not parse: {first_line}") from err
    except RecursionError as err:
        raise ExpressionError("Expression is nested too deeply") from err
    if too_deep:
        raise ExpressionError("Expression is nested too deeply")
    return _Compiled(tree, syntax)  # pyright: ignore[reportUnknownArgumentType]


def check_expression(source: str) -> None:
    """Raise `ExpressionError` when `source` does not parse or breaks a static bound."""
    _compile(source)


def check_template(text: str) -> None:
    """Raise `ExpressionError` for the first expression in `text` that does not compile."""
    for source in expressions_in(text):
        _compile(source)


def _path(member: _Node) -> tuple[str, ...] | None:
    """The dotted path of a `member` chain rooted at a namespace, or None.

    A string-literal index (`x['k']`) extends the path; a computed one ends it there.
    """
    names: list[str] = []
    node = member
    while True:
        head = node.children[0] if node.children else None
        if not isinstance(head, _Node):
            return None
        if node.data == "member" and len(node.children) == 1:
            node = head
        elif node.data in ("member_dot", "member_index"):
            key = node.children[1]
            name = str(key) if node.data == "member_dot" else _string_literal(key)
            names = [] if name is None else [name, *names]
            node = head
        elif node.data == "primary" and head.data == "ident":
            root = str(head.children[0])
            return (root, *names) if root in NAMESPACES else None
        else:
            return None


def _string_literal(node: _Node | str) -> str | None:
    tokens = (
        [c for n in node.walk() for c in n.children if isinstance(c, str)]
        if isinstance(node, _Node)
        else [node]
    )
    text = tokens[0] if len(tokens) == 1 else ""
    return text[1:-1] if text[:1] in ("'", '"') else None


def expression_refs(source: str) -> list[Ref]:
    """Every namespace path the expression reads; paths under `has()` are guards."""
    refs: list[Ref] = []
    guarded: set[int] = set()
    consumed: set[int] = set()
    for node in _compile(source).syntax.walk():
        if node.data == "ident_arg" and node.children[0] == "has":
            guarded.update(id(n) for n in node.walk())
        if node.data != "member" or id(node) in consumed:
            continue
        path = _path(node)
        if path is not None:
            consumed.update(id(n) for n in node.walk())
            ref = Ref(path, guarded=id(node) in guarded)
            if ref not in refs:
                refs.append(ref)
    return refs


def template_refs(text: str) -> list[Ref]:
    return [ref for source in expressions_in(text) for ref in expression_refs(source)]


_NO_SEPARATOR: Final = celtypes.StringType("")


class _Budget:
    def __init__(self, steps: int) -> None:
        self.left = steps


class _BoundedEvaluator(Evaluator):
    def __init__(self, ast: lark.Tree[Result], activation: Activation, budget: _Budget) -> None:
        super().__init__(ast, activation=activation)  # pyright: ignore[reportUnknownMemberType]  # untyped lark.Tree
        self.budget = budget

    @override
    def sub_evaluator(self, ast: lark.Tree[Result]) -> Evaluator:
        return _BoundedEvaluator(ast, self.activation, self.budget)

    # lark calls `_visit_tree` for every node (`visit` only for the root), so the budget is here.
    @override
    def _visit_tree(self, tree: lark.Tree[Result]) -> Result:
        self.budget.left -= 1
        if self.budget.left < 0:
            raise ExpressionError("Expression is too costly to evaluate")
        value: Result = super()._visit_tree(tree)  # pyright: ignore[reportUnknownVariableType]  # lark.Interpreter generics
        if isinstance(value, celtypes.StringType | celtypes.BytesType) and len(value) > MAX_STRING:
            raise ExpressionError("Expression builds a value that is too large")
        return value  # pyright: ignore[reportUnknownVariableType]  # Result holds an untyped Tree


def _functions(now: datetime) -> dict[str, CELFunction]:
    def lower_ascii(value: celtypes.Value) -> celtypes.Value:
        return celtypes.StringType(str(value).lower())

    def upper_ascii(value: celtypes.Value) -> celtypes.Value:
        return celtypes.StringType(str(value).upper())

    def trim(value: celtypes.Value) -> celtypes.Value:
        return celtypes.StringType(str(value).strip())

    def join(values: celtypes.Value, separator: celtypes.Value = _NO_SEPARATOR) -> celtypes.Value:
        if not isinstance(values, list):
            raise TypeError("join takes a list")
        return celtypes.StringType(str(separator).join(str(v) for v in values))

    def current_time() -> celtypes.Value:
        return celtypes.TimestampType(now)

    return {
        "lowerAscii": lower_ascii,
        "upperAscii": upper_ascii,
        "trim": trim,
        "join": join,
        "now": current_time,
    }


def _to_json(value: object) -> JsonValue:
    match value:
        case celtypes.BoolType():
            return bool(value)
        case celtypes.TimestampType():
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        case celtypes.DurationType() | timedelta():
            return f"{value.total_seconds():g}s"
        case celtypes.NullType() | None:
            return None
        case celtypes.IntType() | celtypes.UintType():
            return int(value)
        case celtypes.DoubleType():
            return float(value)
        case celtypes.StringType():
            return str(value)
        case bytes():
            return value.decode("utf-8", errors="replace")
        case celtypes.MapType():
            return {str(k): _to_json(v) for k, v in value.items()}
        case celtypes.ListType():
            return [_to_json(v) for v in value]
        case _:
            raise ExpressionError(f"Expression gives a {type(value).__name__}, which is not JSON")


def evaluate(
    source: str, context: Mapping[str, JsonValue], *, now: datetime, steps: int = MAX_STEPS
) -> JsonValue:
    """Evaluate one CEL source against JSON `context`, within the step budget."""
    tree = _compile(source).tree
    environment = _environment()
    runner = celpy.InterpretedRunner(environment, tree, _functions(now))
    evaluator = _BoundedEvaluator(tree, runner.new_activation(), _Budget(steps))
    try:
        # Inside the try: a deeply nested payload overflows the converter, too.
        activation: dict[str, celtypes.Value] = {k: json_to_cel(v) for k, v in context.items()}
        return _to_json(evaluator.evaluate(activation))
    except (CELEvalError, CELUnsupportedError) as err:
        raise ExpressionError(f"Expression failed: {err.args[0] if err.args else err}") from err
    except RecursionError as err:
        raise ExpressionError("Expression is nested too deeply") from err


def render(
    value: JsonValue, context: Mapping[str, JsonValue], *, now: datetime, steps: int = MAX_STEPS
) -> JsonValue:
    """Evaluate every template string inside `value` (dicts and lists walked)."""
    if isinstance(value, dict):
        return {k: render(v, context, now=now, steps=steps) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, context, now=now, steps=steps) for v in value]
    if not isinstance(value, str) or "${{" not in value:
        return value
    if is_single_expression(value):
        return evaluate(expressions_in(value)[0], context, now=now, steps=steps)

    def piece(match: re.Match[str]) -> str:
        result = evaluate(match.group(1).strip(), context, now=now, steps=steps)
        return result if isinstance(result, str) else _text(result)

    return _EXPR.sub(piece, value)


def _text(value: JsonValue) -> str:
    return "" if value is None else json.dumps(value, separators=(",", ":"), ensure_ascii=False)
