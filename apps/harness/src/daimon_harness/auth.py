"""Validate local bearer secrets or tenant-bound Entra access tokens."""

from __future__ import annotations

import asyncio
import hmac
import os
from uuid import UUID

import jwt
from fastapi import HTTPException
from jwt import PyJWKClient


class Authenticator:
    def __init__(self, token: str | None = None) -> None:
        self.mode = os.environ.get("HARNESS_AUTH_MODE", "bearer")
        self.allowed = {
            value.strip()
            for value in os.environ.get("HARNESS_ALLOWED_CALLERS", "").split(",")
            if value.strip()
        }
        self.gateway_ready = all(
            os.environ.get(name)
            for name in (
                "FOUNDRY_PROJECT_ENDPOINT",
                "FOUNDRY_AGENT_NAME",
                "FOUNDRY_AGENT_SESSION_ID",
            )
        )
        if self.mode not in {"bearer", "entra", "foundry_gateway"}:
            raise ValueError("unknown HARNESS_AUTH_MODE")
        if self.mode in {"entra", "foundry_gateway"} and not self.allowed:
            raise ValueError("HARNESS_ALLOWED_CALLERS is required for Entra and gateway auth")
        self.required_role = os.environ.get("HARNESS_REQUIRED_ROLE")
        self.token = token if token is not None else os.environ.get("HARNESS_TOKEN", "")
        self.audience = os.environ.get("HARNESS_ENTRA_AUDIENCE", "")
        tenant = os.environ.get("HARNESS_ENTRA_TENANT_ID", "")
        self.issuer = ""
        self.jwks: PyJWKClient | None = None
        if self.audience and tenant:
            tenant = str(UUID(tenant))
            self.issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
            self.jwks = PyJWKClient(
                f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
                cache_jwk_set=True,
                lifespan=3600,
            )
        if self.mode == "entra" and self.jwks is None:
            raise ValueError("configure HARNESS_ENTRA_AUDIENCE and tenant id")
        if self.mode == "bearer" and not self.token:
            raise ValueError("configure HARNESS_TOKEN or HARNESS_ENTRA_AUDIENCE and tenant id")

    async def verify(
        self,
        authorization: str | None,
        user_id: str | None = None,
        call_id: str | None = None,
    ) -> str:
        if self.mode == "foundry_gateway":
            if not self.gateway_ready or not user_id or not call_id:
                raise HTTPException(status_code=401, detail="Missing platform identity")
            if self.allowed and user_id not in self.allowed:
                raise HTTPException(status_code=403, detail="Caller is not allowed")
            return user_id
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Unauthorized")
        value = authorization[7:]
        if (
            self.mode == "bearer"
            and self.token
            and hmac.compare_digest(value.encode(), self.token.encode())
        ):
            return "bearer"
        if self.mode == "entra" and self.jwks is not None:
            try:
                key = await asyncio.to_thread(self.jwks.get_signing_key_from_jwt, value)
                claims = jwt.decode(
                    value,
                    key.key,
                    algorithms=["RS256"],
                    audience=self.audience,
                    issuer=[
                        self.issuer,
                        self.issuer.replace(
                            "https://login.microsoftonline.com/", "https://sts.windows.net/"
                        ).replace("/v2.0", "/"),
                    ],
                    options={"require": ["exp", "iat", "iss", "aud"]},
                )
                principals = {
                    value
                    for name in ("oid", "appid", "azp")
                    if isinstance(value := claims.get(name), str)
                }
                permitted = principals & self.allowed
                roles = claims.get("roles", [])
                if not permitted or (
                    self.required_role
                    and (not isinstance(roles, list) or self.required_role not in roles)
                ):
                    raise HTTPException(status_code=403, detail="Caller is not allowed")
                return sorted(permitted)[0]
            except jwt.PyJWTError:
                pass
        raise HTTPException(status_code=401, detail="Unauthorized")
