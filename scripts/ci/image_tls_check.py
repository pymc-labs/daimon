"""Reach each provider's API over TLS with the image's own certificate roots, sending no key.

A stale root store fails the handshake; a healthy one gets an HTTP status (401 or 403).
Run inside the built image: `docker run --rm -i --entrypoint python IMAGE - < this file`.
"""

import sys

import httpx
from mux.drivers import isolated_ssl_context

HOSTS = (
    "https://api.openai.com/v1/models",
    "https://api.anthropic.com/v1/models",
    "https://generativelanguage.googleapis.com/v1beta/models",
)

failed = False
with httpx.Client(verify=isolated_ssl_context(), trust_env=False, timeout=20) as client:
    for url in HOSTS:
        try:
            print(f"{url}: HTTP {client.get(url).status_code}")
        except httpx.HTTPError as error:
            failed = True
            print(f"{url}: {type(error).__name__}: {error}", file=sys.stderr)
sys.exit(int(failed))
