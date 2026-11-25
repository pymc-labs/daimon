"""A person's own sign-in at a source's provider: OAuth authorization code (with PKCE) or device
code, through Daimon's own client there (from settings, else registered once, dynamically).

The sign-in proves who the person is at the source: `complete` links that identity (proof
`oauth`) with the connection and its sealed tokens in one transaction, and refuses an
identity linked to someone else. A link that was there before stays and is not the
connection's to end. Errors are short codes, never provider text.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import httpx
from daimon.core.config import DataSettings
from daimon.data import identity
from daimon.data.audit import record_audit
from daimon.data.identity import Conflict, IdentityKey, Missing, Proof, fence_account
from daimon.data.schema import Schemas
from daimon.data.sync.connector import Person
from daimon.data.sync.credentials import seal, unseal
from psycopg import AsyncConnection
from psycopg.rows import DictRow

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
PENDING = timedelta(minutes=10)
# A device code works for whoever types it, so it lives briefly (the prompt shows until when).
DEVICE_TTL = timedelta(minutes=5)
SKEW = timedelta(seconds=60)
"""A token this close to its end is refreshed first."""
DENIED = frozenset({400, 401, 403})
"""A refresh answered like this will not work again: the person must connect again."""


class Failed(Exception):
    """The provider refused or could not be reached; the message is a short code."""


class NotConfigured(Exception):
    """No client for this provider: none in settings, and it offers no registration."""


@dataclass(frozen=True)
class Provider:
    """Where a provider signs a person in, and where it says who they are."""

    id: str
    flow: Literal["code", "device"]
    scopes: tuple[str, ...] = ()
    issuer: str = ""
    """Discovery: `<issuer>/.well-known/oauth-authorization-server` fills unset endpoints."""
    authorize_url: str = ""
    token_url: str = ""
    device_url: str = ""
    register_url: str = ""
    whoami_url: str = ""
    """GET with the access token; else the user id is in the token answer."""
    subject_key: str = "sub"
    email_key: str = ""
    """Where `whoami_url` answers the person's email, kept only with `email_verified` true."""
    mcp_url: str = ""
    """Else the user is the answer of the MCP server's tool `whoami_tool`."""
    whoami_tool: str = ""


GOOGLE = Provider(
    "google",
    "code",
    ("openid", "email"),
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    whoami_url="https://openidconnect.googleapis.com/v1/userinfo",
    email_key="email",
)
BLUEDOT = Provider(
    "bluedot",
    "device",
    ("offline_access",),
    issuer="https://signin.bluedothq.com",
    mcp_url="https://app.bluedothq.com/api/v1/mcp",
    whoami_tool="get_current_user",
    subject_key="id",
)
NOTION = Provider("notion", "code", issuer="https://mcp.notion.com", subject_key="user_id")
PROVIDERS: dict[str, Provider] = {p.id: p for p in (GOOGLE, BLUEDOT, NOTION)}


@dataclass(frozen=True)
class Client:
    client_id: str
    client_secret: str | None = None


def configured_clients(settings: DataSettings) -> dict[str, Client]:
    """Daimon's own clients set in settings, by provider."""
    if not settings.google_oauth_client_id:
        return {}
    secret = settings.google_oauth_client_secret
    return {
        "google": Client(
            settings.google_oauth_client_id, secret.get_secret_value() if secret else None
        )
    }


@dataclass(frozen=True)
class Started:
    """Code flow: send the person to `url`. Device flow: show `user_code` and `url`, then poll."""

    flow: Literal["code", "device"]
    url: str
    pending_id: uuid.UUID
    user_code: str = ""
    interval: int = 5
    source: str = ""
    """The source's name, shown with the code so the person knows what they approve."""
    expires_at: datetime | None = None


@dataclass(frozen=True)
class Connection:
    """What a person may see of a connection: never a token."""

    source_id: uuid.UUID
    provider: str
    subject: str
    status: str
    made_link: bool


