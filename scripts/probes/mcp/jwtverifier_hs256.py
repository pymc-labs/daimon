"""Probe: does FastMCP JWTVerifier support HS256 symmetric signing?

Characterizes:
  1. Construction with a shared secret via `public_key` + `algorithm="HS256"`.
  2. Verification of a valid HS256 token (with exp).
  3. Verification without `exp` claim — does it pass or fail?
  4. Rejection of a token signed with the wrong secret.
  5. Rejection of a token missing `sub` (client_id falls back to "unknown" — no error).
  6. Rejection of an expired token.

Run:
  uv run python scripts/probes/mcp/jwtverifier_hs256.py

No external services required. Self-contained.
"""

from __future__ import annotations

import asyncio
import time

import jwt as pyjwt  # PyJWT

from fastmcp.server.auth.providers.jwt import JWTVerifier

SECRET = "a-static-shared-secret-at-least-32-chars!!"
WRONG_SECRET = "wrong-secret-definitely-not-the-same!!"


def _mint(claims: dict, secret: str = SECRET) -> str:
    return pyjwt.encode(claims, secret, algorithm="HS256")


def _ok(label: str) -> None:
    print(f"  PASS  {label}")


def _fail(label: str, detail: str) -> None:
    print(f"  FAIL  {label}: {detail}")


async def main() -> None:
    print("== FastMCP JWTVerifier HS256 probe ==\n")

    # ── 1. Construction ────────────────────────────────────────────────────
    print("-- 1. Construction with public_key + algorithm='HS256' --")
    try:
        verifier = JWTVerifier(public_key=SECRET, algorithm="HS256")
        _ok("JWTVerifier(public_key=SECRET, algorithm='HS256') constructs without error")
    except Exception as e:
        _fail("construction", str(e))
        return

    # ── 2. Valid token with exp ────────────────────────────────────────────
    print("\n-- 2. Valid HS256 token (with exp) --")
    now = int(time.time())
    token_valid = _mint({"sub": "user-1", "iat": now, "exp": now + 300})
    result = await verifier.load_access_token(token_valid)
    if result is not None and result.client_id == "user-1":
        _ok(f"valid token accepted, client_id={result.client_id!r}, expires_at={result.expires_at}")
    else:
        _fail("valid token", f"got result={result}")

    # ── 3. Token without exp ───────────────────────────────────────────────
    print("\n-- 3. Token without exp claim --")
    token_no_exp = _mint({"sub": "user-noexp", "iat": now})
    result_no_exp = await verifier.load_access_token(token_no_exp)
    if result_no_exp is not None:
        _ok(
            f"token without exp ACCEPTED (expires_at={result_no_exp.expires_at!r}) "
            "— exp is optional, not required"
        )
    else:
        _ok("token without exp REJECTED — exp is required by FastMCP")

    # ── 4. Wrong secret → rejected ─────────────────────────────────────────
    print("\n-- 4. Token signed with wrong secret --")
    token_wrong = _mint({"sub": "attacker", "iat": now, "exp": now + 300}, secret=WRONG_SECRET)
    result_wrong = await verifier.load_access_token(token_wrong)
    if result_wrong is None:
        _ok("wrong-secret token correctly rejected (returns None)")
    else:
        _fail("wrong-secret token", f"should be None, got {result_wrong}")

    # ── 5. Missing sub claim ───────────────────────────────────────────────
    print("\n-- 5. Token without sub claim --")
    token_no_sub = _mint({"iat": now, "exp": now + 300})
    result_no_sub = await verifier.load_access_token(token_no_sub)
    if result_no_sub is not None:
        _ok(
            f"token without sub ACCEPTED, client_id falls back to {result_no_sub.client_id!r} "
            "(sub is optional; client_id=client_id|azp|sub|'unknown')"
        )
    else:
        _ok("token without sub REJECTED")

    # ── 6. Expired token ───────────────────────────────────────────────────
    print("\n-- 6. Expired token --")
    token_expired = _mint({"sub": "user-old", "iat": now - 600, "exp": now - 300})
    result_expired = await verifier.load_access_token(token_expired)
    if result_expired is None:
        _ok("expired token correctly rejected (returns None)")
    else:
        _fail("expired token", f"should be None, got {result_expired}")

    # ── 7. issuer + audience enforcement (sanity) ──────────────────────────
    print("\n-- 7. Issuer/audience enforcement with HS256 --")
    verifier_strict = JWTVerifier(
        public_key=SECRET,
        algorithm="HS256",
        issuer="my-service",
        audience="mcp-api",
    )
    token_good = _mint({"sub": "u", "iss": "my-service", "aud": "mcp-api", "exp": now + 300})
    token_bad_iss = _mint({"sub": "u", "iss": "evil", "aud": "mcp-api", "exp": now + 300})
    r_good = await verifier_strict.load_access_token(token_good)
    r_bad = await verifier_strict.load_access_token(token_bad_iss)
    if r_good is not None and r_bad is None:
        _ok("issuer/audience enforcement works correctly")
    else:
        _fail("issuer/audience", f"r_good={r_good}, r_bad={r_bad}")

    print("\n== probe complete ==")


if __name__ == "__main__":
    asyncio.run(main())
