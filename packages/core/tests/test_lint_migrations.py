"""Table-driven tests for scripts/lint_migrations.py.

`scripts/` is not an importable package and pytest `testpaths` does not
include it, so the module is loaded by path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "lint_migrations.py"


def _load_lint_migrations() -> ModuleType:
    spec = importlib.util.spec_from_file_location("lint_migrations", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None, "failed to build module spec"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lint_migrations = _load_lint_migrations()


VALID_SAFE = '''"""a migration.

downgrade: safe
"""

from __future__ import annotations

from alembic import op


def upgrade() -> None:
    op.create_table("widgets")


def downgrade() -> None:
    op.drop_table("widgets")
'''

VALID_UNSUPPORTED = '''"""a migration.

downgrade: unsupported
"""

from __future__ import annotations


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError("no going back")
'''

MISSING_MARKER = '''"""a migration with no marker."""

from __future__ import annotations


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError("no going back")
'''

INVALID_MARKER = '''"""a migration.

downgrade: TODO-declare (safe|destructive|unsupported)
"""

from __future__ import annotations


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError("no going back")
'''

DUPLICATE_MARKER = '''"""a migration.

downgrade: safe
downgrade: destructive
"""

from __future__ import annotations

from alembic import op


def upgrade() -> None:
    op.create_table("widgets")


def downgrade() -> None:
    op.drop_table("widgets")
'''

UNSUPPORTED_WITH_REAL_BODY = '''"""a migration.

downgrade: unsupported
"""

from __future__ import annotations

from alembic import op


def upgrade() -> None:
    op.create_table("widgets")


def downgrade() -> None:
    op.drop_table("widgets")
'''

SAFE_WITH_RAISE_BODY = '''"""a migration.

downgrade: safe
"""

from __future__ import annotations


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError("oops")
'''

SAFE_WITH_TRIVIAL_BODY = '''"""a migration.

downgrade: safe
"""

from __future__ import annotations


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
'''

MISSING_DOWNGRADE = '''"""a migration.

downgrade: safe
"""

from __future__ import annotations

from alembic import op


def upgrade() -> None:
    op.create_table("widgets")
'''

SYNTAX_ERROR = "def upgrade(:\n    pass\n"

VALID_MERGE_SAFE = '''"""join two branches.

