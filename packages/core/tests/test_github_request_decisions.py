"""What the requester's card says after an admin grants a GitHub request, on every platform."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from daimon.core import github_request_decisions as decisions


class _Sessions:
    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[None]:
        yield None

    def __call__(self) -> Any:
        return self._session()

    def begin(self) -> Any:
        return self._session()


_REQUEST = SimpleNamespace(
    requester_account_id=uuid.uuid4(), requester_platform_user_id="U1", agent_name="Helper"
)


def _settings(root: str | None) -> Any:
    return SimpleNamespace(mcp=SimpleNamespace(app_root_url=root))


@pytest.mark.parametrize("platform", ["discord", "slack"])
@pytest.mark.parametrize(
    ("root", "linked", "expected"),
    [
        (None, False, decisions.RequesterUpdate("✓ Added. Helper is continuing.", False)),
        ("https://app", True, decisions.RequesterUpdate("✓ Added. Helper is continuing.", False)),
        ("https://app", False, decisions.RequesterUpdate(decisions.LINK_TEXT, True, "link")),
    ],
)
async def test_a_grant_asks_an_unlinked_requester_to_link_github(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    root: str | None,
    linked: bool,
    expected: decisions.RequesterUpdate,
) -> None:
    mint = AsyncMock(return_value="link")
    monkeypatch.setattr(decisions, "account_link_status", AsyncMock(return_value=linked))
    monkeypatch.setattr(decisions, "mint_link", mint)
    update = await decisions.approved_update(
        cast(Any, _Sessions()),
        _settings(root),
        cast(Any, _REQUEST),
        tenant_id=uuid.uuid4(),
        platform=platform,
    )
    assert update == expected
    if expected.link_url:
        assert mint.await_args is not None and mint.await_args.kwargs["platform"] == platform
    else:
        mint.assert_not_awaited()
