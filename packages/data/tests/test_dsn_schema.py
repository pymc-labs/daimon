from __future__ import annotations

import pytest
from daimon.core.config import DataSettings
from daimon.data.dsn import libpq_dsn
from daimon.data.migrate import Facts, missing_prerequisites
from daimon.data.schema import Schemas, render


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql+asyncpg://u:p@db:5432/app", "postgresql://u:p@db:5432/app"),
        ("postgresql+asyncpg://u@db/app?ssl=true", "postgresql://u@db/app?sslmode=require"),
        (
            "postgresql+asyncpg://u@db/app?ssl=verify-full",
            "postgresql://u@db/app?sslmode=verify-full",
        ),
    ],
)
def test_libpq_dsn_translates_the_asyncpg_form(url: str, expected: str) -> None:
    assert libpq_dsn(url) == expected


def test_render_fills_both_schema_names() -> None:
    schemas = Schemas.of(DataSettings(schema_name="docs"))
    assert render("{{data}}.t / {{views}}.v", schemas) == "docs.t / docs_views.v"


@pytest.mark.parametrize(
    ("facts", "missing"),
    [
        (Facts(True, True, False), 0),
        (Facts(True, False, True), 0),
        (Facts(True, False, False), 1),
        (Facts(False, True, True), 1),
        (Facts(False, False, False), 2),
    ],
)
def test_missing_prerequisites_names_what_the_server_lacks(facts: Facts, missing: int) -> None:
    assert len(missing_prerequisites(facts)) == missing
