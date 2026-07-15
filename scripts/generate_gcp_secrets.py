"""Emit `gcloud secrets versions add` commands to populate daimon's GCP secrets.

Auto-generates the machine-derivable secrets (MCP JWT, Fernet crypto key) and
leaves commented placeholders for operator-paste values. Secret VALUES are
printed to stdout for the operator to run — they never touch Terraform state.
"""

from __future__ import annotations

import secrets
import sys

from cryptography.fernet import Fernet


def _add(name: str, value: str) -> str:
    return f"printf %s {value!r} | gcloud secrets versions add {name} --data-file=-"


def main() -> int:
    print("# Auto-generated secret values — review, then run:")
    print(_add("DAIMON_MCP__JWT_SECRET", secrets.token_urlsafe(48)))
    print(_add("DAIMON_CRYPTO__KEYS", Fernet.generate_key().decode()))
    print(_add("DAIMON_NOTEBOOK__ADMIN_SECRET", secrets.token_urlsafe(32)))
    print()
    print("# Operator-paste values (fill in real values, uncomment, run):")
    for name in [
        "DAIMON_ANTHROPIC__API_KEY",
        "DAIMON_GEMINI__API_KEY",
        "DAIMON_DISCORD__BOT_TOKEN",
        "DAIMON_SLACK__BOT_TOKEN",
        "DAIMON_SLACK__APP_TOKEN",
        "DAIMON_SLACK__SIGNING_SECRET",
        "DAIMON_MCP__PUBLIC_URL",  # set from the run.app URL after Phase 2 deploy
    ]:
        print(f"# {_add(name, '<PASTE_VALUE>')}")
    print()
    print("# Stripe (placeholders let MCP boot with billing non-functional):")
    for name in [
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PRICE_10_USD",
        "STRIPE_PRICE_25_USD",
        "STRIPE_PRICE_50_USD",
        "STRIPE_PRICE_100_USD",
    ]:
        print(_add(name, "placeholder"))
    print()
    print("# Optional (GitHub repo auth, Google token broker) — uncomment if used:")
    for name in [
        "DAIMON_GITHUB__APP_ID",
        "DAIMON_GITHUB__APP_PRIVATE_KEY",
        "DAIMON_GITHUB__FALLBACK_PAT",
        "DAIMON_GITHUB__WEBHOOK_SECRET",
        "DAIMON_CREDENTIALS__GOOGLE_SA_JSON",
    ]:
        print(f"# {_add(name, '<PASTE_VALUE>')}")
    print()
    print("# Database DSN secrets (populated during deploy Phase 1 Task 7):")
    print("# Both DAIMON_DATABASE_URL and DAIMON_DATABASE__URL are constructed at deploy time")
    print("# by building: postgresql+asyncpg://daimon:<password>@<sql_private_ip>:5432/daimon")
    print("# Replace <password> with the Cloud SQL database password and <sql_private_ip>")
    print("# with the Cloud SQL private IP address, then create both secrets with this value.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
