"""The connect flow's offer to follow an org: who is offered it, and what proves the signer may."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from daimon.core import github_connect_flow
from daimon.core.github_connect_flow import org_proof
from daimon.core.github_connect_views import (
    ConnectInstallation,
    ConnectPage,
    ConnectRepo,
    ConnectSelection,
    FollowOffer,
    agent_editor_view,
    confirmed_follow_offer,
    follow_offers,
    selection_from_form,
    selection_from_json,
)
from daimon.core.stores.github_access import AgentRepo


def _installation(
    inst: int = 1,
    login: str = "acme",
    *,
    selection: str = "all",
    repos: int = 3,
    truncated: bool = False,
    state: str = "active",
    owner_type: str = "Organization",
) -> ConnectInstallation:
    return ConnectInstallation(
        id=inst,
        owner_id=inst,
        owner_login=login,
        repository_selection=selection,
        owner_type=owner_type,
        repos=tuple(
            ConnectRepo(inst * 100 + n, inst, inst, f"{login}/repo-{n}", "admin")
            for n in range(repos)
        ),
        truncated=truncated,
        state=state,  # type: ignore[arg-type]
    )


def _held(repo_id: int, installation_id: int) -> AgentRepo:
    now = datetime.now(UTC)
    return AgentRepo(
        repo_id=repo_id,
        full_name=f"acme/held-{repo_id}",
        scope="agent",
        max_access="write",
        baseline_access="write",
        ceiling_access="write",
        status="active",
        added_by_account_id=None,
        added_at=now,
        granted_by_account_id=None,
        granted_at=now,
        installation_id=installation_id,
    )


def test_the_offer_is_for_an_admin_and_a_whole_org_only() -> None:
    whole = _installation()
    offers = follow_offers([whole], [], may_follow=True, following=False)
    assert [o.owner for o in offers] == ["acme"]
    assert offers[0].repo_ids == (100, 101, 102)
    assert follow_offers([whole], [], may_follow=False, following=False) == ()
    assert follow_offers([whole], [], may_follow=True, following=True) == ()
    for refused in (
        _installation(selection="selected"),
        _installation(truncated=True),
        _installation(state="suspended"),
        _installation(repos=0),
        _installation(owner_type="User"),
    ):
        assert follow_offers([refused], [], may_follow=True, following=False) == ()


def test_an_agent_with_repos_in_another_org_is_not_offered_the_follow() -> None:
    offers = follow_offers(
        [_installation(1), _installation(2, "other")],
        [_held(5, 1)],
        may_follow=True,
        following=False,
    )
    assert [o.installation_id for o in offers] == [1]


def test_a_save_that_leaves_part_of_the_org_out_is_refused() -> None:
    offer = FollowOffer(installation_id=1, owner="acme", repo_ids=(100, 101, 102))
    whole = {100, 101, 102}
    assert confirmed_follow_offer(
        [offer], installation_id=1, held_after=whole, installations_after={1}
    )
    assert not confirmed_follow_offer(
        [offer], installation_id=1, held_after={100, 101}, installations_after={1}
    )
    assert not confirmed_follow_offer(
        [offer], installation_id=1, held_after=whole, installations_after={1, 2}
    ), "a repo of another org"
    assert not confirmed_follow_offer(
        [offer], installation_id=2, held_after=whole, installations_after={1}
    ), "an org that was not offered"


def test_the_editor_view_carries_the_offer_and_the_follow_state() -> None:
    offer = FollowOffer(installation_id=1, owner="acme", repo_ids=(100, 101, 102))

    def view(**kwargs: Any) -> Any:
        return agent_editor_view(
            root="https://mcp.test",
            state="state",
            invitation_hash="invitation",
            secret="secret",
            agent_name="Atlas",
            workspace="Test",
            platform="discord",
            grants=[],
            installations=[_installation()],
            snapshot="snap",
            cancel_url="https://mcp.test/cancel",
            install_url="https://github.com/apps/x/installations/new",
            **kwargs,
        )

    assert view().follow_offers == [] and view().following is False
    offered = view(follow=[offer], following=False)
    assert offered.follow_offers == [offer]
    dumped = ConnectPage(view=offered).model_dump(mode="json")["view"]
    assert dumped["follow_offers"] == [
        {"installation_id": 1, "owner": "acme", "repo_ids": [100, 101, 102]}
    ]
    assert view(following=True).following is True


def test_a_selection_carries_the_follow_field_through_a_retry() -> None:
    selection = ConnectSelection(follow_org=("77",))
    assert selection.form_fields() == {"follow_org": ["77"]}
    for parsed in (
        selection_from_form(b"follow_org=77"),
        selection_from_json(b'{"follow_org":["77"]}'),
    ):
        assert isinstance(parsed, ConnectSelection)
        assert parsed.follow_org == ("77",) and parsed.unknown_fields == ()


_OFFER = FollowOffer(installation_id=77, owner="alpha", repo_ids=(101, 102))


def _github_org(
    *,
    membership: tuple[int, dict[str, object]],
    installation_repos: list[int],
    total: int | None = None,
) -> httpx.AsyncClient:
    """GitHub: what it says of the signer's membership, and the App's whole listing."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/user/memberships/orgs/alpha":
            assert request.headers["authorization"] == "Bearer user-token"
            return httpx.Response(membership[0], json=membership[1])
        if path == "/app/installations/77/access_tokens":
            return httpx.Response(201, json={"token": "listing"})
        if path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "total_count": len(installation_repos) if total is None else total,
                    "repositories": [
                        {"id": n, "full_name": f"alpha/repo-{n}", "owner": {"id": 55}}
                        for n in installation_repos
                    ],
                },
            )
        raise AssertionError(path)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


