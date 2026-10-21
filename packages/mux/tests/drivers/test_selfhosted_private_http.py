"""Stored insecure-HTTP consent reaches the direct private-cluster transport."""

import httpx
import pytest
from mux.drivers.selfhosted.transport import DirectTransport


@pytest.mark.parametrize(
    "url", ["http://harness:8088", "http://harness.daimon.svc.cluster.local:8088"]
)
async def test_private_http_needs_explicit_opt_in(url):
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            DirectTransport(client, base_url=url, token="test")
        DirectTransport(client, base_url=url, token="test", allow_insecure_http=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example.com:8088",
        "http://169.254.169.254",
        "http://user:pass@harness:8088",
        "http://metadata.google.internal",
        "http://metadata",
        "http://[fd00:ec2::254]",
        "http://[::ffff:169.254.169.254]",
        "http://[::169.254.169.254]",
    ],
)
async def test_http_opt_in_does_not_allow_external_or_metadata_or_credentials(url):
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            DirectTransport(client, base_url=url, token="test", allow_insecure_http=True)
