"""Inside the offline harness: verify real MCP TLS without credentials/model I/O."""

import asyncio
import os
import ssl
import urllib.request
from pathlib import Path

from daimon_harness.model_adapter import bound_mcp_http_client

ca = Path(os.environ["HARNESS_MCP_CA_BUNDLE"])
assert str(ca) == "/etc/daimon/mcp-ca/ca.crt"
assert ca.stat().st_uid == 10000 and ca.stat().st_mode & 0o777 == 0o400
url = os.environ["HARNESS_DAIMON_MCP_URL"].removesuffix("/mcp") + "/healthz"
# Exercise the same read permissions as the running harness service.
os.setgroups([])
os.setgid(10000)
os.setuid(10000)


async def verify_harness_client():
    async with bound_mcp_http_client() as client:
        response = await client.get(url)
        assert response.status_code == 200


asyncio.run(verify_harness_client())
try:
    urllib.request.urlopen(url, context=ssl.create_default_context(), timeout=10)
except urllib.error.URLError as error:
    assert isinstance(error.reason, ssl.SSLCertVerificationError), type(error.reason)
else:
    raise AssertionError("MCP accepted without trusting its CA")
print("MCP HTTPS verified with chart CA as service UID; default trust rejected")
