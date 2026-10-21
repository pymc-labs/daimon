"""The sandbox signer controls certificate authority, identity and key usage."""

import datetime as dt

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from daimon_harness.pod_tls import issue


def test_signer_ignores_requested_ca_and_client_authority(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    now = dt.datetime.now(dt.UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .sign(key, hashes.SHA256())
    )
    (tmp_path / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "ca.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    pod_key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(name)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False)
        .sign(pod_key, hashes.SHA256())
    )
    pem = issue(
        csr.public_bytes(serialization.Encoding.PEM),
        "10.0.0.2",
        tmp_path / "ca.crt",
        tmp_path / "ca.key",
    )
    cert = x509.load_pem_x509_certificate(pem)
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == [
        ExtendedKeyUsageOID.SERVER_AUTH
    ]
    assert (
        str(
            cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value.get_values_for_type(x509.IPAddress)[0]
        )
        == "10.0.0.2"
    )
    assert cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value.key_identifier
    assert cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
    cert.verify_directly_issued_by(ca)