_DENIED = (403, {"message": "Resource not accessible by integration"})
_ADMIN = (200, {"state": "active", "role": "admin"})


async def _proof(**github: Any) -> Any:
    async with _github_org(**github) as client:
        return await org_proof(client, "user-token", _OFFER, app_jwt="app-jwt")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("membership", "expected"),
    [
        (_ADMIN, "org_admin"),
        ((200, {"state": "active", "role": "member"}), None),
        ((200, {"state": "pending", "role": "admin"}), None),
    ],
)
async def test_github_saying_who_the_signer_is_settles_it(
    membership: tuple[int, dict[str, object]], expected: str | None
) -> None:
    assert await _proof(membership=membership, installation_repos=[101, 102, 103]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404])
async def test_a_refused_membership_falls_back_to_the_full_listing(status: int) -> None:
    refused = (status, {"message": "Resource not accessible by integration"})
    assert await _proof(membership=refused, installation_repos=[101, 102]) == "full_listing"


@pytest.mark.asyncio
async def test_a_refused_membership_with_only_part_of_the_org_visible_fails() -> None:
    """A client workspace's admin sees 2 of the org's 3 repos."""
    assert await _proof(membership=_DENIED, installation_repos=[101, 102, 103]) is None


@pytest.mark.asyncio
async def test_a_truncated_installation_listing_proves_nothing() -> None:
    with pytest.raises(ValueError, match="does not match total_count"):
        await _proof(membership=_DENIED, installation_repos=[101, 102], total=3)


@pytest.mark.asyncio
async def test_a_github_outage_is_not_read_as_a_no() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        await _proof(membership=(502, {}), installation_repos=[101, 102])


@pytest.mark.asyncio
async def test_an_offer_whose_proof_fails_is_hidden_not_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page's offers are the ones the signer can prove; a GitHub error hides the offer."""
    flow = object.__new__(github_connect_flow.GitHubConnectFlow)
    flow._private_key = "pem"  # pyright: ignore[reportPrivateUsage]
    flow._app_id = "42"  # pyright: ignore[reportPrivateUsage]
    flow._factory = lambda: _github_org(  # pyright: ignore[reportPrivateUsage]
        membership=_DENIED, installation_repos=[101, 102], total=3
    )
    monkeypatch.setattr(github_connect_flow, "build_app_jwt", lambda *_a, **_k: "app-jwt")
    shown = await flow._proved_offers("user-token", [_OFFER])  # pyright: ignore[reportPrivateUsage]
    assert shown == [], "a cut-short listing proves nothing"
