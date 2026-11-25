"""Google Workspace access for connectors: read-only calls made as one person of a domain.

A service account with domain-wide delegation signs a short assertion naming the person to
act as and trades it for an access token. The account either holds a key (the credential's
`service_account_key`) or, keyless, asks Google's IAM Credentials API to sign as the runtime's
own identity (the account named in `config.service_account`; the runtime needs Token Creator
on it). Every scope is read-only. Tokens are kept in memory per person and scope set, and every
call goes through the guarded transport and the source's request budget.

The directory (people and groups) is read through the same calls by `read_directory`.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from daimon.data.sync.connector import Json, Source
from daimon.data.sync.connectors.chat import obj, rows
from daimon.data.sync.http import GuardedTransport, Upstream, send

TOKEN_URL: Final = "https://oauth2.googleapis.com/token"
SIGN_URL: Final = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{}:signJwt"
METADATA_HOST: Final = "metadata.google.internal"
METADATA_URL: Final = (
    f"http://{METADATA_HOST}/computeMetadata/v1/instance/service-accounts/default/token"
)
GRANT: Final = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_SCOPE: Final = "https://www.googleapis.com/auth/"
SCOPES: Final = {
    "drive": (_SCOPE + "drive.readonly",),
    "calendar": (_SCOPE + "calendar.readonly",),
    "directory": (
        _SCOPE + "admin.directory.user.readonly",
        _SCOPE + "admin.directory.group.readonly",
    ),
}
HOSTS: Final = {
    "drive": "https://www.googleapis.com",
    "calendar": "https://www.googleapis.com",
    "directory": "https://admin.googleapis.com",
}
LIFETIME: Final = 3000.0
"""Seconds a token is used (Google's last 3600)."""
LIMITED: Final = {"userRateLimitExceeded", "rateLimitExceeded"}
"""A 403 that only asks the caller to slow down."""
PAUSES: Final = (1.0, 2.0, 4.0)

_who: contextvars.ContextVar[str] = contextvars.ContextVar("google_subject")


class ApiError(Upstream):
    """A Google call that failed: the HTTP status and Google's reason code, never its message
    (it may quote content)."""

    def __init__(self, status: int, reason: str, path: str) -> None:
        super().__init__(f"Google answered {status} {reason} for {path}".replace("  ", " "))
        self.status, self.reason = status, reason


class DelegationRefused(Upstream):
    """No token could be made for a person: suspended, deleted, or delegation is not set up."""


def reason_of(r: httpx.Response) -> str:
    try:
        error = obj(r.json().get("error"))
        first = (rows(error.get("errors")) or [{}])[0]
        return str(first.get("reason") or error.get("status") or "")
    except (ValueError, AttributeError):
        return ""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def read_key(source: Source) -> Json | None:
    """The service account key of the credential, or None for a keyless source."""
    raw = source.credentials.get("service_account_key", "").strip()
    if not raw:
        return None
    try:
        key = obj(json.loads(raw))
        if key.get("client_email") and key.get("private_key"):
            return key
    except ValueError:
        pass
    raise ValueError("the service account key is not a Google key file")


class _Delegated(httpx.Auth):
    """Adds the token of the person the current call acts as; a 401 gets one new token."""

    def __init__(self, google: Google, api: str) -> None:
        self.google, self.api = google, api

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        who = _who.get()
        for attempt in (0, 1):
            token = await self.google.token(who, self.api)
            request.headers["Authorization"] = f"Bearer {token}"
            response = yield request
            if response.status_code != 401 or attempt:
                return
            self.google.forget(who, self.api)


class Google:
    """Calls to Google as one person at a time: `await g.get(person, "drive", path, params)`.
    `make(hosts)` builds a transport that may also reach `hosts` when they are private."""

    def __init__(self, source: Source, make: Callable[[tuple[str, ...]], httpx.AsyncBaseTransport]):
        self.source, self._make = source, make
        self._key = read_key(source)
        self._account = str(
            (self._key or {}).get("client_email") or source.config.get("service_account") or ""
        )
        if not self._account:
            raise ValueError("config.service_account or a service account key is required")
        self._tokens: dict[tuple[str, str], tuple[float, str]] = {}
        self._clients = {
            api: httpx.AsyncClient(
                base_url=host, auth=_Delegated(self, api), timeout=60, transport=make(())
            )
            for api, host in HOSTS.items()
        }
        self._plain = httpx.AsyncClient(timeout=30, transport=make(()))

    async def aclose(self) -> None:
        for client in (*self._clients.values(), self._plain):
            await client.aclose()

    def forget(self, who: str, api: str) -> None:
        self._tokens.pop((who.lower(), api), None)

    async def token(self, who: str, api: str) -> str:
        hit = self._tokens.get((who.lower(), api))
        if hit and hit[0] > time.monotonic():
            return hit[1]
        now = int(time.time())
        claims = {
            "iss": self._account,
            "sub": who,
            "scope": " ".join(SCOPES[api]),
            "aud": TOKEN_URL,
            "iat": now,
            "exp": now + 3600,
        }
        assertion = self._sign(claims) if self._key else await self._sign_by_iam(claims)
        r = await self._plain.post(TOKEN_URL, data={"grant_type": GRANT, "assertion": assertion})
        if r.status_code != 200:
            try:
                code = str(r.json().get("error") or r.status_code)
            except ValueError:
                code = str(r.status_code)
            raise DelegationRefused(f"Google refused to act as {who}: {code}")
        token = str(r.json()["access_token"])
        self._tokens[(who.lower(), api)] = (time.monotonic() + LIFETIME, token)
        return token

    def _sign(self, claims: Json) -> str:
        assert self._key is not None
        head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        body = f"{head}.{_b64(json.dumps(claims).encode())}"
        pem = serialization.load_pem_private_key(self._key["private_key"].encode(), None)
        assert isinstance(pem, rsa.RSAPrivateKey), "a Google key file holds an RSA key"
        return f"{body}.{_b64(pem.sign(body.encode(), padding.PKCS1v15(), hashes.SHA256()))}"

    async def _sign_by_iam(self, claims: Json) -> str:
        """The assertion, signed by the IAM Credentials API as this runtime's identity. The
        metadata server is private by design: only its own name is allowed past the guard."""
        async with httpx.AsyncClient(timeout=10, transport=self._make((METADATA_HOST,))) as meta:
            r = await meta.get(METADATA_URL, headers={"Metadata-Flavor": "Google"})
        if r.status_code != 200:
            raise Upstream("keyless delegation needs a Google runtime identity, and none answered")
        r = await self._plain.post(
            SIGN_URL.format(quote(self._account)),
            json={"payload": json.dumps(claims)},
            headers={"Authorization": f"Bearer {r.json()['access_token']}"},
        )
        if r.status_code != 200:
            raise Upstream(
                f"Google refused to sign as {self._account} ({r.status_code}): "
                "the runtime identity needs Token Creator on it"
            )
        return str(r.json()["signedJwt"])

    async def _call(
        self, who: str, api: str, path: str, params: Mapping[str, object] | None
    ) -> httpx.Response:
        mark = _who.set(who)
        try:
            for pause in PAUSES:
                r = await send(
                    self._clients[api],
                    self.source.budget,
                    path,
                    params,
                    deadline=self.source.deadline,
                )
                if r.status_code != 403 or reason_of(r) not in LIMITED:
                    return r
                await asyncio.sleep(pause)
            return await send(
                self._clients[api], self.source.budget, path, params, deadline=self.source.deadline
            )
        finally:
            _who.reset(mark)

    async def get(
        self, who: str, api: str, path: str, params: Mapping[str, object] | None = None
    ) -> Json:
        r = await self._call(who, api, path, params)
        if r.status_code != 200:
            raise ApiError(r.status_code, reason_of(r), path.split("?")[0])
        return r.json()

    async def raw(
        self,
        who: str,
        api: str,
        path: str,
        params: Mapping[str, object] | None = None,
        cap: int | None = None,
    ) -> bytes | None:
        """A file's bytes; None when they are over `cap`."""
        r = await self._call(who, api, path, params)
        if r.status_code != 200:
            raise ApiError(r.status_code, reason_of(r), path)
        return None if cap is not None and len(r.content) > cap else r.content

    async def pages(
        self,
        who: str,
        api: str,
        path: str,
        key: str,
        params: Mapping[str, object] | None = None,
    ) -> AsyncIterator[Json]:
        """Every item under `key`, page by page."""
        query = dict(params or {})
        while True:
            body = await self.get(who, api, path, query)
            for item in rows(body.get(key)):
                yield item
            if not (more := body.get("nextPageToken")):
                return
            query["pageToken"] = more


@dataclass(frozen=True)
class Directory:
    """The domain's people and groups as the directory lists them now."""

    domain: str
    people: dict[str, Json]
    """Directory user id to user: internal people (guests left out), suspended included."""
    groups: dict[str, tuple[str, ...]]
    """Group email to the ids of its active members (nested groups flattened)."""

    @property
    def active(self) -> dict[str, str]:
        """Primary email to user id of every active person."""
        return {
            u["primaryEmail"].lower(): i
            for i, u in self.people.items()
            if not (u.get("suspended") or u.get("archived"))
        }


async def read_directory(g: Google, domain: str, who: str) -> Directory:
    """The domain's users and groups, read as the admin. A failed call raises, so a pass is
    never published from half a directory."""
    base = "/admin/directory/v1"
    users = [
        u
        async for u in g.pages(
            who, "directory", f"{base}/users", "users", {"domain": domain, "maxResults": 500}
        )
    ]
    people = {u["id"]: u for u in users if not u.get("isGuestUser")}
    ids = Directory(domain, people, {}).active
    groups: dict[str, tuple[str, ...]] = {}
    async for grp in g.pages(
        who, "directory", f"{base}/groups", "groups", {"domain": domain, "maxResults": 200}
    ):
        email = grp["email"].lower()
        members = [
            m
            async for m in g.pages(
                who,
                "directory",
                f"{base}/groups/{quote(email, safe='@')}/members",
                "members",
                {"includeDerivedMembership": "true", "maxResults": 200},
            )
        ]
        groups[email] = tuple(
            sorted(
                {
                    ids[e]
                    for m in members
                    if m.get("type", "USER") == "USER" and (e := str(m.get("email")).lower()) in ids
                }
            )
        )
    return Directory(domain, people, groups)


def connect(source: Source, transport: httpx.AsyncBaseTransport | None = None) -> Google:
    """Google calls for `source`, on its guarded transport (`transport` replaces it: tests)."""

    def make(hosts: tuple[str, ...]) -> httpx.AsyncBaseTransport:
        return transport or GuardedTransport((*source.private_hosts, *hosts))

    return Google(source, make)


async def person_email(g: Google, admin: str, user_id: str) -> str | None:
    """The primary address of one active directory user, read as the admin; None when the
    directory does not list them or they are suspended."""
    try:
        u = await g.get(
            admin,
            "directory",
            f"/admin/directory/v1/users/{quote(user_id, safe='')}",
            {"fields": "primaryEmail,suspended,archived,isGuestUser"},
        )
    except ApiError as e:
        if e.status in (403, 404):
            return None
        raise
    if u.get("suspended") or u.get("archived") or u.get("isGuestUser"):
        return None
    return str(u["primaryEmail"]).lower() if u.get("primaryEmail") else None