downgrade: safe
"""

from __future__ import annotations

revision: str = "0003_merge"
down_revision: tuple[str, str] = ("0002_a", "0002_b")


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
'''

MERGE_DESTRUCTIVE_WITH_TRIVIAL_BODY = VALID_MERGE_SAFE.replace(
    "downgrade: safe", "downgrade: destructive"
)

SINGLE_PARENT_SAFE_WITH_TRIVIAL_BODY = VALID_MERGE_SAFE.replace(
    'down_revision: tuple[str, str] = ("0002_a", "0002_b")', 'down_revision: str = "0002_a"'
)


@pytest.mark.parametrize(
    ("source", "expected_rule"),
    [
        (MISSING_MARKER, "migrations/missing-marker"),
        (INVALID_MARKER, "migrations/invalid-marker"),
        (DUPLICATE_MARKER, "migrations/duplicate-marker"),
        (UNSUPPORTED_WITH_REAL_BODY, "migrations/marker-body-mismatch"),
        (SAFE_WITH_RAISE_BODY, "migrations/marker-body-mismatch"),
        (SAFE_WITH_TRIVIAL_BODY, "migrations/marker-body-mismatch"),
        (SINGLE_PARENT_SAFE_WITH_TRIVIAL_BODY, "migrations/marker-body-mismatch"),
        (MERGE_DESTRUCTIVE_WITH_TRIVIAL_BODY, "migrations/marker-body-mismatch"),
        (MISSING_DOWNGRADE, "migrations/missing-downgrade"),
        (SYNTAX_ERROR, "syntax-error"),
    ],
)
def test_lint_file_flags_expected_rule(tmp_path: Path, source: str, expected_rule: str) -> None:
    migration_file = tmp_path / "0001_bad.py"
    migration_file.write_text(source, encoding="utf-8")

    findings = lint_migrations.lint_file(migration_file)

    rules = {f.rule for f in findings}
    assert expected_rule in rules, f"expected {expected_rule} in {rules} for source:\n{source}"


@pytest.mark.parametrize("source", [VALID_SAFE, VALID_UNSUPPORTED, VALID_MERGE_SAFE])
def test_lint_file_produces_no_findings_for_valid_marker(tmp_path: Path, source: str) -> None:
    migration_file = tmp_path / "0001_good.py"
    migration_file.write_text(source, encoding="utf-8")

    findings = lint_migrations.lint_file(migration_file)

    assert findings == [], f"expected zero findings, got {findings}"


def test_lint_tree_over_real_versions_directory_is_clean() -> None:
    versions_dir = _REPO_ROOT / "packages" / "core" / "alembic" / "versions"

    findings = lint_migrations.lint_tree(versions_dir)

    assert findings == [], f"real migrations must be clean, got {findings}"


def test_alembic_has_one_head() -> None:
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert all(len(revision.revision) <= 32 for revision in script.walk_revisions())


def test_github_migrations_follow_agent_avatars() -> None:
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    followup = script.get_revision("0076_github_connect_followup")
    assert followup is not None and followup.down_revision == "0075_pending_skill_add_one_open"
    notices = script.get_revision("0054_github_new_repo_notices")
    audit = script.get_revision("0055_github_token_audit_versions")
    teams_sites = script.get_revision("0056_teams_channel_sites")
    panel = script.get_revision("0066_github_panel_notices")
    access_requests = script.get_revision("0067_github_access_requests")
    assert notices is not None and notices.down_revision == "0053_agent_avatars"
    assert audit is not None and audit.down_revision == "0054_github_new_repo_notices"
    assert (
        teams_sites is not None and teams_sites.down_revision == "0055_github_token_audit_versions"
    )
    assert panel is not None and panel.down_revision == "0065_turn_card_recovery_failures"
    assert (
        access_requests is not None and access_requests.down_revision == "0066_github_panel_notices"
    )
    [head] = script.get_heads()
    chain = {rev.revision for rev in script.iterate_revisions(head, "base")}
    assert "0055_github_token_audit_versions" in chain, "on the line to the head"
    assert "0066_github_panel_notices" in chain, "on the line to the head"
    assert "0067_github_access_requests" in chain, "on the line to the head"
    refresh = script.get_revision("0057_github_mcp_session_refresh")
    assert refresh is not None and refresh.down_revision == "0056_teams_channel_sites"
    installation_kind = script.get_revision("0058_github_app_install_kind")
    assert installation_kind is not None
    assert installation_kind.down_revision == "0057_github_mcp_session_refresh"
    panel_audit = script.get_revision("0059_security_audit_agent_name")
    assert panel_audit is not None and panel_audit.down_revision == "0058_github_app_install_kind"
    operator_origin = script.get_revision("0062_github_connect_origin")
    assert operator_origin is not None
    assert operator_origin.down_revision == "0061_github_agent_connect"


def test_main_returns_zero_for_clean_real_tree(capsys: pytest.CaptureFixture[str]) -> None:
    versions_dir = _REPO_ROOT / "packages" / "core" / "alembic" / "versions"

    exit_code = lint_migrations.main([str(versions_dir)])

    assert exit_code == 0, "main() should return 0 against the clean real versions tree"


def test_main_returns_one_for_tree_with_violation(tmp_path: Path) -> None:
    (tmp_path / "0001_bad.py").write_text(MISSING_MARKER, encoding="utf-8")

    exit_code = lint_migrations.main([str(tmp_path)])

    assert exit_code == 1, "main() should return 1 when a violating fixture is present"


def test_finding_format_matches_file_line_rule_message_shape(tmp_path: Path) -> None:
    migration_file = tmp_path / "0001_bad.py"
    migration_file.write_text(MISSING_MARKER, encoding="utf-8")

    findings = lint_migrations.lint_file(migration_file)

    assert len(findings) == 1, "expected exactly one finding for a missing marker"
    formatted = findings[0].format()
    assert formatted.startswith(str(migration_file)), "format() should lead with the file path"
    assert "migrations/missing-marker" in formatted, "format() should include the rule id"


_VERSIONS = _REPO_ROOT / "packages" / "core" / "alembic" / "versions"


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("0221_routine_delivery_hold", "expand"),
        ("0220_discord_audience_facts", "expand"),
        # Replaces a trigger function 0142 created, which changes what old code runs.
        ("0217_monitored_events", "contract"),
        ("0216_usage_obs_observed_at", "contract"),
    ],
)
def test_classify_reads_the_shape_of_real_migrations(stem: str, expected: str) -> None:
    assert lint_migrations.classify_file(_VERSIONS / f"{stem}.py") == expected, stem


_LOCKED = "    op.execute(\"SET LOCAL lock_timeout = '5s'\")\n"
EXPAND_HEADER = '''"""a migration.

