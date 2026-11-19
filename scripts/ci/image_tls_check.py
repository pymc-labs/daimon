"""Reach each provider's API over TLS with the image's own certificate roots, sending no key.

A stale root store fails the handshake; a healthy one gets an HTTP status (401 or 403).
A handshake is tried a few times, so one network blip at a provider does not fail a PR;
a stale store fails every try.
Run inside the built image: `docker run --rm -i --entrypoint python IMAGE - < this file`.
"""

import sys
import time

import httpx
from mux.drivers import isolated_ssl_context

HOSTS = (
    "https://api.openai.com/v1/models",
    "https://api.anthropic.com/v1/models",
    "https://generativelanguage.googleapis.com/v1beta/models",
)

TRIES = 4

failed = False
with httpx.Client(verify=isolated_ssl_context(), trust_env=False, timeout=20) as client:
    for url in HOSTS:
        for attempt in range(1, TRIES + 1):
            try:
                print(f"{url}: HTTP {client.get(url).status_code}")
                break
            except httpx.HTTPError as error:
                print(f"{url}: {type(error).__name__}: {error}", file=sys.stderr)
                if attempt == TRIES:
                    failed = True
                else:
                    time.sleep(2 * attempt)
sys.exit(int(failed))
