"""Google sign-in for guests: the code exchange and the identity it proves.

Only an address Google confirms counts, since a guest is matched to their invitation by it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from daimon.core.platform_sign_in import PlatformSignInError
from daimon.core.web_identity import VerifiedIdentity, WebSignInRefused

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_SIGN_IN_SCOPES: tuple[str, ...] = ("openid", "email", "profile")


def google_authorize_url(
    *, client_id: str, redirect_uri: str, state: str, code_challenge: str
) -> str:
    """Google's consent URL. The account picker always shows: guests often hold several
    Google accounts, and the invitation names one."""
    query = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(GOOGLE_SIGN_IN_SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(query)}"


async def exchange_google_code(
    http: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> str:
    """Trade an authorization code for the user's access token."""
    response = await http.post(
        GOOGLE_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not isinstance(token, str) or not token:
        raise PlatformSignInError("google exchange returned no access token")
    return token


@dataclass(frozen=True, slots=True)
class GoogleProfile:
    subject: str
    email: str | None
    email_verified: bool
    name: str | None


async def fetch_google_profile(http: httpx.AsyncClient, *, access_token: str) -> GoogleProfile:
    """The token's account: its stable id, address and whether Google confirmed it."""
    response = await http.get(
        GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
    )
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    subject, email, name = body.get("sub"), body.get("email"), body.get("name")
    if not isinstance(subject, str) or not subject:
        raise PlatformSignInError("google profile named no account")
    return GoogleProfile(
        subject=subject,
        email=email if isinstance(email, str) else None,
        email_verified=body.get("email_verified") is True,
        name=name if isinstance(name, str) else None,
    )


def google_identity(profile: GoogleProfile) -> VerifiedIdentity:
    """The identity a profile proves; refused unless Google confirmed its address, and
    for a non-ASCII one, which sharing by email cannot match."""
    if not profile.email or not profile.email_verified:
        raise WebSignInRefused("Google hasn't confirmed your email address.")
    email = profile.email.strip().lower()
    if not email.isascii():
        raise WebSignInRefused("Sign in with an email address in plain letters.")
    return VerifiedIdentity(
        provider="google",
        subject=profile.subject,
        display_name=profile.name or email.split("@")[0],
        email=email,
    )
