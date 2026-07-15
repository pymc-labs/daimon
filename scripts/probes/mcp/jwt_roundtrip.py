"""Probe PyJWT HS256 sign/verify with only `sub` + `iat` — no exp, no jti.

Run:
    uv run --with pyjwt python scripts/probes/mcp/jwt_roundtrip.py
"""

from __future__ import annotations

import time

import jwt

SECRET = "test-secret-abc"


def case(label: str, fn):
    try:
        result = fn()
        print(f"[OK]   {label}: {result!r}")
    except Exception as e:  # noqa: BLE001
        print(f"[RAISE] {label}: {type(e).__name__}: {e}")


def main() -> None:
    now = int(time.time())

    # Sign.
    token = jwt.encode({"sub": "cli:testuser", "iat": now}, SECRET, algorithm="HS256")
    print(f"token: {token[:60]}...")

    # Roundtrip.
    case(
        "decode happy path (require sub,iat)",
        lambda: jwt.decode(
            token, SECRET, algorithms=["HS256"], options={"require": ["sub", "iat"]}
        ),
    )

    # Wrong secret.
    case(
        "decode wrong secret",
        lambda: jwt.decode(token, "other", algorithms=["HS256"]),
    )

    # No exp — PyJWT shouldn't complain unless we opt in.
    case(
        "decode with verify_exp=True (no exp in token)",
        lambda: jwt.decode(
            token, SECRET, algorithms=["HS256"], options={"verify_exp": True}
        ),
    )

    # Missing sub.
    tok_no_sub = jwt.encode({"iat": now}, SECRET, algorithm="HS256")
    case(
        "decode require sub on token without sub",
        lambda: jwt.decode(
            tok_no_sub, SECRET, algorithms=["HS256"], options={"require": ["sub"]}
        ),
    )

    # None algorithm — confirm rejected.
    tok_none = jwt.encode({"sub": "x"}, "", algorithm="none")
    case(
        "decode 'none' alg token with algorithms=['HS256']",
        lambda: jwt.decode(tok_none, SECRET, algorithms=["HS256"]),
    )

    # Tampered payload.
    parts = token.split(".")
    tampered = ".".join([parts[0], parts[1][:-1] + ("A" if parts[1][-1] != "A" else "B"), parts[2]])
    case(
        "decode tampered payload",
        lambda: jwt.decode(tampered, SECRET, algorithms=["HS256"]),
    )


if __name__ == "__main__":
    main()
