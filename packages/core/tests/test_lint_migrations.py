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


@pytest.mark.parametrize(
    ("source", "expected_rule"),
    [
        (MISSING_MARKER, "migrations/missing-marker"),
        (INVALID_MARKER, "migrations/invalid-marker"),
        (DUPLICATE_MARKER, "migrations/duplicate-marker"),
        (UNSUPPORTED_WITH_REAL_BODY, "migrations/marker-body-mismatch"),
        (SAFE_WITH_RAISE_BODY, "migrations/marker-body-mismatch"),
        (SAFE_WITH_TRIVIAL_BODY, "migrations/marker-body-mismatch"),
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


@pytest.mark.parametrize("source", [VALID_SAFE, VALID_UNSUPPORTED])
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
