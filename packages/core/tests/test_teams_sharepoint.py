"""Which shared files count as inside a channel's Files folder."""

from __future__ import annotations

import pytest
from daimon.core.teams_sharepoint import DriveFolder, DriveItem, is_inside

_FOLDER = DriveFolder(drive_id="b!d", item_id="f1")


def _item(path: str | None, *, drive: str = "b!d") -> DriveItem:
    return DriveItem.model_validate(
        {"id": "i", "parentReference": {"driveId": drive, "path": path}}
    )


@pytest.mark.parametrize(
    ("item", "inside"),
    [
        (_item("/drives/b!d/root:/General"), True),
        (_item("/drives/b!d/root:/general/Q3"), True),
        (_item("/drives/b!d/root:/General 2"), False),
        (_item("/drives/b!d/root:/Board"), False),
        (_item("/drives/b!d/root:"), False),
        (_item("/drives/b!x/root:/General", drive="b!x"), False),
    ],
)
def test_is_inside_takes_the_folder_and_its_subfolders_only(item: DriveItem, inside: bool) -> None:
    """A sibling sharing the name's prefix, the library root and another library stay out."""
    assert is_inside(item, _FOLDER, "/drives/b!d/root:/General") is inside, item


def test_is_inside_a_library_root_takes_that_library_only() -> None:
    """A channel whose folder is a library's root (no Graph path) takes that whole library."""
    assert is_inside(_item("/drives/b!d/root:/Any"), _FOLDER, None), "same library"
    assert not is_inside(_item(None, drive="b!x"), _FOLDER, None), "another library"
