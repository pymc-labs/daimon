"""``__main__`` gating: the adapter only boots when Teams settings exist."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from daimon.adapters.teams.__main__ import main


@pytest.mark.asyncio
async def test_main_exits_cleanly_without_teams_settings() -> None:
    settings = MagicMock()
    settings.teams = None
    with (
        patch("daimon.adapters.teams.__main__.load_settings", return_value=settings),
        pytest.raises(SystemExit) as exc,
    ):
        await main()
    assert exc.value.code == 0


@pytest.mark.asyncio
async def test_main_requires_crypto_keys_for_token_decryption() -> None:
    settings = MagicMock()
    settings.crypto.keys = ()
    with (
        patch("daimon.adapters.teams.__main__.load_settings", return_value=settings),
        pytest.raises(SystemExit) as exc,
    ):
        await main()
    assert exc.value.code == 1
