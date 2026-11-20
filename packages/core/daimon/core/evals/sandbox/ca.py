"""The eval CA: one RSA key in deployment secrets; the certificate is derived from it.

Sandbox tools trust the certificate; the gateway mints a certificate per host with the key.
The same key always gives the same certificate, so pods started at different times agree.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


class EvalCAError(ValueError):
    pass


@dataclass(frozen=True)
class EvalCA:
    cert_pem: bytes
    key_pem: bytes

    @property
    def gateway_pem(self) -> bytes:
        """Key and certificate in one file, as the gateway reads them."""
        return self.key_pem + self.cert_pem


def eval_ca(key_pem: str) -> EvalCA:
    try:
        key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    except ValueError as error:
        raise EvalCAError("the eval CA key is not a PEM private key") from error
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise EvalCAError("the eval CA key must be RSA, 2048 bits or more")
    public = key.public_key()
    der = public.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Daimon Eval CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public)
        .serial_number(int.from_bytes(hashlib.sha256(der).digest()[:16]) >> 1)
        # Wide on both sides: tools may run on a case's past clock.
        .not_valid_before(datetime(2000, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2099, 12, 31, tzinfo=UTC))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_out = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return EvalCA(cert.public_bytes(serialization.Encoding.PEM), key_out)
