"""`attachment_prompt`: an upload goes inline only as the one shared vision rule allows."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.web import files
from daimon.adapters.web.files import attachment_prompt
from daimon.core.media.vision import MAX_VISION_IMAGE_DIMENSION
from daimon.testing.images import png_bytes


async def test_an_over_wide_upload_is_resized_inline_and_a_cut_off_one_is_a_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inlining one image past the edge cap ends the session, so the web path resizes it
    like every chat adapter; bytes that cannot be decoded are linked, never inlined."""
    wide = png_bytes(MAX_VISION_IMAGE_DIMENSION + 1000, 200)
    held = {
        "wide": SimpleNamespace(content=wide),
        "cut": SimpleNamespace(content=wide[:-30]),
        "fine": SimpleNamespace(content=png_bytes()),
    }
    monkeypatch.setattr(files.file_uploads, "get_person_uploads", AsyncMock(return_value=held))
    prompt, blocks = await attachment_prompt(
        MagicMock(),
        tenant_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        files=[cast(Any, SimpleNamespace(id=key, name=f"{key}.png")) for key in held],
        link=lambda handle: f"https://files.example/{handle}",
    )

    assert len(blocks) == 2, "the wide and the small image are inline; the cut-off one is not"
    assert "https://files.example/cut" in prompt, "the cut-off image is offered as a link"
    assert "https://files.example/wide" not in prompt, "the resized one needs no link"
