"""Pod-local keys and controller-signed, UID-pinned server certificates."""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import time
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .pvc_store import PVCStore


def issue(csr_pem: bytes, ip: str, ca_cert: Path, ca_key: Path) -> bytes:
    csr = x509.load_pem_x509_csr(csr_pem)
    if not csr.is_signature_valid or not isinstance(csr.public_key(), ec.EllipticCurvePublicKey):
        raise ValueError("invalid CSR")
    ca = x509.load_pem_x509_certificate(ca_cert.read_bytes())
    key = serialization.load_pem_private_key(ca_key.read_bytes(), password=None)
    if not isinstance(key, (ec.EllipticCurvePrivateKey, rsa.RSAPrivateKey)):
        raise ValueError("unsupported signer key")
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ip)]))
        .issuer_name(ca.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(csr.public_key()), False)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]), False
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def prepare() -> tuple[str, str]:
    """Run as root before service UID drop; no private material enters pod spec."""
    root = Path("/run/daimon-tls")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sandbox")]))
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )
    with httpx.Client(
        verify=os.environ["HARNESS_CONTROLLER_CA"], trust_env=False, timeout=10
    ) as client:
        for _ in range(90):
            try:
                response = client.post(
                    os.environ["HARNESS_CHECKPOINT_CONTROLLER_URL"] + "/internal/certificate",
                    json={"uid": os.environ["HARNESS_POD_UID"], "csr": csr.decode()},
                )
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            raise RuntimeError("sandbox certificate bootstrap unavailable")
    import io

    store = PVCStore(root)
    store.upload(
        "server.key",
        io.BytesIO(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        ),
    )
    store.upload("server.crt", io.BytesIO(response.content))
    return str(root / "server.crt"), str(root / "server.key")
