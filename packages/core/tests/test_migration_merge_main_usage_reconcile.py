"""The merge revision joins main's usage reconcile branch with the neutral-core branch.

Main's 0087_usage_reconcile_progress keeps its id and parent (it may already be
applied), so 0090 joins it with the neutral-core chain, and 0092 joins that
with 0091_agent_backend_settings; later revisions follow that. Each branch still
reverses and re-applies on its own under the merge.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

_REPO_ROOT = Path(__file__).resolve().parents[3]
_VERSIONS = _REPO_ROOT / "packages" / "core" / "alembic" / "versions"
MERGE = "0090_merge_main_usage_reconcile"
HEAD = "0092_merge_agent_backend"
AGENT_BACKEND = "0091_agent_backend_settings"
MAIN = "0087_usage_reconcile_progress"
NEUTRAL = ("0087_neutral_state", "0088_usage_observation_revision", "0089_neutral_catalog")
FORK = "0086_discord_gateway_lease"


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(_REPO_ROOT / "alembic.ini")))


def _module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_merge_sits_under_the_single_head_over_both_branches() -> None:
    script = _script()
    heads = script.get_heads()
    assert len(heads) == 1
    assert HEAD in {revision.revision for revision in script.iterate_revisions(heads[0], FORK)}
    head = script.get_revision(HEAD)
    assert head is not None
    assert set(head.down_revision or ()) == {MERGE, AGENT_BACKEND}
    merge = script.get_revision(MERGE)
    assert merge is not None
    assert set(merge.down_revision or ()) == {NEUTRAL[-1], MAIN}
    # Main's revision is untouched: same id, same parent.
    main = script.get_revision(MAIN)
    assert main is not None and main.down_revision == FORK
    below = {revision.revision for revision in script.iterate_revisions(MERGE, FORK)}
    assert below == {MERGE, MAIN, *NEUTRAL}


@pytest.mark.fresh_schema
async def test_each_branch_reverses_and_reapplies_under_the_merge(
    db_session: AsyncSession,
) -> None:
    connection = await db_session.connection()
    merge, main, catalog = _module(MERGE), _module(MAIN), _module(NEUTRAL[-1])

    def cycle(sync: Connection) -> None:
        here = sync.scalar(text("SELECT current_schema()"))

        def tables() -> set[str]:
            return set(inspect(sync).get_table_names(schema=here))

        with Operations.context(MigrationContext.configure(sync)):
            start = tables()
            assert {"usage_reconcile_progress", "neutral_agent_revisions"} <= start
            merge.downgrade()
            assert tables() == start, "the merge itself changes no schema"
            main.downgrade()
            catalog.downgrade()
            assert "usage_reconcile_progress" not in tables()
            assert "neutral_agent_revisions" not in tables()
            catalog.upgrade()
            main.upgrade()
            merge.upgrade()
            assert tables() == start

    await connection.run_sync(cycle)
