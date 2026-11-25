"""Schema names and the SQL templates that use them."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources

from daimon.core.config import DataSettings


@dataclass(frozen=True)
class Schemas:
    """The store's schema and its read views, named by `DAIMON_DATA__SCHEMA_NAME`.

    Tests give each xdist worker its own pair, so SQL never names a schema directly.
    """

    data: str
    views: str

    @classmethod
    def of(cls, settings: DataSettings) -> Schemas:
        return cls(settings.schema_name, f"{settings.schema_name}_views")


def render(sql: str, schemas: Schemas) -> str:
    """Fill `{{data}}` and `{{views}}`. The names are validated identifiers, never quoted."""
    return sql.replace("{{data}}", schemas.data).replace("{{views}}", schemas.views)


def read_sql(name: str) -> str:
    """One file from this package's `sql/` folder."""
    return resources.files("daimon.data").joinpath("sql", name).read_text("utf-8")
