"""`/api/support`: admins change limits, menus and the request channel; stale, refusals, audit."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from daimon.adapters.web.channel_visibility import forget_visibility
from daimon.core.config import DiscordSettings, Settings, SupportSettings, TurnSettings
from daimon.core.platform_names import ChannelSighting
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores import workspace_members as members_store
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.platform_names import KnownName, upsert_channel_names, upsert_user_names
from daimon.core.stores.tenants import get_tenant
from daimon.core.support import MenuSpec, RequestChoice, SupportPlace
from daimon.testing.factories import make_platform_principal
from httpx import Response
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import shown_trail
from .conftest import FakeUpstream, WebClientFactory
from .test_admin_routes import Admin, signed_in_admin

type Factory = async_sessionmaker[AsyncSession]

_P1 = "100000000000000001"
_STALE = "This changed since you opened it. Reload and try again."
_OPTION: dict[str, object] = {
    "key": "review",
    "label": "Review",
    "price": {"kind": "range", "min": "1", "max": "3"},
}
_MENU: dict[str, object] = {"name": "Support", "options": [_OPTION]}
_SETTINGS = Settings(
    _env_file=None,  # pyright: ignore[reportCallIssue]
    database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
    turn=TurnSettings(channel_backends=True),
    support=SupportSettings(escalation_channel_id="c9"),
)
_LOCKED = Settings(
    _env_file=None,  # pyright: ignore[reportCallIssue]
    database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
    turn=TurnSettings(channel_backends=True),
    support=SupportSettings(escalation_channel_id="c9"),
    features={"support_own_channel": "off"},
)
_UNSET = Settings(
    _env_file=None,  # pyright: ignore[reportCallIssue]
    database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
    turn=TurnSettings(channel_backends=True),
)
_ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/api/support", None),
    ("GET", "/api/support/people?q=a", None),
    (
        "PUT",
        "/api/support/limits",
        {"scope": "workspace", "limit": {"shared": "5"}, "expected_revision": None},
    ),
    ("PUT", "/api/support/menus/support", {"menu": _MENU, "expected_revision": None}),
    (
        "PUT",
        "/api/support/menus/use",
        {"scope": "workspace", "menu": None, "expected": None},
    ),
    ("PUT", "/api/support/channel", {"channel_id": None, "expected_revision": None}),
]


_VIEW, _SEND, _EMBED = 1 << 10, 1 << 11, 1 << 14


class _Bot:
    """Discord as Daimon's bot sees it: every channel is a text channel in guild G1 where the bot
    holds `everyone`'s permissions; any `status` other than 200 fails every call. Both can change
    mid-test."""

    def __init__(self, *, everyone: int = _VIEW | _SEND | _EMBED, status: int = 200) -> None:
        self.everyone = everyone
        self.status = status
        self.upstream = FakeUpstream(self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.status != 200:
            return httpx.Response(self.status)
        if path.endswith("/users/@me"):
            return httpx.Response(200, json={"id": "B1"})
        if "/channels/" in path:
            channel: dict[str, object] = {"type": 0, "guild_id": "G1", "permission_overwrites": []}
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[1], **channel})
        if path.endswith("/members/B1"):
            return httpx.Response(200, json={"roles": []})
        roles = [{"id": "G1", "permissions": str(self.everyone)}]
        return httpx.Response(200, json={"owner_id": "other", "roles": roles})


@asynccontextmanager
async def _support(
    web_client: WebClientFactory,
    factory: Factory,
    tmp_path: Path,
    *,
    settings: Settings = _SETTINGS,
    bot: _Bot | None = None,
) -> AsyncIterator[Admin]:
    """The admin fixture with channel `c1` named general, `c9` requests and thread `t9`.

    Daimon's bot can post everywhere unless `bot` says otherwise.
    """
    forget_visibility()
    settings = settings.model_copy(update={"discord": DiscordSettings(bot_token=SecretStr("t"))})
    provider = (bot or _Bot()).upstream
    async with signed_in_admin(
        web_client, factory, tmp_path, settings=settings, provider=provider
    ) as admin:
        async with factory.begin() as db:
            await upsert_channel_names(
                db,
                tenant_id=admin.ws.tenant_id,
                platform="discord",
                names={
                    "c1": ChannelSighting("general"),
                    "c9": ChannelSighting("requests"),
                    "t9": ChannelSighting("Launch", kind="thread", parent_id="c1"),
                },
            )
        yield admin


def _limit(
    scope: str = "workspace",
    scope_id: str = "",
    *,
    limit: dict[str, object] | None,
    revision: int | None = None,
    person_id: str | None = None,
) -> dict[str, object]:
    return {
        "scope": scope,
        "scope_id": scope_id,
        "person_id": person_id,
        "limit": limit,
        "expected_revision": revision,
    }


def _locs(response: Response) -> list[list[object]]:
    """The `loc` of every problem in a 422."""
    return [p["loc"] for p in response.json()["detail"]]


def _one(rows: list[dict[str, Any]], **match: object) -> dict[str, Any]:
    (row,) = [r for r in rows if all(r[k] == v for k, v in match.items())]
    return row


async def _submit(
    factory: Factory,
    admin: Admin,
    message: str,
    *,
    user: str = "1",
    choice: RequestChoice | None = None,
) -> uuid.UUID:
    """Record one request (the default menu's unless `choice`) in channel c1; its id."""
    async with factory.begin() as db:
        outcome = await ledger.submit_request(
            db,
            tenant_id=admin.ws.tenant_id,
            account_id=None,
            platform="discord",
            platform_user_id=user,
            channel_id="c1",
            message_id=message,
            ma_session_id=None,
            place=SupportPlace(channel_id="c1", agent_id=None),
            choice=choice or RequestChoice.default("help"),
            default_allowance=20,
        )
    assert outcome.row is not None, "the request was recorded"
    return outcome.row.id


async def test_admin_sets_limits_a_menu_and_its_use_then_reads_them_back(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """Every write returns the whole page, and the audit trail names the place, not a person."""
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        put = admin.client.put
        menu = await put(
            "/api/support/menus/support", json={"menu": _MENU, "expected_revision": None}
        )
        workspace = await put(
            "/api/support/limits",
            json=_limit(limit={"shared": "5", "each_person": "2", "window": "monthly"}),
        )
        channel = await put(
            "/api/support/limits", json=_limit("channel", "c1", limit={"each_person": "1.5"})
        )
        use = await put(
            "/api/support/menus/use",
            json={"scope": "channel", "scope_id": "c1", "menu": "support", "expected": None},
        )
        page = await admin.client.get("/api/support")
        trail = await shown_trail(db_session_factory, admin.ws.tenant_id)

    for step in (menu, workspace, channel, use, page):
        assert step.status_code == 200, step.text
    body = page.json()
    assert body["platform"] == "discord", "the workspace's platform"
    (shown_menu,) = body["menus"]
    assert (shown_menu["key"], shown_menu["name"], shown_menu["options"]) == (
        "support",
        "Support",
        1,
    ), "the menu as listed"
    assert shown_menu["body"]["options"][0]["key"] == "review", "the menu body as stored"
    assert shown_menu["used_in"] == [{"scope": "channel", "scope_id": "c1", "name": "general"}], (
        "the place that uses it, by name"
    )
    ws_limit = _one(body["limits"], scope="workspace", person_id=None)
    assert (ws_limit["shared"], ws_limit["each_person"], ws_limit["window"], ws_limit["used"]) == (
        "5",
        "2",
        "monthly",
        "0",
    ), "the workspace limit as saved"
    own = _one(body["limits"], scope="channel", scope_id="c1")
    assert (own["name"], own["shared"], own["each_person"]) == ("general", None, "1.5"), (
        "a channel limit with only an each-person amount"
    )
    assert _one(body["menu_use"], scope="channel", scope_id="c1")["menu"] == "support", "the pick"
    assert trail[-4:] == [
        ("Changed support menu", "support", "Menu"),
        ("Changed support limit", "Workspace", "Limit"),
        ("Changed support limit", "#general", "Limit"),
        ("Changed support menu", "#general", "Menu choice"),
    ], "each write audited with where it applied"


async def test_a_stale_revision_is_a_409_with_the_contract_text_and_the_last_read_goes_through(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """Create over an existing row, a changed row and a changed menu or pick all refuse."""
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        put = admin.client.put
        first = await put("/api/support/limits", json=_limit(limit={"shared": "5"}))
        revision = _one(first.json()["limits"], scope="workspace")["revision"]
        create_again = await put("/api/support/limits", json=_limit(limit={"shared": "6"}))
        stale = await put(
            "/api/support/limits", json=_limit(limit={"shared": "6"}, revision=revision + 100)
        )
        changed = await put(
            "/api/support/limits", json=_limit(limit={"shared": "7"}, revision=revision)
        )
        stale_clear = await put("/api/support/limits", json=_limit(limit=None, revision=revision))
        menu = await put(
            "/api/support/menus/support", json={"menu": _MENU, "expected_revision": None}
        )
        stale_menu = await put(
            "/api/support/menus/support", json={"menu": _MENU, "expected_revision": None}
        )
        stale_remove = await put(
            "/api/support/menus/support", json={"menu": None, "expected_revision": 0}
        )
        use = await put(
            "/api/support/menus/use",
            json={"scope": "workspace", "menu": "support", "expected": None},
        )
        stale_use = await put(
            "/api/support/menus/use",
            json={"scope": "workspace", "menu": "support", "expected": "other"},
        )
        cleared = await put(
            "/api/support/limits",
            json=_limit(
                limit=None, revision=_one(changed.json()["limits"], scope="workspace")["revision"]
            ),
        )

    assert [
        r.status_code
        for r in (create_again, stale, stale_clear, stale_menu, stale_remove, stale_use)
    ] == [409] * 6, "a value set since it was read is never overwritten"
    for refused in (create_again, stale, stale_menu, stale_use):
        assert refused.json() == {"detail": _STALE}, "the contract text, as is"
    assert changed.status_code == 200, changed.text
    assert _one(changed.json()["limits"], scope="workspace")["shared"] == "7", (
        "the last read goes through"
    )
    assert menu.status_code == 200 and use.status_code == 200, "the menu and its pick"
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["limits"] == [], "a null limit removes the row"


@pytest.mark.parametrize(
    ("method", "path", "body"), _ROUTES, ids=[f"{m} {p}" for m, p, _ in _ROUTES]
)
async def test_a_member_is_refused_every_route_and_an_admin_is_not(
    web_client: WebClientFactory,
    db_session_factory: Factory,
    tmp_path: Path,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """Only server admins reach the page and its writes."""
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        allowed = await admin.client.request(method, path, json=body)
        admin.as_member()
        refused = await admin.client.request(method, path, json=body)

    assert refused.status_code == 403, f"a member on {method} {path}: {refused.text}"
    assert allowed.status_code == 200, f"an admin on {method} {path}: {allowed.text}"


async def test_problems_name_the_input_with_a_loc_that_starts_at_the_body_key(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """422s carry `loc` and `msg`; nothing is saved."""
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        put = admin.client.put

        async def limit(**fields: object) -> Response:
            return await put("/api/support/limits", json=_limit(limit=dict(fields)))

        neither = await limit()
        range_ = await limit(shared="1000000")
        places = await limit(shared="1.234")
        backwards = await limit(
            shared="1", starts_at="2026-12-18T00:00:00Z", ends_at="2026-12-01T00:00:00Z"
        )
        person_shared = await put(
            "/api/support/limits",
            json=_limit(limit={"shared": "3"}, person_id=_P1),
        )
        person_none = await put("/api/support/limits", json=_limit(limit={}, person_id=_P1))
        bad_key = await put(
            "/api/support/menus/Bad Key", json={"menu": _MENU, "expected_revision": None}
        )
        bad_menu = await put(
            "/api/support/menus/support",
            json={"menu": {"name": "x", "options": [{"key": "a"}]}, "expected_revision": None},
        )
        no_scope = await put("/api/support/limits", json={"limit": None, "expected_revision": None})
        page = (await admin.client.get("/api/support")).json()

    assert [
        r.status_code
        for r in (
            neither,
            range_,
            places,
            backwards,
            person_shared,
            person_none,
            bad_key,
            bad_menu,
            no_scope,
        )
    ] == [422] * 9, "all refused"
    assert _locs(neither) == [["limit"]], "neither amount names the limit"
    assert _locs(range_) == [["limit", "shared"]], "a credit out of range"
    assert _locs(places) == [["limit", "shared"]], "more than two decimals"
    assert _locs(backwards) == [["limit", "ends_at"]], "ends before it starts"
    assert _locs(person_shared) == [["limit", "shared"]], "a person row has no shared amount"
    assert _locs(person_none) == [["limit", "each_person"]], "a person row needs its own amount"
    assert _locs(bad_key) == [["path", "key"]], "the menu key"
    assert _locs(bad_menu)[0][:3] == ["menu", "options", 0], "the option that is wrong"
    assert _locs(no_scope) == [["scope"]], "a missing key, with no `body` in front"
    assert all(p["msg"] for r in (neither, bad_menu) for p in r.json()["detail"]), "each has a msg"
    assert (page["limits"], page["menus"]) == ([], []), "nothing saved"


async def test_a_menu_a_place_uses_cannot_be_removed_and_an_unknown_one_is_a_404_alone(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """`MENU_IN_USE` is a 422 on `menu`; `NO_MENU` is the whole text of the 404."""
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        put = admin.client.put
        await put("/api/support/menus/support", json={"menu": _MENU, "expected_revision": None})
        used = await put(
            "/api/support/menus/use",
            json={"scope": "workspace", "menu": "support", "expected": None},
        )
        revision = _one(used.json()["menus"], key="support")["revision"]
        in_use = await put(
            "/api/support/menus/support", json={"menu": None, "expected_revision": revision}
        )
        unknown_use = await put(
            "/api/support/menus/use",
            json={"scope": "channel", "scope_id": "c1", "menu": "nope", "expected": None},
        )
        unknown_remove = await put(
            "/api/support/menus/nope", json={"menu": None, "expected_revision": None}
        )
        dropped = await put(
            "/api/support/menus/use",
            json={"scope": "workspace", "menu": None, "expected": "support"},
        )
        removed = await put(
            "/api/support/menus/support", json={"menu": None, "expected_revision": revision}
        )

    assert in_use.status_code == 422 and _locs(in_use) == [["menu"]], in_use.text
    assert unknown_use.status_code == 404, unknown_use.text
    assert unknown_use.json() == {"detail": "No menu has that key."}, "the text alone"
    assert unknown_remove.status_code == 404, unknown_remove.text
    assert unknown_remove.json() == {"detail": "No menu has that key."}, "the text alone"
    assert dropped.status_code == 200, dropped.text
    assert removed.status_code == 200 and removed.json()["menus"] == [], removed.text


async def test_an_unknown_place_is_a_404_when_setting_but_not_when_removing(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """A limit or a pick for an agent or channel the workspace lacks; removal always works."""
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        put = admin.client.put
        await put("/api/support/menus/support", json={"menu": _MENU, "expected_revision": None})
        agent = await put(
            "/api/support/limits", json=_limit("agent", "agent_x", limit={"shared": "1"})
        )
        channel = await put(
            "/api/support/limits", json=_limit("channel", "zzz", limit={"shared": "1"})
        )
        use = await put(
            "/api/support/menus/use",
            json={"scope": "channel", "scope_id": "zzz", "menu": "support", "expected": None},
        )
        gone = await put("/api/support/limits", json=_limit("channel", "zzz", limit=None))
        thread = await put(
            "/api/support/limits", json=_limit("channel", "c1", limit={"shared": "1"})
        )

    assert [r.status_code for r in (agent, channel, use)] == [404] * 3, "No such place."
    assert agent.json() == {"detail": "No such place."}, "the text"
    assert gone.status_code == 200, "removing needs no place lookup: nothing to remove is fine"
    assert gone.json()["limits"] == [], "nothing was there"
    assert thread.status_code == 200, thread.text


async def test_last_day_round_trips_as_a_utc_instant_and_resending_it_changes_nothing(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """A bare date is read as 00:00 UTC; the echo, sent back as is, is accepted unchanged."""
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        put = admin.client.put
        first = await put(
            "/api/support/limits", json=_limit(limit={"shared": "5", "ends_at": "2026-12-18"})
        )
        row = _one(first.json()["limits"], scope="workspace")
        again = await put(
            "/api/support/limits",
            json=_limit(
                limit={"shared": "5", "ends_at": row["ends_at"], "starts_at": row["starts_at"]},
                revision=row["revision"],
            ),
        )

    assert row["ends_at"] == "2026-12-18T00:00:00Z", "the date as an instant"
    assert again.status_code == 200, again.text
    echoed = _one(again.json()["limits"], scope="workspace")
    assert (echoed["ends_at"], echoed["starts_at"]) == (row["ends_at"], row["starts_at"]), (
        "unchanged"
    )


async def test_person_limits_map_a_web_person_to_the_platform_user_and_back(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """A person id is a web person's uuid when they have one here, else the raw platform id."""
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        async with db_session_factory.begin() as db:
            tenant = await get_tenant(db, admin.ws.tenant_id)
            account = await get_account(db, admin.ws.person.account_id)
            assert tenant is not None and account is not None, "the workspace and its person"
            await make_platform_principal(
                db, platform="discord", external_id=_P1, tenant=tenant, account=account
            )
        put = admin.client.put
        await put("/api/support/limits", json=_limit(limit={"shared": "9"}))
        by_uuid = await put(
            "/api/support/limits",
            json=_limit(limit={"each_person": "3"}, person_id=str(admin.ws.person.id)),
        )
        raw = await put(
            "/api/support/limits",
            json=_limit(limit={"each_person": "2"}, person_id="<@100000000000000002>"),
        )
        unknown = await put(
            "/api/support/limits",
            json=_limit(limit={"each_person": "2"}, person_id=str(uuid.uuid4())),
        )
        bad = await put(
            "/api/support/limits", json=_limit(limit={"each_person": "2"}, person_id="u1")
        )
        cleared = await put(
            "/api/support/limits",
            json=_limit(
                limit=None,
                person_id=str(admin.ws.person.id),
                revision=_one(by_uuid.json()["limits"], person_id=str(admin.ws.person.id))[
                    "revision"
                ],
            ),
        )

    assert by_uuid.status_code == 200, by_uuid.text
    mine = _one(by_uuid.json()["limits"], person_id=str(admin.ws.person.id))
    assert (mine["each_person"], mine["shared"], mine["person_name"]) == ("3", None, "Ada"), (
        "the web person's row, by uuid and name"
    )
    assert raw.status_code == 200, raw.text
    assert _one(raw.json()["limits"], person_id="100000000000000002")["each_person"] == "2", (
        "a person with no web account is their platform id"
    )
    assert unknown.status_code == 422 and _locs(unknown) == [["person_id"]], unknown.text
    assert bad.status_code == 422 and _locs(bad) == [["person_id"]], bad.text
    assert cleared.status_code == 200, cleared.text
    assert [r["person_id"] for r in cleared.json()["limits"] if r["person_id"]] == [
        "100000000000000002"
    ], "the row removed by person id"


async def test_people_search_finds_chat_only_people_and_web_people_once_each(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """A chat user who never signed in to the web app is found by name or handle; a web
    person with a chat account is found by their web name, once, with their handle."""
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        async with db_session_factory.begin() as db:
            tenant = await get_tenant(db, admin.ws.tenant_id)
            account = await get_account(db, admin.ws.person.account_id)
            assert tenant is not None and account is not None, "the workspace and its person"
            await make_platform_principal(
                db, platform="discord", external_id=_P1, tenant=tenant, account=account
            )
            names = {
                _P1: KnownName("Ada L", "lovelace"),
                "100000000000000002": KnownName("Adam Chat"),
            }
            for user in names:
                if user != _P1:
                    await get_or_create_platform_principal(
                        db, tenant_id=tenant.id, platform="discord", external_id=user
                    )
            await upsert_user_names(db, tenant_id=tenant.id, platform="discord", names=names)
        found = await admin.client.get("/api/support/people", params={"q": "AD"})
        by_handle = await admin.client.get("/api/support/people", params={"q": "LOVEL"})
        none = await admin.client.get("/api/support/people", params={"q": "zzz"})
        blank = await admin.client.get("/api/support/people", params={"q": "  "})
        missing = await admin.client.get("/api/support/people")
        long = await admin.client.get("/api/support/people", params={"q": "a" * 101})

    assert found.status_code == 200, found.text
    assert found.json() == [
        {"id": _P1, "name": "Ada", "handle": "lovelace"},
        {"id": "100000000000000002", "name": "Adam Chat", "handle": None},
    ], "the web person under their web name with the chat handle, the chat-only person; each once"
    assert [p["id"] for p in by_handle.json()] == [_P1], "found by handle"
    assert none.json() == [] and blank.json() == [], "no match, and a blank query, are empty"
    assert missing.status_code == 422 and long.status_code == 422, "q is 1 to 100 characters"


async def test_people_search_offers_only_listed_members_once_the_list_is_loaded(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """With no member list nobody named is hidden; with one loaded in full, only members."""
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        tenant_id = admin.ws.tenant_id
        async with db_session_factory.begin() as db:
            for user, name in (("1", "Sam In"), ("2", "Sam Gone")):
                await get_or_create_platform_principal(
                    db, tenant_id=tenant_id, platform="discord", external_id=user
                )
                await upsert_user_names(
                    db, tenant_id=tenant_id, platform="discord", names={user: KnownName(name)}
                )
        before = await admin.client.get("/api/support/people", params={"q": "sam"})
        async with db_session_factory.begin() as db:
            await members_store.replace_members(
                db,
                tenant_id=tenant_id,
                members={"1": False},
                workspace_name="Guild",
                now=datetime.now(UTC),
            )
        after = await admin.client.get("/api/support/people", params={"q": "sam"})

    assert [p["id"] for p in before.json()] == ["2", "1"], "named people, listed or not"
    assert [p["id"] for p in after.json()] == ["1"], "a loaded list narrows to its members"


async def test_the_request_channel_is_locked_when_own_channels_are_off(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """The deployment's channel shows as the default; a PUT is a 403 and the value is null."""
    async with _support(web_client, db_session_factory, tmp_path, settings=_LOCKED) as admin:
        locked = await admin.client.put(
            "/api/support/channel", json={"channel_id": "c1", "expected_revision": None}
        )
        page = (await admin.client.get("/api/support")).json()

    assert locked.status_code == 403, locked.text
    channel = page["channel"]
    assert channel["own_allowed"] is False, "locked"
    assert (channel["value"], channel["revision"]) == (None, None), "no own pick shown"
    assert channel["default"] == {"name": "requests"}, "the deployment's channel, by name"
    assert channel["state"] == "ready" and channel["detail"] is None, "a destination exists"


async def test_the_request_channel_can_be_set_replaced_and_cleared_when_allowed(
    web_client: WebClientFactory,
    db_session_factory: Factory,
    tmp_path: Path,
) -> None:
    """With `support_own_channel` on (its default): set, stale 409, clear, and the audit rows."""
    async with _support(web_client, db_session_factory, tmp_path, settings=_UNSET) as admin:
        put = admin.client.put
        before = (await admin.client.get("/api/support")).json()["channel"]
        picked = await put(
            "/api/support/channel", json={"channel_id": "c1", "expected_revision": None}
        )
        stale = await put(
            "/api/support/channel", json={"channel_id": "c9", "expected_revision": None}
        )
        revision = picked.json()["channel"]["revision"]
        unknown = await put(
            "/api/support/channel", json={"channel_id": "zzz", "expected_revision": revision}
        )
        cleared = await put(
            "/api/support/channel", json={"channel_id": None, "expected_revision": revision}
        )
        trail = await shown_trail(db_session_factory, admin.ws.tenant_id)

    assert (before["own_allowed"], before["state"]) == (True, "none"), before
    assert before["detail"], "what to do when requests go nowhere"
    assert picked.status_code == 200, picked.text
    channel = picked.json()["channel"]
    assert channel["value"] == {"id": "c1", "name": "general"}, "the pick by name"
    assert channel["state"] == "ready", "a pick is a destination"
    assert stale.status_code == 409 and stale.json() == {"detail": _STALE}, stale.text
    assert unknown.status_code == 404, unknown.text
    assert unknown.json() == {"detail": "No such channel."}, "the text"
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["channel"]["value"] is None, "dropped: the default applies"
    assert trail[-2:] == [
        ("Changed support channel", "#general", "Request channel"),
        ("Changed support channel", "Default", "Request channel"),
    ], "set and cleared, audited"


async def test_a_channel_daimon_cannot_post_in_is_refused_with_the_fix_and_not_saved(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """A definite `blocked` is a 422 on `channel_id`; the page keeps its old value."""
    bot = _Bot(everyone=_VIEW)
    async with _support(
        web_client, db_session_factory, tmp_path, settings=_UNSET, bot=bot
    ) as admin:
        refused = await admin.client.put(
            "/api/support/channel", json={"channel_id": "c1", "expected_revision": None}
        )
        page = (await admin.client.get("/api/support")).json()["channel"]

    assert refused.status_code == 422, refused.text
    assert _locs(refused) == [["channel_id"]], "on the field"
    assert "Give Daimon Send Messages and Embed Links in #general." in refused.text, refused.text
    assert page["value"] is None, "nothing was saved"


async def test_a_check_that_cannot_run_is_unverified_and_the_pick_saves(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """A platform error is not a refusal: the pick saves and the page says it is unverified."""
    bot = _Bot(status=500)
    async with _support(
        web_client, db_session_factory, tmp_path, settings=_UNSET, bot=bot
    ) as admin:
        saved = await admin.client.put(
            "/api/support/channel", json={"channel_id": "c1", "expected_revision": None}
        )
        page = (await admin.client.get("/api/support")).json()["channel"]

    assert saved.status_code == 200, saved.text
    assert page["value"] == {"id": "c1", "name": "general"}, "saved"
    assert (page["state"], page["detail"]) == ("unverified", "Couldn't check. Try again."), page


async def test_a_saved_channel_that_loses_permissions_reads_blocked_and_can_still_be_cleared(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """The page names the fix; clearing a pick needs no check."""
    bot = _Bot()
    async with _support(
        web_client, db_session_factory, tmp_path, settings=_UNSET, bot=bot
    ) as admin:
        picked = await admin.client.put(
            "/api/support/channel", json={"channel_id": "c1", "expected_revision": None}
        )
        bot.everyone = _VIEW | _EMBED
        forget_visibility()
        page = (await admin.client.get("/api/support")).json()["channel"]
        cleared = await admin.client.put(
            "/api/support/channel",
            json={"channel_id": None, "expected_revision": picked.json()["channel"]["revision"]},
        )

    assert (page["state"], page["detail"]) == (
        "blocked",
        "Give Daimon Send Messages in #general.",
    ), page
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["channel"]["value"] is None, "cleared while blocked"


async def test_a_thread_cannot_be_the_request_channel(
    web_client: WebClientFactory,
    db_session_factory: Factory,
    tmp_path: Path,
) -> None:
    """Only channels: a thread id is a 404 like an unknown one."""
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        thread = await admin.client.put(
            "/api/support/channel", json={"channel_id": "t9", "expected_revision": None}
        )

    assert thread.status_code == 404, thread.text
    assert thread.json() == {"detail": "No such channel."}, "the text"


async def test_requests_go_nowhere_when_no_channel_is_set(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """No deployment channel and no own pick: state `none` with something to do."""
    async with _support(web_client, db_session_factory, tmp_path, settings=_UNSET) as admin:
        channel = (await admin.client.get("/api/support")).json()["channel"]

    assert (channel["state"], channel["default"]) == ("none", None), channel
    assert channel["detail"], "the next step"


async def test_places_list_the_agents_and_channels_with_an_ask_a_person_override(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """`places` are the channels and agents whose `support_escalation` is set, by name."""
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        async with db_session_factory.begin() as db:
            await set_feature_value(
                db,
                tenant_id=admin.ws.tenant_id,
                target=FeatureTarget(tier="channel", channel_id="c1"),
                key="support_escalation",
                value="off",
                set_by=admin.ws.person.account_id,
                surface="web",
            )
        page = (await admin.client.get("/api/support")).json()

    assert page["places"] == [{"scope": "channel", "id": "c1", "name": "general"}], (
        "the channel with its own setting"
    )
    assert {"scope": "channel", "scope_id": "c1", "name": "general", "menu": None} in page[
        "menu_use"
    ], "channels can pick a menu"
    assert page["menu_use"][0] == {
        "scope": "workspace",
        "scope_id": "",
        "name": "Workspace",
        "menu": None,
    }, "the workspace first"


async def test_requests_show_as_recent_used_open_and_undelivered(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """A charge under its limit, the open count, and the request no channel received."""
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        await admin.client.put(
            "/api/support/limits", json=_limit("channel", "c1", limit={"shared": "5"})
        )
        await _submit(db_session_factory, admin, "m1")
        second = await _submit(db_session_factory, admin, "m2")
        async with db_session_factory.begin() as db:
            await ledger.mark_delivered(db, escalation_id=second)
        page = (await admin.client.get("/api/support")).json()

    limit = _one(page["limits"], scope="channel", scope_id="c1")
    assert limit["used"] == "2", "both requests count against the limit"
    assert [(c["credits"], c["what"], c["person"]) for c in limit["recent"]] == [
        ("1", "Ask a person", "1"),
        ("1", "Ask a person", "1"),
    ], "newest first, by option and person"
    assert page["channel"]["open"]["count"] == 2, "fixed-price requests wait for Accept"
    assert [(u["credits"], u["charged"]) for u in page["undelivered"]] == [("1", False)], (
        "the undelivered one holds its price until a person handles it"
    )


async def test_a_range_priced_request_counts_as_open_until_settled(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """Pending requests are the open count, and show where they were posted."""
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        await admin.client.put(
            "/api/support/menus/support", json={"menu": _MENU, "expected_revision": None}
        )
        await admin.client.put(
            "/api/support/menus/use",
            json={"scope": "workspace", "menu": "support", "expected": None},
        )
        option = MenuSpec.model_validate(_MENU).options[0]
        await _submit(db_session_factory, admin, "m1", choice=RequestChoice.of(option))
        page = (await admin.client.get("/api/support")).json()

    assert page["channel"]["open"]["count"] == 1, page["channel"]


async def test_a_person_row_counts_what_a_request_would_charge_without_a_place_limit(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """Everywhere 5 total for Alice, no limit on the place: 4 spent shows 4 used and the 4 asks."""
    alice = "100000000000000002"
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        put = await admin.client.put(
            "/api/support/limits",
            json=_limit(limit={"each_person": "5"}, person_id=f"<@{alice}>"),
        )
        assert put.status_code == 200, put.text
        for n in range(4):
            await _submit(db_session_factory, admin, f"m{n}", user=alice)
        await _submit(db_session_factory, admin, "other", user="999")
        page = (await admin.client.get("/api/support")).json()

    row = _one(page["limits"], person_id=alice)
    assert (row["each_person"], row["used"]) == ("5", "4"), "4 of 5 used, so 1 left"
    assert len(row["recent"]) == 4, "only Alice's asks, on the ledger that charged them"


async def test_open_requests_name_each_channel_they_sit_in_once(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """`channel.open.where` is a list of distinct channel names, empty with none open."""
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        none = (await admin.client.get("/api/support")).json()["channel"]["open"]
        await admin.client.put(
            "/api/support/menus/support", json={"menu": _MENU, "expected_revision": None}
        )
        await admin.client.put(
            "/api/support/menus/use",
            json={"scope": "workspace", "menu": "support", "expected": None},
        )
        option = MenuSpec.model_validate(_MENU).options[0]
        ids = [
            await _submit(db_session_factory, admin, m, choice=RequestChoice.of(option))
            for m in ("m1", "m2")
        ]
        async with db_session_factory.begin() as db:
            for escalation_id, channel in zip(ids, ("c1", "c9"), strict=True):
                await ledger.mark_delivered(
                    db, escalation_id=escalation_id, support_channel_id=channel, support_post_id="p"
                )
        shown = (await admin.client.get("/api/support")).json()["channel"]["open"]

    assert none == {"count": 0, "where": []}, "nothing open"
    assert shown["count"] == 2 and sorted(shown["where"]) == ["general", "requests"], shown


async def test_a_fixed_price_request_is_accepted_or_declined_from_the_page(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """Accept charges its price, decline releases it; a handled one is 409; another amount 422."""
    fixed = _option_fixed()
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        await admin.client.put(
            "/api/support/menus/support", json={"menu": fixed, "expected_revision": None}
        )
        await admin.client.put(
            "/api/support/menus/use",
            json={"scope": "workspace", "menu": "support", "expected": None},
        )
        await admin.client.put(
            "/api/support/limits", json=_limit("channel", "c1", limit={"shared": "5"})
        )
        choice = RequestChoice.of(MenuSpec.model_validate(fixed).options[0])
        kept = await _submit(db_session_factory, admin, "m1", choice=choice)
        declined = await _submit(db_session_factory, admin, "m2", choice=choice)
        post = admin.client.post
        base = "/api/support/requests"
        listed = (await admin.client.get("/api/support")).json()
        other = await post(f"{base}/{kept}/settle", json={"charge": "1"})
        accept = await post(f"{base}/{kept}/settle", json={})
        decline = await post(f"{base}/{declined}/decline")
        again = await post(f"{base}/{kept}/decline")
        again_settle = await post(f"{base}/{declined}/settle", json={})
        trail = await shown_trail(db_session_factory, admin.ws.tenant_id)

    rows = {i["id"]: i for i in listed["undelivered"]}
    assert {(r["credits"], r["charged"]) for r in rows.values()} == {("2", False)}, rows
    assert set(rows) == {str(kept), str(declined)}, "both are listed"
    assert other.status_code == 422 and _locs(other) == [["charge"]], other.text
    assert accept.status_code == 200 and decline.status_code == 200, "accepted and declined"
    assert decline.json()["undelivered"] == [], "both left the list"
    limit = _one(decline.json()["limits"], scope="channel", scope_id="c1")
    assert limit["used"] == "2", "only the accepted charge is left on the limit"
    assert again.status_code == 409 and again_settle.status_code == 409, "already handled"
    details = [d for a, _, d in trail if "support request" in a]
    assert details[0] == "Accepted" and len(details) == 2, f"audited once each: {trail}"


def _option_fixed() -> dict[str, object]:
    """A menu of one fixed-price option (2 credits)."""
    option = {"key": "quick", "label": "Quick", "price": {"kind": "fixed", "amount": "2"}}
    return {"name": "Support", "options": [option]}


async def test_an_undelivered_request_is_settled_or_declined_from_the_page(
    web_client: WebClientFactory, db_session_factory: Factory, tmp_path: Path
) -> None:
    """Settle and decline answer with the page; refusals are 404, 409 and 422 on `charge`."""
    async with _support(web_client, db_session_factory, tmp_path) as admin:
        await admin.client.put(
            "/api/support/menus/support", json={"menu": _MENU, "expected_revision": None}
        )
        await admin.client.put(
            "/api/support/menus/use",
            json={"scope": "workspace", "menu": "support", "expected": None},
        )
        option = MenuSpec.model_validate(_MENU).options[0]
        first = await _submit(db_session_factory, admin, "m1", choice=RequestChoice.of(option))
        second = await _submit(db_session_factory, admin, "m2", choice=RequestChoice.of(option))
        page = (await admin.client.get("/api/support")).json()
        items = {i["id"]: i for i in page["undelivered"]}
        post = admin.client.post
        base = "/api/support/requests"
        unknown = await post(f"{base}/{uuid.uuid4()}/settle", json={"charge": "1"})
        malformed = await post(f"{base}/not-an-id/decline")
        bad = await post(f"{base}/{first}/settle", json={"charge": "abc"})
        over = await post(f"{base}/{first}/settle", json={"charge": "9"})
        settled = await post(f"{base}/{first}/settle", json={"charge": "2.5"})
        again = await post(f"{base}/{first}/settle", json={"charge": "2"})
        declined = await post(f"{base}/{second}/decline")
        declined_again = await post(f"{base}/{second}/decline")
        admin.as_member()
        refused = await post(f"{base}/{first}/decline")
        async with db_session_factory() as db:
            tenant_id = admin.ws.tenant_id
            row_one = await ledger.get_request(db, escalation_id=first, tenant_id=tenant_id)
            row_two = await ledger.get_request(db, escalation_id=second, tenant_id=tenant_id)
        trail = await shown_trail(db_session_factory, admin.ws.tenant_id)

    assert set(items) == {str(first), str(second)}, "both are listed by id"
    assert items[str(first)]["price"] == {"kind": "range", "min": "1", "max": "3"}, "its terms"
    assert items[str(first)]["credits"] == "1", "what it holds now: the lowest price"
    assert unknown.status_code == 404 and malformed.status_code == 404, "no such request"
    assert bad.status_code == 422 and _locs(bad) == [["charge"]], bad.text
    assert over.status_code == 422 and _locs(over) == [["charge"]], over.text
    assert settled.status_code == 200, settled.text
    assert "limits" in settled.json() and "undelivered" in settled.json(), "the whole page"
    assert [i["id"] for i in settled.json()["undelivered"]] == [str(second)], (
        "a settled request leaves the list"
    )
    assert declined.json()["undelivered"] == [], "so does a declined one"
    assert again.status_code == 409 and declined_again.status_code == 409, "no longer pending"
    assert declined.status_code == 200, declined.text
    assert refused.status_code == 403, "admins only"
    assert row_one is not None and (row_one.status, row_one.charge) == ("settled", Decimal("2.5"))
    assert row_two is not None and (row_two.status, row_two.charge) == ("declined", Decimal(0))
    actions = [a for a, _, _ in trail if "support request" in a]
    assert actions == ["Settled support request", "Declined support request"], "audited once each"
