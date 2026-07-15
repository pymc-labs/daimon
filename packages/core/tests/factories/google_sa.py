"""Ephemeral Google Service Account JSON factory for Phase 19 GWS provider tests.

Generates an in-memory RSA keypair on every call and emits a dict shaped like
the JSON google-auth's `Credentials.from_service_account_info` accepts. Keys
never touch disk and are scoped to the test process.

Mitigation T-19-W0-01: Ephemeral keypair generated per call; never written to
disk; never committed; tests use it only inside process memory.
"""

from __future__ import annotations

from urllib.parse import quote

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def make_test_service_account_info(
    *,
    client_email: str = "test-sa@example.iam.gserviceaccount.com",
) -> dict[str, str]:
    """Return a dict shaped like a real Google Service Account JSON.

    Generates a fresh RSA-2048 keypair on every call so the result satisfies
    `google.oauth2.service_account.Credentials.from_service_account_info`.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key_pem: str = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    return {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "test-key-id",
        "private_key": private_key_pem,
        "client_email": client_email,
        "client_id": "000000000000000000000",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": (
            f"https://www.googleapis.com/robot/v1/metadata/x509/{quote(client_email)}"
        ),
    }