downgrade: destructive
schema: {schema}
"""

from alembic import op
import sqlalchemy as sa

'''


def _expand(body: str, schema: str = "expand") -> str:
    return (
        EXPAND_HEADER.format(schema=schema)
        + "def upgrade() -> None:\n"
        + body
        + "\n\ndef downgrade() -> None:\n    op.drop_column('t', 'c')\n"
    )


@pytest.mark.parametrize(
    ("body", "line_text"),
    [
        ("    op.drop_column('t', 'c')\n", "op.drop_column"),
        (
            "    op.execute(\"SET LOCAL lock_timeout = '5s'\")\n"
            "    op.add_column('t', sa.Column('c', sa.Text(), nullable=False))\n",
            "NOT NULL column without a server_default",
        ),
        (
            "    op.add_column('t', sa.Column('c', sa.Text(), nullable=True))\n",
            "before SET LOCAL lock_timeout",
        ),
        ("    op.execute('UPDATE t SET c = 1')\n", "UPDATE"),
        ("    op.execute(f'SELECT {1}')\n", "not a literal"),
        ("    op.create_index('ix', 't', ['c'])\n", "without concurrently"),
        (_LOCKED + "    op.execute('CREATE INDEX ix ON t (c)')\n", "without CONCURRENTLY"),
        (
            "    op.create_index('ix', 't', ['c'], unique=True, postgresql_concurrently=True)\n",
            "unique index",
        ),
        (
            _LOCKED
            + "    op.execute('CREATE TRIGGER g BEFORE INSERT ON t EXECUTE FUNCTION f()')\n",
            "trigger or policy",
        ),
        (_LOCKED + "    op.execute('CREATE POLICY p ON t AS RESTRICTIVE')\n", "trigger or policy"),
        ("    op.execute('CREATE OR REPLACE FUNCTION f() RETURNS int')\n", "OR REPLACE"),
        (
            "    op.create_table('w', sa.Column('t_id', sa.Integer(), sa.ForeignKey('t.id')))\n",
            "foreign key to an existing table",
        ),
        (
            "    op.create_table('w')\n    op.create_foreign_key('fk', 'w', 't', ['t_id'], ['id'])\n",
            "foreign key to an existing table",
        ),
        (
            _LOCKED + "    op.add_column('t', sa.Column('c', sa.Integer(), primary_key=True))\n",
            "NOT NULL",
        ),
        (
            "    session = sa.orm.Session(bind=op.get_bind())\n    session.delete(1)\n",
            "may write",
        ),
        ("    op.execute(\"COMMENT ON TABLE t IS 'a -- b'; DROP TABLE x\")\n", "DROP TABLE x"),
    ],
)
def test_expand_declared_but_not_expand_is_reported_with_its_line(
    tmp_path: Path, body: str, line_text: str
) -> None:
    path = tmp_path / "0222_x.py"
    path.write_text(_expand(body), encoding="utf-8")

    findings = [
        f for f in lint_migrations.lint_file(path) if f.rule == "migrations/expand-not-expand"
    ]

    assert findings and line_text in findings[0].message, f"{line_text!r} in {findings}"
    assert findings[0].line > 8, "the finding names the offending line, not the docstring"
    assert lint_migrations.classify_file(path) == "contract", "the classifier agrees"


def test_additive_upgrade_declared_expand_passes_and_contract_may_be_declared_loosely(
    tmp_path: Path,
) -> None:
    body = (
        "    op.execute(\"SET LOCAL lock_timeout = '5s'\")\n"
        "    op.add_column('t', sa.Column('c', sa.Text(), server_default='x', nullable=False))\n"
        "    op.create_index('ix', 't', ['c'], postgresql_concurrently=True)\n"
        "    op.create_table('w', sa.Column('t_id', sa.Integer(), sa.ForeignKey('t.id')))\n"
        "    op.execute('CREATE TRIGGER g BEFORE INSERT ON w EXECUTE FUNCTION f()')\n"
    )
    expand, contract = tmp_path / "0222_a.py", tmp_path / "0223_b.py"
    expand.write_text(_expand(body), encoding="utf-8")
    contract.write_text(_expand(body, "contract"), encoding="utf-8")

    assert lint_migrations.lint_file(expand) == [], "additive upgrade passes as expand"
    assert lint_migrations.lint_file(contract) == [], "declaring contract is always allowed"


def test_a_call_through_an_aliased_op_is_classified(tmp_path: Path) -> None:
    path = tmp_path / "0222_x.py"
    source = _expand("    o.drop_column('t', 'c')\n")
    path.write_text(source.replace("from alembic import op\n", "from alembic import op as o\n"))

    assert lint_migrations.classify_file(path) == "contract", "o is alembic's op"


def test_schema_marker_is_required_from_the_first_new_number_only(tmp_path: Path) -> None:
    source = VALID_SAFE
    old, new = tmp_path / "0232_old.py", tmp_path / "0233_new.py"
    old.write_text(source, encoding="utf-8")
    new.write_text(source, encoding="utf-8")

    assert lint_migrations.lint_file(old) == [], "files up to 0232 need no schema marker"
    assert [f.rule for f in lint_migrations.lint_file(new)] == ["migrations/missing-schema"]
    placeholder = _expand("    pass\n", "TODO-declare (expand|contract)")
    new.write_text(placeholder, encoding="utf-8")
    assert [f.rule for f in lint_migrations.lint_file(new)] == ["migrations/invalid-schema"]


def test_classify_prints_one_line_per_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "0222_x.py"
    path.write_text(_expand("    op.create_table('w')\n"), encoding="utf-8")

    assert lint_migrations.main(["--classify", str(path)]) == 0
    assert capsys.readouterr().out == f"{path}: expand\n"


def test_module_is_stdlib_only() -> None:
    source = _SCRIPT_PATH.read_text(encoding="utf-8")
    allowed_prefixes = (
        "import argparse",
        "import ast",
        "import re",
        "import sys",
        "from __future__",
        "from dataclasses",
        "from pathlib",
    )
    import_lines = [
        line
        for line in source.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    ]
    for line in import_lines:
        assert line.startswith(allowed_prefixes), f"non-stdlib import found: {line!r}"


if "lint_migrations" in sys.modules:
    del sys.modules["lint_migrations"]
