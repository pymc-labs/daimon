"""New-repo notice names require the recipient's own GitHub proof."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet, MultiFernet
from daimon.core import github_notice_visibility as visibility
from daimon.core.stores.github_new_repo_notices import NewRepoNotice, NewRepoNoticeGroup


def _group() -> NewRepoNoticeGroup:
    tenant_id = uuid.uuid4()
    now = datetime.now(UTC)
    return NewRepoNoticeGroup(
        notices=(
            NewRepoNotice(
                tenant_id=tenant_id,
                installation_id=9,
                repo_full_name="example/visible",
                queued_at=now,
                claimed_at=now,
            ),
            NewRepoNotice(
                tenant_id=tenant_id,
                installation_id=9,
                repo_full_name="example/hidden",
                queued_at=now,
                claimed_at=now,
            ),
        )
    )


def test_notice_copy_has_private_and_generic_forms() -> None:
    assert visibility.new_repo_notice_copy(()).text == "New repos are available on GitHub."
    named = visibility.new_repo_notice_copy(("example/visible",))
    assert named.text == "🐙 New on GitHub: example/visible\nConnect this repo to Daimon?"
    assert named.connect_label == "Connect repo"
    assert named.dismiss_label == "Not now"


@pytest.mark.asyncio
async def test_notice_names_need_prior_connection_and_current_repo_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = uuid.uuid4()
    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    link = SimpleNamespace(github_user_id=3, platform="discord", platform_user_id="person")
    user = SimpleNamespace(
        status="active",
        access_expires_at=datetime.now(UTC) + timedelta(hours=1),
        encrypted_access_token=fernet.encrypt(b"test-token"),
    )
    monkeypatch.setattr(visibility, "get_account_link", AsyncMock(return_value=link))
    monkeypatch.setattr(visibility, "get_user", AsyncMock(return_value=user))
    prior = AsyncMock(return_value={9})
    monkeypatch.setattr(visibility, "prior_connection_installations", prior)
    session = AsyncMock()

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(200 if request.url.path.endswith("/visible") else 404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        visible = await visibility.visible_new_repo_names(
            session,
            group=_group(),
            account_id=account_id,
            platform="discord",
            platform_user_id="person",
            fernet=fernet,
            http_client=client,
        )
        assert visible == ("example/visible",)
        assert (
            await visibility.visible_new_repo_names(
                session,
                group=_group(),
                account_id=account_id,
                platform="slack",
                platform_user_id="person",
                fernet=fernet,
                http_client=client,
            )
            == ()
        )
        prior.return_value = set()
        assert (
            await visibility.visible_new_repo_names(
                session,
                group=_group(),
                account_id=account_id,
                platform="discord",
                platform_user_id="person",
                fernet=fernet,
                http_client=client,
            )
            == ()
        )
