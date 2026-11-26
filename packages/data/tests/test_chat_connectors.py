"""The Discord and Slack connectors against recorded-shape fake servers, the guarded transport,
and the rate-limit handling they share."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import count

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.data.access import ReadContext, scoped, subject
from daimon.data.schema import Schemas
from daimon.data.sync import connectors  # noqa: F401  # pyright: ignore[reportUnusedImport]
from daimon.data.sync.connector import CONNECTORS, Json, Source
from daimon.data.sync.connectors.discord import Config as DiscordConfig
from daimon.data.sync.connectors.discord import snowflake
from daimon.data.sync.connectors.gdrive import Config as GDriveConfig
from daimon.data.sync.connectors.slack import Config as SlackConfig
from daimon.data.sync.credentials import seal
from daimon.data.sync.engine import Env, RunResult, run_source
from daimon.data.sync.http import GuardedTransport, Refused, send
from daimon.data.sync.rate import RateBudget
from daimon.testing.data_db import DataDb
from psycopg import AsyncConnection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

KEY = Fernet.generate_key().decode()
TODAY = datetime.now(UTC).date()
D1 = TODAY - timedelta(days=1)


@dataclass
class Store:
    conn: AsyncConnection[DictRow]
    schemas: Schemas
    env: Env
    tenant: uuid.UUID

    @property
    def d(self) -> str:
        return self.schemas.data

    async def add(self, kind: str, token: str, **config: object) -> uuid.UUID:
        cur = await self.conn.execute(
            f"insert into {self.d}.sources (tenant_id, kind, name, provider, config, credential,"
            " default_scope, publication) values (%s, %s, 'chat', %s, %s::jsonb, %s,"
            " 'internal', 'published') returning id".encode(),
            (self.tenant, kind, kind, Jsonb(config), seal((KEY,), {"token": token})),
        )
        row = await cur.fetchone()
        assert row
        return row["id"]

    async def configure(self, src: uuid.UUID, **config: object) -> None:
        await self.conn.execute(
            f"update {self.d}.sources set config = config || %s::jsonb where id = %s".encode(),
            (Jsonb(config), src),
        )

    async def run(self, src: uuid.UUID) -> RunResult:
        result = await run_source(self.conn, self.env, src)
        assert result.status == "ok", result.error
        return result

    async def bodies(self, src: uuid.UUID) -> dict[str, str]:
        cur = await self.conn.execute(
            f"select external_id, body from {self.d}.records where source_id = %s".encode(), (src,)
        )
        return {r["external_id"]: r["body"] for r in await cur.fetchall()}

    async def sees(self, provider: str, workspace: str, user: str) -> set[str]:
        ctx = ReadContext((self.tenant,), readers=(subject(provider, workspace, "user", user),))
        async with scoped(self.conn, self.schemas, ctx) as c:
            cur = await c.execute(f"select external_id from {self.d}.records".encode())
            return {r["external_id"] for r in await cur.fetchall()}

    async def accounts(self, provider: str) -> dict[str, tuple[str | None, bool]]:
        cur = await self.conn.execute(
            f"select subject, email, active from {self.d}.identities"
            " where tenant_id = %s and provider = %s".encode(),
            (self.tenant, provider),
        )
        return {r["subject"]: (r["email"], r["active"]) for r in await cur.fetchall()}


@pytest.fixture
async def store(data_db: DataDb) -> AsyncIterator[Store]:
    async with data_db.aconnect() as conn:
        yield Store(conn, data_db.schemas, Env(data_db.schemas, (KEY,)), uuid.uuid4())


def reply(status: int = 200, **body: object) -> httpx.Response:
    return httpx.Response(status, json=body)


def listing(items: object) -> httpx.Response:
    return httpx.Response(200, json=items)


def source_of(kind: str, token: str) -> Source:
    return Source(
        uuid.uuid4(),
        uuid.uuid4(),
        kind,
        kind,
        kind,
        {},
        {"token": token},
        "internal",
        {},
        None,
        RateBudget(0),
    )


# ---- Discord ---------------------------------------------------------------------------------

G, BOT, STAFF = "1100000000000000001", "1200000000000000001", "1300000000000000001"
ALICE, BOB, CAROL = (f"120000000000000000{n}" for n in (2, 3, 4))
GENERAL, PRIVATE, HIDDEN = (f"140000000000000000{n}" for n in (1, 2, 3))
EVERYONE_READS = str((1 << 10) | (1 << 16))
VIEW = "1024"


def overwrite(who: str, allow: str = "0", deny: str = "0") -> Json:
    return {"id": who, "type": 0, "allow": allow, "deny": deny}


class FakeGuild:
    """A Discord server: the routes the connector may call, and a record of every call."""

    def __init__(self) -> None:
        self.roles = {G: EVERYONE_READS, STAFF: "0"}
        self.members: dict[str, Json] = {}
        self.channels: dict[str, Json] = {}
        self.messages: dict[str, list[Json]] = {}
        self.refused: set[str] = set()
        self.calls: list[str] = []
        self._n = count(1)

    def person(self, uid: str, name: str, roles: Sequence[str] = (), bot: bool = False) -> None:
        user: Json = {"id": uid, "username": name.lower(), "global_name": name}
        self.members[uid] = {"user": {**user, **({"bot": True} if bot else {})}, "roles": [*roles]}

    def channel(self, cid: str, name: str, overwrites: Sequence[Json] = ()) -> None:
        self.channels[cid] = {
            "id": cid,
            "type": 0,
            "name": name,
            "permission_overwrites": [*overwrites],
        }
        self.messages[cid] = []

    def say(self, cid: str, uid: str, text: str, day: date, files: Sequence[Json] = ()) -> Json:
        mid = str(snowflake(day) + ((10 * 3_600_000) << 22) + next(self._n))
        m: Json = {
            "id": mid,
            "type": 0,
            "content": text,
            "author": self.members[uid]["user"],
            "attachments": [*files],
            "mentions": [],
        }
        self.messages[cid].append(m)
        return m

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.discordapp.com":
            return httpx.Response(200, content=b"spec text")
        assert request.headers["authorization"] == "Bot example-token"
        path = request.url.path.removeprefix("/api/v10")
        self.calls.append(path)
        parts = path.strip("/").split("/")
        if path == "/users/@me":
            return reply(id=BOT)
        if path == f"/guilds/{G}":
            roles = [{"id": k, "name": k, "permissions": v} for k, v in self.roles.items()]
            return reply(id=G, name="Example server", roles=roles)
        if path == f"/guilds/{G}/channels":
            return listing(
                [
                    {**c, "last_message_id": (self.messages[k] or [{}])[-1].get("id")}
                    for k, c in self.channels.items()
                ]
            )
        if path == f"/guilds/{G}/members":
            return listing(list(self.members.values()))
        if path == f"/guilds/{G}/threads/active":
            return reply(threads=[])
        assert parts[0] == "channels", path
        cid = parts[1]
        if cid in self.refused:
            return reply(403, code=50001, message="Missing Access")
        if parts[2:] == ["threads", "archived", "public"]:
            return reply(threads=[], has_more=False)
        if parts[2:] == ["messages"]:
            after = int(request.url.params["after"])
            return listing([m for m in self.messages[cid] if int(m["id"]) > after])
        if parts[2] == "messages":
            return listing(next(m for m in self.messages[cid] if m["id"] == parts[3]))
        raise AssertionError(path)


@pytest.fixture
def guild(monkeypatch: pytest.MonkeyPatch) -> FakeGuild:
    """A server of two channels everyone reads, one only staff read, and one the bot cannot."""
    fake = FakeGuild()
    fake.person(BOT, "Helper", [STAFF], bot=True)
    fake.person(ALICE, "Alice")
    fake.person(BOB, "Bob")
    fake.person(CAROL, "Carol", [STAFF])
    fake.channel(GENERAL, "general")
    fake.channel(PRIVATE, "staff", [overwrite(G, deny=VIEW), overwrite(STAFF, allow=VIEW)])
    fake.channel(HIDDEN, "vault", [overwrite(G, deny=VIEW)])
    spec = {
        "filename": "spec.txt",
        "content_type": "text/plain",
        "size": 9,
        "url": "https://cdn.discordapp.com/attachments/1/2/spec.txt",
    }
    fake.say(GENERAL, ALICE, "first", D1)
    fake.say(GENERAL, BOB, "second", TODAY, files=[spec])
    fake.say(PRIVATE, CAROL, "payroll", TODAY)
    fake.say(HIDDEN, CAROL, "never read", TODAY)
    monkeypatch.setattr(CONNECTORS["discord"], "transport", httpx.MockTransport(fake))
    return fake


def window(cid: str, day: date) -> str:
    return f"conversation:{cid}:{day.isoformat()}"


async def test_a_discord_window_is_read_by_the_members_the_server_lets_read_it(
    store: Store, guild: FakeGuild
) -> None:
    src = await store.add("discord", "example-token", workspace=G)
    await store.run(src)

    bodies = await store.bodies(src)
    assert set(bodies) == {window(GENERAL, D1), window(GENERAL, TODAY), window(PRIVATE, TODAY)}, (
        "a window per channel and day; the channel the bot cannot read is skipped"
    )
    assert "10:00 Alice: first" in bodies[window(GENERAL, D1)]
    assert "[file: spec.txt]" in bodies[window(GENERAL, TODAY)], "an attachment is listed"
    assert not [c for c in guild.calls if HIDDEN in c], "no call to a channel the bot cannot see"

    everywhere = {window(GENERAL, D1), window(GENERAL, TODAY)}
    assert await store.sees("discord", G, ALICE) == everywhere
    assert await store.sees("discord", G, CAROL) == {*everywhere, window(PRIVATE, TODAY)}, (
        "the staff role reads the staff channel"
    )
    assert set(await store.accounts("discord")) == {ALICE, BOB, CAROL}, "people only, no bot"


async def test_a_member_who_loses_a_role_loses_the_channel_and_a_day_is_rebuilt(
    store: Store, guild: FakeGuild
) -> None:
    src = await store.add("discord", "example-token", workspace=G)
    await store.run(src)

    guild.members[CAROL]["roles"] = []
    gone = guild.messages[GENERAL][0]
    guild.messages[GENERAL].remove(gone)
    guild.say(GENERAL, ALICE, "added later", D1)
    await store.run(src)

    assert await store.sees("discord", G, CAROL) == {window(GENERAL, D1), window(GENERAL, TODAY)}
    day = (await store.bodies(src))[window(GENERAL, D1)]
    assert "added later" in day and "first" not in day, "the day is read again whole"


async def test_an_excluded_channel_is_dropped_and_an_unreadable_one_is_kept(
    store: Store, guild: FakeGuild
) -> None:
    src = await store.add("discord", "example-token", workspace=G)
    await store.run(src)

    guild.refused.add(GENERAL)
    await store.run(src)
    assert window(GENERAL, TODAY) in await store.bodies(src), "a refused read deletes nothing"

    await store.configure(src, exclude=["staff"])
    await store.run(src)
    assert window(PRIVATE, TODAY) not in await store.bodies(src), "an excluded channel is dropped"
    assert window(GENERAL, TODAY) in await store.bodies(src)


async def test_a_discord_file_is_fetched_from_its_message(guild: FakeGuild) -> None:
    message = guild.messages[GENERAL][1]
    connector = CONNECTORS["discord"]
    got = await connector.fetch_file(
        source_of("discord", "example-token"), {"channel_id": GENERAL}, message["id"], "spec.txt"
    )
    assert got == (b"spec text", "text/plain")


# ---- Slack -----------------------------------------------------------------------------------

TEAM = "T0EXAMPLE01"
SBOT, SALICE, SBOB, SGUEST, SGONE, SOUTSIDER = (
    "U0BOT00001",
    "U0ALICE001",
    "U0BOB00001",
    "U0GUEST001",
    "U0GONE0001",
    "U0OUTSIDE1",
)
PUBLIC, PRIV, DIRECT, NOTJOINED = "C0PUBLIC01", "C0PRIVATE1", "D0DIRECT01", "C0NOTJOIN1"


class FakeSlack:
    """A Slack workspace: the methods the connector may call, and a record of every call."""

    def __init__(self) -> None:
        self.users: dict[str, Json] = {}
        self.channels: dict[str, Json] = {}
        self.members: dict[str, list[str]] = {}
        self.messages: dict[str, list[Json]] = {}
        self.calls: list[str] = []
        self._n = count(1)

    def person(
        self,
        uid: str,
        name: str,
        *,
        guest: bool = False,
        gone: bool = False,
        bot: bool = False,
        team: str = TEAM,
        email: str | None = None,
        confirmed: bool = False,
    ) -> None:
        self.users[uid] = {
            "id": uid,
            "team_id": team,
            "name": name.lower(),
            "real_name": name,
            "deleted": gone,
            "is_bot": bot,
            "is_restricted": guest,
            "is_email_confirmed": confirmed,
            "profile": {"display_name": name, **({"email": email} if email else {})},
        }

    def channel(
        self,
        cid: str,
        name: str,
        *,
        private: bool = False,
        joined: bool = True,
        direct: bool = False,
        members: Sequence[str] = (),
    ) -> None:
        created = datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=UTC) - timedelta(days=2)
        self.channels[cid] = {
            "id": cid,
            "name": name,
            "created": int(created.timestamp()),
            "is_private": private,
            "is_member": joined,
            "is_im": direct,
        }
        self.members[cid] = [*members]
        self.messages[cid] = []

    def say(
        self,
        cid: str,
        uid: str,
        text: str,
        day: date,
        thread: str | None = None,
        files: Sequence[Json] = (),
    ) -> str:
        base = int(datetime(day.year, day.month, day.day, 10, tzinfo=UTC).timestamp())
        ts = f"{base}.{next(self._n):06d}"
        profile = {"display_name": self.users[uid]["real_name"]}
        m: Json = {"ts": ts, "user": uid, "text": text, "user_profile": profile, "files": [*files]}
        if thread:
            m["thread_ts"] = thread
            parent = next(p for p in self.messages[cid] if p["ts"] == thread)
            parent["thread_ts"] = thread
            parent["reply_count"] = parent.get("reply_count", 0) + 1
        self.messages[cid].append(m)
        return ts

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "files.slack.com":
            assert request.headers["authorization"] == "Bearer xoxb-example"
            return httpx.Response(200, content=b"spec text")
        assert request.headers["authorization"] == "Bearer xoxb-example"
        method = request.url.path.removeprefix("/api/")
        self.calls.append(method)
        q = request.url.params
        cid = q.get("channel", "")
        assert cid != DIRECT, "a direct message is never read"
        if method == "auth.test":
            return reply(ok=True, team_id=TEAM, team="Example", url="https://example.slack.com/")
        if method == "conversations.list":
            return reply(ok=True, channels=list(self.channels.values()))
        if method == "users.list":
            return reply(ok=True, members=list(self.users.values()))
        if method == "users.info":
            return reply(ok=True, user=self.users[q["user"]])
        if method == "conversations.members":
            return reply(ok=True, members=self.members[cid])
        msgs = self.messages[cid]
        if method == "conversations.history":
            top = [m for m in msgs if m.get("thread_ts") in (None, m["ts"])]
            lo, hi = float(q.get("oldest", 0)), float(q.get("latest") or "inf")
            found = [m for m in top if lo <= float(m["ts"]) <= hi]
            return reply(ok=True, messages=found, has_more=False)
        if method == "conversations.replies":
            found = [m for m in msgs if q["ts"] in (m["ts"], m.get("thread_ts"))]
            return reply(ok=True, messages=found, has_more=False)
        raise AssertionError(method)


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch) -> FakeSlack:
    """Two full members, a guest, a deactivated member, a bot and an outsider; a public channel,
    a private one, a direct message and a public channel the bot has not joined."""
    fake = FakeSlack()
    fake.person(SBOT, "Helper", bot=True)
    fake.person(SALICE, "Alice", email="alice@example.com", confirmed=True)
    fake.person(SBOB, "Bob", email="bob@example.com")
    fake.person(SGUEST, "Guest", guest=True)
    fake.person(SGONE, "Gone", gone=True)
    fake.person(SOUTSIDER, "Outsider", team="T0OTHER0001")
    everyone = [SBOT, SALICE, SBOB, SGUEST, SGONE, SOUTSIDER]
    fake.channel(PUBLIC, "general", members=everyone)
    fake.channel(PRIV, "leads", private=True, members=[SALICE, SGONE])
    fake.channel(DIRECT, "", direct=True)
    fake.channel(NOTJOINED, "elsewhere", joined=False)
    fake.say(PUBLIC, SALICE, "hello", D1)
    fake.say(PUBLIC, SGUEST, "hi there", D1)
    fake.say(PRIV, SALICE, "budget", TODAY)
    fake.say(NOTJOINED, SALICE, "never read", TODAY)
    monkeypatch.setattr(CONNECTORS["slack"], "transport", httpx.MockTransport(fake))
    return fake


def day_window(cid: str, day: date) -> str:
    return f"conversation:{cid}:d:{day.isoformat()}"


async def test_a_slack_channel_is_read_by_its_members_and_people_are_listed(
    store: Store, workspace: FakeSlack
) -> None:
    src = await store.add("slack", "xoxb-example", workspace=TEAM)
    await store.run(src)

    bodies = await store.bodies(src)
    assert set(bodies) == {day_window(PUBLIC, D1), day_window(PRIV, TODAY)}, (
        "private channels the bot is in are read by default; the direct message and the "
        "channel the bot has not joined are never read"
    )
    assert "Alice: hello" in bodies[day_window(PUBLIC, D1)]
    assert await store.sees("slack", TEAM, SBOB) == {day_window(PUBLIC, D1)}
    assert await store.sees("slack", TEAM, SGUEST) == {day_window(PUBLIC, D1)}, "a guest in it"
    assert await store.sees("slack", TEAM, SALICE) == {
        day_window(PUBLIC, D1),
        day_window(PRIV, TODAY),
    }
    assert await store.sees("slack", TEAM, SGONE) == set(), "a deactivated member reads nothing"
    assert await store.accounts("slack") == {
        SALICE: ("alice@example.com", True),
        SBOB: (None, True),
        SGUEST: (None, True),
        SGONE: (None, False),
    }, "humans of the workspace; an email only where Slack confirmed it"


async def test_a_lost_slack_channel_is_hidden_at_once_restored_on_return_and_gone_after_30_days(
    store: Store, workspace: FakeSlack
) -> None:
    src = await store.add("slack", "xoxb-example", workspace=TEAM)
    await store.run(src)
    leads = day_window(PRIV, TODAY)

    workspace.channels[PRIV]["is_member"] = False
    await store.run(src)
    assert leads in await store.bodies(src), "kept while access may come back"
    assert leads not in await store.sees("slack", TEAM, SALICE), "hidden from every read at once"

    workspace.channels[PRIV]["is_member"] = True
    await store.run(src)
    assert leads in await store.sees("slack", TEAM, SALICE), "access back restores it"

    workspace.channels[PRIV]["is_member"] = False
    await store.run(src)
    await store.conn.execute(
        f"update {store.d}.source_cursors set state = jsonb_set(state, %s, %s)"
        " where source_id = %s".encode(),
        (["lost", PRIV], Jsonb((datetime.now(UTC) - timedelta(days=31)).isoformat()), src),
    )
    await store.run(src)
    assert leads not in await store.bodies(src), "lost for 30 days: its records go"
    assert day_window(PUBLIC, D1) in await store.bodies(src)


def test_slack_and_discord_recheck_a_week_and_slack_reads_private_channels_by_default() -> None:
    """A YAML `no` (False) turns a yes/no setting off, as the text `no` does."""
    s, d = SlackConfig.of({"workspace": TEAM}), DiscordConfig.of({"workspace": G})
    assert (s.days, d.days) == (7, 7), "both re-read the last 7 days for edits and deletes"
    assert s.private, "private channels the bot is in are read unless turned off"
    for off in ("no", False):
        assert not SlackConfig.of({"workspace": TEAM, "private": off}).private
        drive = {"domain": "x.example", "admin": "a@x.example", "personal_drive": off}
        assert not GDriveConfig.of(drive).personal_drive


async def test_a_slack_thread_is_a_window_of_its_own_and_goes_with_its_parent(
    store: Store, workspace: FakeSlack
) -> None:
    parent = workspace.say(PUBLIC, SALICE, "question", TODAY)
    workspace.say(PUBLIC, SBOB, "answer", TODAY, thread=parent)
    src = await store.add("slack", "xoxb-example", workspace=TEAM)
    await store.run(src)

    thread = f"conversation:{PUBLIC}:t:{parent}:{TODAY.isoformat()}"
    bodies = await store.bodies(src)
    assert "Bob: answer" in bodies[thread]
    assert "[thread: 1 reply]" in bodies[day_window(PUBLIC, TODAY)], "the parent shows its replies"

    workspace.messages[PUBLIC] = [
        m for m in workspace.messages[PUBLIC] if parent not in (m["ts"], m.get("thread_ts"))
    ]
    await store.run(src)
    assert thread not in await store.bodies(src), "a deleted parent takes its thread with it"


async def test_a_token_for_another_workspace_reads_nothing(
    store: Store, workspace: FakeSlack
) -> None:
    src = await store.add("slack", "xoxb-example", workspace="T0ANOTHER01")
    result = await run_source(store.conn, store.env, src)
    assert result.status == "error"
    assert await store.bodies(src) == {}
    assert "conversations.history" not in workspace.calls


async def test_a_slack_file_is_fetched_with_the_sources_token(workspace: FakeSlack) -> None:
    spec = {
        "name": "spec.txt",
        "mimetype": "text/plain",
        "size": 9,
        "url_private_download": "https://files.slack.com/files-pri/T-F/spec.txt",
    }
    ts = workspace.say(PUBLIC, SALICE, "attached", TODAY, files=[spec])
    got = await CONNECTORS["slack"].fetch_file(
        source_of("slack", "xoxb-example"), {"channel_id": PUBLIC}, ts, "spec.txt"
    )
    assert got == (b"spec text", "text/plain")


# ---- the guarded transport and the rate-limit answers ---------------------------------------


async def test_the_guarded_transport_checks_every_hop_and_sends_to_the_checked_address() -> None:
    reached: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request)
        if request.url.path == "/hop":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/inside"})
        return httpx.Response(200)

    transport = GuardedTransport(("10.0.0.0/8",), httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        with pytest.raises(Refused):
            await client.get("http://127.0.0.1/direct")
        assert (await client.get("http://10.1.2.3/listed")).status_code == 200
        with pytest.raises(Refused):
            await client.get("http://8.8.8.8/hop")
    assert [str(r.url) for r in reached] == ["http://10.1.2.3/listed", "http://8.8.8.8/hop"], (
        "a private address is refused, a listed one is reached, and a redirect inward is refused"
    )


async def test_a_rate_limited_answer_is_waited_out(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    answers = iter(
        [
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(
                200, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset-after": "3"}
            ),
        ]
    )
    transport = httpx.MockTransport(lambda _: next(answers))
    async with httpx.AsyncClient(base_url="https://example.com", transport=transport) as client:
        r = await send(client, RateBudget(0), "/x")
    assert r.status_code == 200
    assert slept == [2.0, 3.0], "the asked wait, then the wait for the emptied bucket"