def _obj(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _short(resp: httpx.Response) -> str:
    try:
        e = _obj(resp.json()).get("error")
    except ValueError:
        e = None
    return (
        e if isinstance(e, str) and re.fullmatch(r"[a-z_]{1,40}", e) else f"http_{resp.status_code}"
    )


def _dotted(value: object, path: str) -> object:
    for k in path.split("."):
        value = _obj(value).get(k)
    return value


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _lifetime(tok: Mapping[str, object], *keys: str) -> datetime | None:
    for k in keys:
        if isinstance(seconds := tok.get(k), int | str) and str(seconds).isdigit():
            return _now() + timedelta(seconds=int(seconds))
    return None


@dataclass
class Connector:
    """Sign-ins for one deployment: its store, crypto keys, HTTP client, callback and clients."""

    schemas: Schemas
    keys: Sequence[str]
    http: httpx.AsyncClient
    redirect_uri: str
    clients: Mapping[str, Client] = field(default_factory=dict[str, Client])
    providers: Mapping[str, Provider] = field(default_factory=lambda: PROVIDERS)
    _meta: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])

    @property
    def _d(self) -> str:
        return self.schemas.data

    async def _endpoints(self, p: Provider) -> dict[str, str]:
        """What the provider sets by hand, else what its issuer publishes."""
        if p.issuer and p.id not in self._meta:
            r = await self.http.get(f"{p.issuer}/.well-known/oauth-authorization-server")
            if r.status_code != 200:
                raise Failed(_short(r))
            m = r.json()
            self._meta[p.id] = {
                k: str(m.get(f"{name}_endpoint") or "")
                for k, name in (
                    ("authorize", "authorization"),
                    ("token", "token"),
                    ("device", "device_authorization"),
                    ("register", "registration"),
                )
            }
        meta = self._meta.get(p.id, {})
        own = {
            "authorize": p.authorize_url,
            "token": p.token_url,
            "device": p.device_url,
            "register": p.register_url,
        }
        return {k: v or meta.get(k, "") for k, v in own.items()}

    async def _client(self, conn: AsyncConnection[DictRow], p: Provider) -> Client:
        """The client in settings, else the one registered here before, else one registered now."""
        if (set_up := self.clients.get(p.id)) is not None:
            return set_up
        await conn.execute(
            b"select pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"data-connect-client:{p.id}",),
        )
        cur = await conn.execute(
            f"select credential from {self._d}.connect_clients where provider = %s".encode(),
            (p.id,),
        )
        if row := await cur.fetchone():
            held = unseal(self.keys, row["credential"])
            return Client(held["client_id"], held.get("client_secret"))
        url = (await self._endpoints(p))["register"]
        if not url:
            raise NotConfigured(p.id)
        r = await self.http.post(
            url,
            json={
                "client_name": "Daimon",
                "redirect_uris": [self.redirect_uri],
                "token_endpoint_auth_method": "none",
                "response_types": ["code"],
                "grant_types": ["authorization_code", "refresh_token", DEVICE_GRANT],
            },
        )
        if r.status_code not in (200, 201) or not r.json().get("client_id"):
            raise Failed(_short(r))
        got = {k: str(r.json()[k]) for k in ("client_id", "client_secret") if r.json().get(k)}
        await conn.execute(
            f"insert into {self._d}.connect_clients (provider, credential)"
            " values (%s, %s)".encode(),
            (p.id, seal(self.keys, got)),
        )
        return Client(got["client_id"], got.get("client_secret"))

    async def _exchange(
        self, conn: AsyncConnection[DictRow], p: Provider, data: dict[str, str]
    ) -> tuple[int, dict[str, object]]:
        cl = await self._client(conn, p)
        auth = {"client_secret": cl.client_secret} if cl.client_secret else {}
        r = await self.http.post(
            (await self._endpoints(p))["token"],
            data={**data, "client_id": cl.client_id, **auth},
            headers={"Accept": "application/json"},
        )
        if r.status_code != 200:
            return r.status_code, {"error": _short(r)}
        return 200, _obj(r.json())

    async def _lock(
        self, conn: AsyncConnection[DictRow], account: uuid.UUID, source: uuid.UUID
    ) -> None:
        """The one lock of a connection: every change to it takes this first, to commit."""
        await conn.execute(
            b"select pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"data-connect:{account}:{source}",),
        )

    async def _source(
        self, conn: AsyncConnection[DictRow], tenant_id: uuid.UUID, source_id: uuid.UUID
    ) -> tuple[Provider, str, str]:
        """The source's provider, the workspace its identities live in, and its name."""
        cur = await conn.execute(
            f"select name, provider, kind, config from {self._d}.sources where id = %s"
            " and tenant_id = %s and status <> 'disabled'".encode(),
            (source_id, tenant_id),
        )
        row = await cur.fetchone()
        p = self.providers.get(row["provider"] or row["kind"]) if row else None
        if row is None or p is None:
            raise Missing("You can't sign in to that source here.")
        return p, str(row["config"].get("workspace") or source_id), row["name"]

    # ---- sign in ------------------------------------------------------------------------------

    async def start(
        self,
        conn: AsyncConnection[DictRow],
        *,
        tenant_id: uuid.UUID,
        account_id: uuid.UUID,
        source_id: uuid.UUID,
    ) -> Started:
        """Begin a sign-in for this source; one open sign-in per person and source."""
        async with conn.transaction():
            await fence_account(conn, self.schemas, account_id)
            await self._lock(conn, account_id, source_id)
            p, _, name = await self._source(conn, tenant_id, source_id)
            cl, ep = await self._client(conn, p), await self._endpoints(p)
            await conn.execute(
                f"delete from {self._d}.connect_pending where account_id = %s"
                " and source_id = %s".encode(),
                (account_id, source_id),
            )
            pending, state = uuid.uuid4(), secrets.token_urlsafe(24)
            if p.flow == "code":
                verifier = secrets.token_urlsafe(48)
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                query = httpx.QueryParams(
                    {
                        "response_type": "code",
                        "client_id": cl.client_id,
                        "redirect_uri": self.redirect_uri,
                        "state": state,
                        "scope": " ".join(p.scopes),
                        "code_challenge": challenge.rstrip(b"=").decode(),
                        "code_challenge_method": "S256",
                    }
                )
                secret, expires, started = (
                    {"verifier": verifier},
                    _now() + PENDING,
                    Started("code", f"{ep['authorize']}?{query}", pending),
                )
            else:
                r = await self.http.post(
                    ep["device"], data={"client_id": cl.client_id, "scope": " ".join(p.scopes)}
                )
                if r.status_code != 200:
                    raise Failed(_short(r))
                d = r.json()
                secret = {"device_code": str(d["device_code"])}
                expires = _now() + min(timedelta(seconds=int(d.get("expires_in", 600))), DEVICE_TTL)
                url = str(d.get("verification_uri_complete") or d["verification_uri"])
                started = Started(
                    "device", url, pending, str(d["user_code"]), int(d.get("interval", 5))
                )
            await conn.execute(
                f"insert into {self._d}.connect_pending (id, tenant_id, account_id, source_id,"
                " provider, flow, state_hash, secret, expires_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)".encode(),
                (
                    pending,
                    tenant_id,
                    account_id,
                    source_id,
                    p.id,
                    p.flow,
                    _sha(state) if p.flow == "code" else None,
                    seal(self.keys, secret),
                    expires,
                ),
            )
        return replace(started, source=name, expires_at=expires)

    async def _pending(
        self, conn: AsyncConnection[DictRow], account_id: uuid.UUID, where: str, arg: object
    ) -> DictRow:
        """The person's open sign-in, locked: the account, then the connection, then the row."""
        find = (
            f"select * from {self._d}.connect_pending where {where} and account_id = %s"
            " and expires_at > now()"
        )
        cur = await conn.execute(find.encode(), (arg, account_id))
        if row := await cur.fetchone():
            await fence_account(conn, self.schemas, account_id)
            await self._lock(conn, account_id, row["source_id"])
            row = await (
                await conn.execute(f"{find} for update".encode(), (arg, account_id))
            ).fetchone()
        if row is None:
            raise Missing("That sign-in expired. Start again.")
        return row

    async def finish_code(
        self, conn: AsyncConnection[DictRow], *, account_id: uuid.UUID, state: str, code: str
    ) -> Connection:
        """The provider sent the person back with a code: exchange it and connect."""
        async with conn.transaction():
            row = await self._pending(
                conn, account_id, "flow = 'code' and state_hash = %s", _sha(state)
            )
            p, ws, _ = await self._source(conn, row["tenant_id"], row["source_id"])
            verifier = unseal(self.keys, row["secret"])["verifier"]
            status, tok = await self._exchange(
                conn,
                p,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "code_verifier": verifier,
                },
            )
            if status != 200:
                raise Failed(str(tok["error"]))
            return await self._complete(conn, row, p, ws, tok)

    async def poll(
        self, conn: AsyncConnection[DictRow], *, account_id: uuid.UUID, pending_id: uuid.UUID
    ) -> Connection | None:
        """One poll of a device sign-in; None until the person approves it."""
        failed: str | None = None
        async with conn.transaction():
            row = await self._pending(conn, account_id, "flow = 'device' and id = %s", pending_id)
            p, ws, _ = await self._source(conn, row["tenant_id"], row["source_id"])
            device = unseal(self.keys, row["secret"])["device_code"]
            status, tok = await self._exchange(
                conn, p, {"grant_type": DEVICE_GRANT, "device_code": device}
            )
            if status == 200:
                return await self._complete(conn, row, p, ws, tok)
            if tok["error"] in ("authorization_pending", "slow_down"):
                return None
            failed = str(tok["error"])
            await conn.execute(
                f"delete from {self._d}.connect_pending where id = %s".encode(), (row["id"],)
            )
        raise Failed(failed)

    async def _whoami(self, p: Provider, tok: Mapping[str, object]) -> tuple[str, str | None]:
        """The provider's id of who signed in, and the email it verified for them (if any)."""
        body: object = tok
        if p.whoami_url:
            r = await self.http.get(
                p.whoami_url,
                headers={
                    "Authorization": f"Bearer {tok.get('access_token')}",
                    "Accept": "application/json",
                },
            )
            body = r.json() if r.status_code == 200 else {}
        elif p.mcp_url:
            body = await self._mcp_user(p, str(tok.get("access_token")))
        found = _dotted(body, p.subject_key)
        if found in (None, ""):
            raise Failed("no_subject")
        email = _dotted(body, p.email_key) if p.email_key else None
        verified = _dotted(body, "email_verified") in (True, "true")
        return str(found), str(email) if isinstance(email, str) and email and verified else None

    async def _mcp_user(self, p: Provider, access: str) -> object:
        """What the provider's MCP server says of the token's user: `initialize`, then the
        `whoami_tool` call. Empty on any refusal."""
        head = {
            "Authorization": f"Bearer {access}",
            "Accept": "application/json, text/event-stream",
        }

        async def rpc(msg: dict[str, object], session: str | None = None) -> httpx.Response:
            h = {**head, **({"Mcp-Session-Id": session} if session else {})}
            return await self.http.post(p.mcp_url, headers=h, json={"jsonrpc": "2.0", **msg})

        r = await rpc(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "Daimon", "version": "1"},
                },
            }
        )
        if r.status_code != 200:
            return {}
        sid = r.headers.get("mcp-session-id")
        await rpc({"method": "notifications/initialized"}, sid)
        r = await rpc(
            {
                "id": 2,
                "method": "tools/call",
                "params": {"name": p.whoami_tool, "arguments": {}},
            },
            sid,
        )
        if r.status_code != 200:
            return {}
        text = r.text
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            text = next((x[5:] for x in text.splitlines() if x.startswith("data:")), "{}")
        try:
            result = _obj(_obj(json.loads(text or "{}")).get("result"))
            if result.get("isError"):
                return {}
            body: object = result.get("structuredContent")
            if body is None:
                body = {}
                for x in _list(result.get("content")):
                    if _obj(x).get("type") == "text":
                        body = json.loads(str(_obj(x).get("text")))
                        break
        except ValueError:
            return {}
        user = _obj(body)
        return user if user.get("id") else _obj(user.get("user"))

    async def _complete(
        self,
        conn: AsyncConnection[DictRow],
        row: DictRow,
        p: Provider,
        ws: str,
        tok: Mapping[str, object],
    ) -> Connection:
        """Under the pending row's locks: the link (unless the person's already), the connection
        and its tokens, or Conflict and nothing written."""
        d, account, source, tenant = self._d, row["account_id"], row["source_id"], row["tenant_id"]
        subject, email = await self._whoami(p, tok)
        key = IdentityKey(p.id, ws, subject)
        cur = await conn.execute(
            f"select 1 from {d}.person_connections where source_id = %s and subject = %s"
            " and account_id <> %s".encode(),
            (source, subject, account),
        )
        if await cur.fetchone():
            raise Conflict("That account is linked to someone else.")
        cur = await conn.execute(
            f"select * from {d}.person_connections where account_id = %s and source_id = %s"
            " for update".encode(),
            (account, source),
        )
        old = await cur.fetchone()
        if old and old["subject"] != subject:
            await self._release_link(conn, old)
        cur = await conn.execute(
            f"select proof from {d}.identities where tenant_id = %s and provider = %s"
            " and workspace = %s and subject = %s and account_id = %s".encode(),
            (tenant, p.id, ws, subject, account),
        )
        held = await cur.fetchone()
        made = held is None or bool(old and old["subject"] == subject and old["made_link"])
        if held is None:
            await identity.link(
                conn,
                self.schemas,
                tenant_id=tenant,
                account_id=account,
                proof=Proof(key, "oauth", email),
            )
        tokens = {k: str(tok[k]) for k in ("access_token", "refresh_token") if tok.get(k)}
        scope = tok.get("scope")
        await conn.execute(
            f"insert into {d}.person_connections as c (tenant_id, account_id, source_id, provider,"
            " workspace, subject, credential, scopes, access_expires_at, refresh_expires_at,"
            " last_ok_at, made_link) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)"
            " on conflict (account_id, source_id) do update set subject = excluded.subject,"
            " workspace = excluded.workspace, credential = excluded.credential,"
            " scopes = excluded.scopes, access_expires_at = excluded.access_expires_at,"
            " refresh_expires_at = excluded.refresh_expires_at, status = 'active',"
            " last_ok_at = now(), last_error = null, made_link = excluded.made_link,"
            " generation = c.generation + 1".encode(),
            (
                tenant,
                account,
                source,
                p.id,
                ws,
                subject,
                seal(self.keys, tokens),
                str(scope).split() if scope else list(p.scopes),
                _lifetime(tok, "expires_in"),
                _lifetime(tok, "refresh_token_expires_in", "refresh_expires_in"),
                made,
            ),
        )
        await conn.execute(f"delete from {d}.connect_pending where id = %s".encode(), (row["id"],))
        await record_audit(
            conn,
            self.schemas,
            tenant_id=tenant,
            kind="connection.connect",
            account_id=account,
            target=key.ref(),
            source_id=source,
        )
        return Connection(source, p.id, subject, "active", made)

    async def _release_link(self, conn: AsyncConnection[DictRow], row: DictRow) -> None:
        """End the link a connection made; one another connection of the person relies on stays
        and that connection takes it over. A link that was there before it is not its to end."""
        if not row["made_link"]:
            return
        d = self._d
        same = (
            row["account_id"],
            row["tenant_id"],
            row["provider"],
            row["workspace"],
            row["subject"],
            row["id"],
        )
        cur = await conn.execute(
            f"update {d}.person_connections set made_link = true where account_id = %s"
            " and tenant_id = %s and provider = %s and workspace = %s and subject = %s"
            " and id <> %s returning 1".encode(),
            same,
        )
        if await cur.fetchone():
            return
        await conn.execute(
            f"update {d}.identities set account_id = null, proof = null, linked_at = null"
            " where account_id = %s and tenant_id = %s and provider = %s and workspace = %s"
            " and subject = %s and proof = 'oauth'".encode(),
            same[:5],
        )

    # ---- keeping a token good -----------------------------------------------------------------

    async def token(
        self, conn: AsyncConnection[DictRow], *, account_id: uuid.UUID, source_id: uuid.UUID
    ) -> str | None:
        """The person's access token for this source, refreshed when it ends soon. None: not
        connected, or they must connect again. One refresh at a time per connection, so a rotated
        refresh token is never used twice; a caller that waited gets the new token."""
        d = self._d
        async with conn.transaction():
            await self._lock(conn, account_id, source_id)
            cur = await conn.execute(
                f"select * from {d}.person_connections where account_id = %s and source_id = %s"
                " and status = 'active' for update".encode(),
                (account_id, source_id),
            )
            row = await cur.fetchone()
            if row is None or not row["credential"] or row["provider"] not in self.providers:
                return None
            held = unseal(self.keys, row["credential"])
            ends = row["access_expires_at"]
            if ends is None or ends > _now() + SKEW:
                return held.get("access_token")
            if not held.get("refresh_token"):
                await self._fail(conn, row["id"], "no_refresh_token", reconnect=True)
                return None
            p = self.providers[row["provider"]]
            try:
                status, tok = await self._exchange(
                    conn, p, {"grant_type": "refresh_token", "refresh_token": held["refresh_token"]}
                )
            except (httpx.HTTPError, Failed, NotConfigured) as e:
                status, tok = 0, {"error": type(e).__name__.lower()}
            if status != 200 or not tok.get("access_token"):
                await self._fail(
                    conn, row["id"], str(tok.get("error", "no_token")), reconnect=status in DENIED
                )
                return None
            held = {
                "access_token": str(tok["access_token"]),
                "refresh_token": str(tok.get("refresh_token") or held["refresh_token"]),
            }
            await conn.execute(
                f"update {d}.person_connections set credential = %s, access_expires_at = %s,"
                " refresh_expires_at = coalesce(%s, refresh_expires_at), last_ok_at = now(),"
                " last_error = null where id = %s".encode(),
                (
                    seal(self.keys, held),
                    _lifetime(tok, "expires_in"),
                    _lifetime(tok, "refresh_token_expires_in", "refresh_expires_in"),
                    row["id"],
                ),
            )
            return held["access_token"]

    async def _fail(
        self, conn: AsyncConnection[DictRow], connection_id: uuid.UUID, why: str, *, reconnect: bool
    ) -> None:
        await conn.execute(
            f"update {self._d}.person_connections set last_error = %s,"
            " status = case when %s then 'reconnect' else status end where id = %s".encode(),
            (why, reconnect, connection_id),
        )

    async def disconnect(
        self, conn: AsyncConnection[DictRow], *, account_id: uuid.UUID, source_id: uuid.UUID
    ) -> None:
        """End the connection, its tokens and the link it made, in one transaction."""
        async with conn.transaction():
            await self._lock(conn, account_id, source_id)
            cur = await conn.execute(
                f"select * from {self._d}.person_connections where account_id = %s"
                " and source_id = %s for update".encode(),
                (account_id, source_id),
            )
            row = await cur.fetchone()
            if row is None:
                raise Missing("You're not connected to that source.")
            await self._release_link(conn, row)
            await conn.execute(
                f"delete from {self._d}.person_connections where id = %s".encode(), (row["id"],)
            )
            await record_audit(
                conn,
                self.schemas,
                tenant_id=row["tenant_id"],
                kind="connection.disconnect",
                account_id=account_id,
                source_id=source_id,
            )


async def connected_people(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    keys: Sequence[str],
    clients: Mapping[str, Client],
    transport: httpx.AsyncBaseTransport,
    source_id: uuid.UUID,
) -> tuple[Person, ...]:
    """The people connected to a source, each with a good access token (refreshed when it ends
    soon), or none when the connection no longer proves itself."""
    cur = await conn.execute(
        f"select account_id, subject, status from {schemas.data}.person_connections"
        " where source_id = %s order by account_id".encode(),
        (source_id,),
    )
    rows = await cur.fetchall()
    out: list[Person] = []
    async with httpx.AsyncClient(timeout=20, transport=transport) as http:
        sign_in = Connector(schemas, keys, http, "", clients)
        for r in rows:
            token = (
                await sign_in.token(conn, account_id=r["account_id"], source_id=source_id)
                if r["status"] == "active"
                else None
            )
            out.append(Person(r["account_id"], r["subject"], token))
    return tuple(out)
