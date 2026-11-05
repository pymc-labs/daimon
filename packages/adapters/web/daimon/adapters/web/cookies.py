"""The browser cookies: the session and the short-lived sign-in flow.

Both are `__Host-` prefixed (Secure, Path=/, no Domain) so no sibling subdomain can set
or shadow them. Only a local development server over plain http drops the prefix and
the Secure flag, because browsers refuse them there.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import timedelta

from cryptography.fernet import InvalidToken, MultiFernet
from daimon.core.config import WebSettings
from daimon.core.web_admin import token_sha256 as token_sha256
from starlette.requests import Request
from starlette.responses import Response

FLOW_TTL = timedelta(minutes=10)


def _secure(web: WebSettings) -> bool:
    return web.public_url.scheme == "https"


def session_cookie_name(web: WebSettings) -> str:
    return ("__Host-" if _secure(web) else "") + "daimon_web"


def flow_cookie_name(web: WebSettings) -> str:
    return ("__Host-" if _secure(web) else "") + "daimon_web_flow"


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def set_cookie(
    response: Response, web: WebSettings, *, name: str, value: str, max_age: timedelta
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=int(max_age.total_seconds()),
        path="/",
        secure=_secure(web),
        httponly=True,
        samesite="lax",
    )


def clear_cookie(response: Response, web: WebSettings, *, name: str) -> None:
    response.delete_cookie(name, path="/", secure=_secure(web), httponly=True, samesite="lax")


def session_token(request: Request, web: WebSettings) -> str | None:
    return request.cookies.get(session_cookie_name(web))


@dataclass(frozen=True)
class SignInFlow:
    """What the callback needs to finish what `start` began."""

    verifier: str
    state: str
    next: str
    # Set when a signed-in session connects this account to itself rather than signing in:
    # the session's id.
    connect_session: str | None = None


def seal_flow(fernet: MultiFernet, flow: SignInFlow) -> str:
    """Encrypt and authenticate the flow, so the cookie can be neither read nor forged."""
    payload = json.dumps(
        {
            "v": flow.verifier,
            "s": flow.state,
            "x": flow.next,
            "c": flow.connect_session,
        }
    ).encode()
    return fernet.encrypt(payload).decode()


def open_flow(fernet: MultiFernet, sealed: str | None) -> SignInFlow | None:
    """The flow in the cookie, or None when absent, tampered with or older than `FLOW_TTL`."""
    if not sealed:
        return None
    try:
        data = json.loads(fernet.decrypt(sealed.encode(), ttl=int(FLOW_TTL.total_seconds())))
        return SignInFlow(
            verifier=data["v"],
            state=data["s"],
            next=data["x"],
            connect_session=data.get("c"),
        )
    except (InvalidToken, ValueError, KeyError, TypeError):
        return None
