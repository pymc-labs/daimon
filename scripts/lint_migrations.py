"""AST lint over Alembic migration markers.

Every file under ``packages/core/alembic/versions/`` must declare a
``downgrade: safe|destructive|unsupported`` marker line inside its module
docstring, and the declared value is cross-checked by AST against the shape
of the migration's ``downgrade()`` body. Migrations numbered ``_SCHEMA_FROM`` or
above also declare ``schema: expand|contract``, checked against what ``upgrade()``
does: ``expand`` only adds, so old and new code both run on it. ``--classify FILE...``
prints each file's class. stdlib-only; CI-shaped (default path:
packages/core/alembic/versions).
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
_SCHEMA_RE = re.compile(r"^schema:\s*(?P<value>.+?)\s*$")
_VALID_SCHEMA_VALUES = {"expand", "contract"}
# Main's head was 0221 when `schema:` became required; older files stay untouched.
_SCHEMA_FROM = 233
_NUMBERED_RE = re.compile(r"^(\d+)[a-z]?_")


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    rule: str
    message: str

    def format(self) -> str:
        return f"{self.file}:{self.line}: {self.rule}: {self.message}"


def _find_markers(
    docstring_node: ast.Constant, docstring: str, pattern: re.Pattern[str] = _MARKER_RE
) -> list[tuple[int, str]]:
    """Return (line, value) for every marker-shaped line in the docstring."""
    matches: list[tuple[int, str]] = []
    for offset, line in enumerate(docstring.splitlines()):
        m = pattern.match(line.strip())
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


def _is_merge_revision(tree: ast.Module) -> bool:
    """True when the module's ``down_revision`` is a tuple of two or more parents.

    An Alembic merge revision only joins branches: it has no schema change of
    its own, so its downgrade (back to both parents) is a safe no-op.
    """
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name, value = node.targets[0].id, node.value
        else:
            continue
        if name == "down_revision":
            return isinstance(value, ast.Tuple) and len(value.elts) >= 2
    return False


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


_DOLLAR_QUOTED = re.compile(r"\$(\w*)\$.*?\$\1\$", re.DOTALL)
# One pass, so a quote inside a comment and `--` inside a string are both read right.
_STRING_OR_COMMENT = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.DOTALL)
_LOCK_TIMEOUT = re.compile(r"SET\s+LOCAL\s+lock_timeout\b", re.IGNORECASE)
# Replacing a function changes what old code calls, so only a new one is expand.
_PLAIN_EXPAND_SQL = re.compile(
    r"(CREATE\s+(FUNCTION|(OR\s+REPLACE\s+)?VIEW)|GRANT|COMMENT\s+ON)\b", re.IGNORECASE
)
_INDEX_SQL = re.compile(r"CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\b", re.IGNORECASE)
_BEHAVIOUR_SQL = re.compile(
    r"CREATE\s+(OR\s+REPLACE\s+)?(CONSTRAINT\s+)?TRIGGER\b|CREATE\s+POLICY\b", re.IGNORECASE
)
_ON_TABLE = re.compile(r"\bON\s+(?:ONLY\s+)?(?:\w+\.)?\"?(?P<table>\w+)\"?", re.IGNORECASE)
_ENABLE_RLS = re.compile(
    r"ALTER\s+TABLE\s+(ONLY\s+)?\"?(?P<table>\w+)\"?\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY\b",
    re.IGNORECASE,
)
_EXECUTE_ATTRS = {"execute", "exec_driver_sql"}
_OBJECT_DDL_ATTRS = {"create", "drop", "create_all", "drop_all"}
_TRUSTED_ROOTS = {"sqlalchemy", "alembic", *sys.stdlib_module_names}
# op.<name> -> index of the table argument, for constraints that are expand on a new table.
_CONSTRAINT_OPS = {
    "create_foreign_key": 1,
    "create_check_constraint": 1,
    "create_unique_constraint": 1,
    "create_primary_key": 1,
}
_NEUTRAL_OPS = {"get_bind", "get_context", "f", "inline_literal"}
_FOREIGN_KEYS = {"ForeignKey", "ForeignKeyConstraint"}
# Methods that only read or build values; any other call on a local object (an ORM
# session, a table) may write, so it makes the migration contract.
_READ_METHODS = {
    "all", "append", "bindparams", "first", "fetchall", "fetchone", "format", "get", "items",
    "join", "keys", "lower", "mappings", "one", "scalar", "scalar_one", "scalar_one_or_none",
    "scalars", "split", "strip", "upper", "values",
}  # fmt: skip


def _sql_statements(sql: str) -> list[str]:
    """Split a SQL literal into statements, ignoring quoted bodies and comments."""
    stripped = _STRING_OR_COMMENT.sub(
        lambda m: "''" if m.group(0).startswith("'") else " ", _DOLLAR_QUOTED.sub("$$ $$", sql)
    )
    return [part.strip() for part in stripped.split(";") if part.strip()]


class _Classifier:
    """Walks upgrade() in execution order, following calls to functions in the file."""

    def __init__(self, tree: ast.Module) -> None:
        self.funcs: dict[str, ast.FunctionDef] = {}
        self.consts: dict[str, list[str]] = {}
        self.foreign: set[str] = set()
        self.modules: set[str] = set()  # names bound by a trusted import
        self.ops: set[str] = {"op"}  # alembic's op, under any alias
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                self.funcs[node.name] = node
            elif isinstance(node, ast.Import | ast.ImportFrom):
                module = getattr(node, "module", None)
                for alias in node.names:
                    root = (module or alias.name).split(".")[0]
                    bound = (alias.asname or alias.name).split(".")[0]
                    if root not in _TRUSTED_ROOTS or (
                        isinstance(node, ast.ImportFrom) and node.level
                    ):
                        self.foreign.add(bound)
                    elif module == "alembic" and alias.name == "op":
                        self.ops.add(bound)
                    else:
                        self.modules.add(bound)
            elif isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
                target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                values = self._literal_strings(node.value)
                if isinstance(target, ast.Name) and values is not None:
                    self.consts[target.id] = values
        self.new_tables: set[str] = set()
        self.locked = False
        self.reasons: list[tuple[int, str]] = []
        self.active: list[str] = []

    @staticmethod
    def _literal_strings(node: ast.expr) -> list[str] | None:
        try:
            value = ast.literal_eval(node)
        except (ValueError, SyntaxError):
            return None
        if isinstance(value, str):
            return [value]
        if isinstance(value, tuple | list) and all(isinstance(v, str) for v in value):  # pyright: ignore[reportUnknownVariableType]
            return list(value)  # pyright: ignore[reportUnknownArgumentType]
        return None

    def _resolve(self, node: ast.expr | None, env: dict[str, list[str]]) -> list[str] | None:
        """Every string `node` can be, or None when it is not a literal."""
        if node is None:
            return None
        if isinstance(node, ast.Name):
            return env.get(node.id, self.consts.get(node.id))
        if isinstance(node, ast.Call) and len(node.args) == 1:
            callee = node.func.attr if isinstance(node.func, ast.Attribute) else None
            if callee == "text" or (isinstance(node.func, ast.Name) and node.func.id == "text"):
                return self._resolve(node.args[0], env)
        return self._literal_strings(node)

    def run(self, tree: ast.Module) -> list[tuple[int, str]]:
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
                self._visit_all(node.body, {})
        return self.reasons

    def _visit_all(self, nodes: list[ast.stmt], env: dict[str, list[str]]) -> None:
        for node in nodes:
            self._visit(node, env)

    def _visit(self, node: ast.AST, env: dict[str, list[str]]) -> None:
        if isinstance(node, ast.FunctionDef):
            self.funcs[node.name] = node  # a helper defined inside upgrade()
            return
        if isinstance(node, ast.Call):
            for child in ast.iter_child_nodes(node):
                self._visit(child, env)
            self._call(node, env)
            return
        if isinstance(node, ast.For):
            self._visit(node.iter, env)
            values = self._resolve(node.iter, env)
            if isinstance(node.target, ast.Name):
                if values is None:
                    env.pop(node.target.id, None)
                else:
                    env[node.target.id] = values
            self._visit_all(node.body + node.orelse, env)
            return
        for child in ast.iter_child_nodes(node):
            self._visit(child, env)
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name):
                values = self._resolve(node.value, env)
                if values is None:
                    env.pop(target.id, None)
                else:
                    env[target.id] = values

    def _bad(self, node: ast.AST, why: str) -> None:
        self.reasons.append((getattr(node, "lineno", 1), why))

    def _call(self, node: ast.Call, env: dict[str, list[str]]) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in _EXECUTE_ATTRS:
                self._execute(node, env)
            elif isinstance(func.value, ast.Name) and func.value.id in self.ops:
                self._op(func.attr, node, env)
            elif func.attr in _OBJECT_DDL_ATTRS:
                self._bad(node, f"`.{func.attr}()` changes the schema")
            else:
                root = func
                while isinstance(root, ast.Attribute):
                    root = root.value  # pyright: ignore[reportAssignmentType]
                if isinstance(root, ast.Name) and root.id in self.foreign:
                    self._bad(node, f"calls into `{root.id}`, which this file does not define")
                elif not (
                    func.attr in _READ_METHODS
                    or (isinstance(root, ast.Name) and root.id in self.modules)
                    or (isinstance(root, ast.Constant) and isinstance(root.value, str))
                ):
                    self._bad(node, f"calls `.{func.attr}()`, which may write")
        elif isinstance(func, ast.Name):
            if func.id in self.funcs and func.id not in self.active:
                self._inline(self.funcs[func.id], node, env)
            elif func.id in self.foreign:
                self._bad(node, f"calls `{func.id}`, which this file does not define")

    def _inline(self, func: ast.FunctionDef, call: ast.Call, env: dict[str, list[str]]) -> None:
        local: dict[str, list[str]] = {}
        for param, arg in zip(func.args.args, call.args, strict=False):
            values = self._resolve(arg, env)
            if values is not None:
                local[param.arg] = values
        self.active.append(func.name)
        self._visit_all(func.body, local)
        self.active.pop()

    def _arg(self, node: ast.Call, index: int, keyword: str) -> ast.expr | None:
        if len(node.args) > index:
            return node.args[index]
        return next((k.value for k in node.keywords if k.arg == keyword), None)

    def _table(
        self, node: ast.Call, index: int, keyword: str, env: dict[str, list[str]]
    ) -> str | None:
        names = self._resolve(self._arg(node, index, keyword), env)
        return names[0] if names and len(names) == 1 else None

    def _op(self, name: str, node: ast.Call, env: dict[str, list[str]]) -> None:
        if name in _NEUTRAL_OPS:
            return
        if name == "create_table":
            if (created := self._table(node, 0, "table_name", env)) is not None:
                self.new_tables.add(created)
            self._foreign_keys(node, env)
        elif name == "add_column":
            self._add_column(node, env)
        elif name == "create_index":
            table = self._table(node, 1, "table_name", env)
            if table not in self.new_tables:
                if not self._true(node, "postgresql_concurrently"):
                    self._bad(node, "op.create_index on an existing table without concurrently")
                if self._true(node, "unique"):
                    self._bad(node, "a unique index on an existing table rejects old writes")
        elif name in _CONSTRAINT_OPS:
            table = self._table(node, _CONSTRAINT_OPS[name], "source_table", env)
            if table not in self.new_tables:
                self._bad(node, f"op.{name} on a table this file did not create")
            if name == "create_foreign_key":
                referent = self._table(node, 2, "referent_table", env)
                self._lock_referent(node, referent)
        else:
            self._bad(node, f"op.{name} is not an additive change")

    @staticmethod
    def _true(node: ast.Call, keyword: str) -> bool:
        return any(
            k.arg == keyword and isinstance(k.value, ast.Constant) and k.value.value is True
            for k in node.keywords
        )

    def _lock_referent(self, node: ast.AST, referent: str | None) -> None:
        """A foreign key locks the table it references (SHARE ROW EXCLUSIVE)."""
        if referent not in self.new_tables and not self.locked:
            self._bad(node, "a foreign key to an existing table before SET LOCAL lock_timeout")

    def _foreign_keys(self, node: ast.Call, env: dict[str, list[str]]) -> None:
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name not in _FOREIGN_KEYS:
                continue
            index, keyword = (0, "column") if name == "ForeignKey" else (1, "refcolumns")
            targets = self._resolve(self._arg(call, index, keyword), env) or [""]
            for target in targets:
                self._lock_referent(call, target.split(".")[-2] if "." in target else None)

    def _add_column(self, node: ast.Call, env: dict[str, list[str]]) -> None:
        table = self._table(node, 0, "table_name", env)
        self._foreign_keys(node, env)
        if table in self.new_tables:
            return
        column = self._arg(node, 1, "column")
        if not self.locked:
            self._bad(node, "op.add_column touches an existing table before SET LOCAL lock_timeout")
        if not isinstance(column, ast.Call):
            self._bad(node, "op.add_column with a column that is not written inline")
            return
        keywords = {k.arg: k.value for k in column.keywords}
        nullable = keywords.get("nullable")
        not_null = nullable is not None and not (
            isinstance(nullable, ast.Constant) and nullable.value is True
        )
        primary = keywords.get("primary_key")
        not_null = not_null or (
            nullable is None and isinstance(primary, ast.Constant) and primary.value is True
        )
        if not_null and "server_default" not in keywords:
            self._bad(node, "op.add_column adds a NOT NULL column without a server_default")

    def _execute(self, node: ast.Call, env: dict[str, list[str]]) -> None:
        statements = self._resolve(self._arg(node, 0, "statement"), env)
        if statements is None:
            self._bad(node, "execute runs SQL that is not a literal")
            return
        for sql in statements:
            for statement in _sql_statements(sql):
                self._statement(node, statement)

    def _statement(self, node: ast.Call, statement: str) -> None:
        if _LOCK_TIMEOUT.match(statement):
            self.locked = True
        elif _PLAIN_EXPAND_SQL.match(statement):
            pass
        elif (index := _INDEX_SQL.match(statement)) or _BEHAVIOUR_SQL.match(statement):
            on = _ON_TABLE.search(statement)
            if on is not None and on.group("table") in self.new_tables:
                return
            if index is None:
                self._bad(node, "a trigger or policy on an existing table changes old code")
            elif index.group("unique"):
                self._bad(node, "a unique index on an existing table rejects old writes")
            elif "CONCURRENTLY" not in statement.upper():
                self._bad(node, "CREATE INDEX on an existing table without CONCURRENTLY")
        elif not ((m := _ENABLE_RLS.match(statement)) and m.group("table") in self.new_tables):
            self._bad(node, f"execute runs `{statement[:40]}`, which is not additive")


def contract_reasons(tree: ast.Module) -> list[tuple[int, str]]:
    """(line, why) for each statement in upgrade() that is not expand-safe."""
    return _Classifier(tree).run(tree)


def schema_class(tree: ast.Module) -> str:
    """`expand` when upgrade() only adds (safe beside old and new code), else `contract`."""
    return "contract" if contract_reasons(tree) else "expand"


def _schema_findings(
    path: Path, tree: ast.Module, docstring_node: ast.Constant | None, docstring: str | None
) -> list[Finding]:
    file = str(path)
    first = docstring_node.lineno if docstring_node is not None else 1
    markers: list[tuple[int, str]] = []
    if docstring_node is not None and docstring is not None:
        markers = _find_markers(docstring_node, docstring, _SCHEMA_RE)
    if not markers:
        numbered = _NUMBERED_RE.match(path.name)
        if numbered and int(numbered.group(1)) >= _SCHEMA_FROM:
            return [
                Finding(
                    file,
                    first,
                    "migrations/missing-schema",
                    "no `schema: expand|contract` marker found in the module docstring",
                )
            ]
        return []
    if len(markers) > 1:
        return [
            Finding(file, line, "migrations/duplicate-schema", "more than one `schema:` marker")
            for line, _ in markers
        ]
    line, value = markers[0]
    if value not in _VALID_SCHEMA_VALUES:
        return [
            Finding(
                file, line, "migrations/invalid-schema", f"`schema: {value}` is not expand|contract"
            )
        ]
    if value == "contract":
        return []
    return [
        Finding(file, at, "migrations/expand-not-expand", f"declares `schema: expand` but {why}")
        for at, why in contract_reasons(tree)
    ]


def classify_file(path: Path) -> str:
    """`expand` or `contract` for a migration file; an unreadable one is `contract`."""
    try:
        return schema_class(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    except (OSError, SyntaxError):
        return "contract"


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
        elif marker_value == "safe" and shape == "trivial" and _is_merge_revision(tree):
            pass  # a merge revision's no-op downgrade is a real reversal
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

    findings.extend(_schema_findings(path, tree, docstring_value, docstring))
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
    parser.add_argument(
        "--classify",
        nargs="+",
        metavar="FILE",
        help="Print `file: expand|contract` for each migration file and exit.",
    )
    args = parser.parse_args(argv)
    if args.classify:
        for name in args.classify:
            print(f"{name}: {classify_file(Path(name))}")
        return 0
    findings = lint_tree(Path(args.path))
    for f in findings:
        print(f.format(), file=sys.stderr)
    if findings:
        print(f"\n{len(findings)} failing finding(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
