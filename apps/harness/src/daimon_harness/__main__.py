"""Run HTTPS listeners, loading TLS keys before the service UID drop."""

import asyncio
import os
import ssl
from collections.abc import Callable
from typing import Any, cast

import uvicorn
from fastapi import FastAPI


def main(factory: Callable[[], FastAPI] | None = None):
    role = os.environ.get("HARNESS_ROLE", "harness")
    cert = os.environ.get("HARNESS_TLS_CERT")
    key = os.environ.get("HARNESS_TLS_KEY")
    client_ca = None
    verify_client = ssl.CERT_NONE
    if role == "sandbox":
        from .pod_tls import prepare

        cert, key = prepare()
        client_ca = os.environ["HARNESS_SANDBOX_CA"]
        verify_client = ssl.CERT_REQUIRED
    if role in {"sandbox", "controller"} and not (cert and key):
        raise RuntimeError("pod controller and sandboxes require TLS")
    app = (
        "daimon_harness.controller:create_app"
        if role == "controller"
        else "daimon_harness.service:create_app"
    )
    config = uvicorn.Config(
        factory or app,
        factory=True,
        loop="asyncio",
        host="0.0.0.0",
        port=8088,
        proxy_headers=False,
        timeout_graceful_shutdown=10,
        ssl_certfile=cert,
        ssl_keyfile=key,
        ssl_ca_certs=client_ca,
        ssl_cert_reqs=verify_client,
    )
    config.load()
    server = uvicorn.Server(config)
    if role != "controller":
        server.run()
        return
    # Same app/state, one lifespan lease; sandbox ingress cannot access C3.
    internal = uvicorn.Config(
        cast(Any, config).loaded_app,
        host="0.0.0.0",
        port=8089,
        proxy_headers=False,
        lifespan="off",
        ssl_certfile=cert,
        ssl_keyfile=key,
    )

    async def serve():
        await asyncio.gather(server.serve(), uvicorn.Server(internal).serve())

    asyncio.run(serve())


if __name__ == "__main__":
    main()
