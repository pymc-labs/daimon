"""One captured, header-protected identity provider shared by model and storage."""

from __future__ import annotations

import asyncio
import os
import threading
from types import TracebackType

from azure.core.credentials import AccessToken
from azure.identity.aio import ManagedIdentityCredential

from .credentials import credential_log_redaction, process_credentials, remember_log_secret
from .isolation import validate_credential_sources


class CapturedIdentity:
    def __init__(self) -> None:
        validate_credential_sources()
        self.endpoint = os.environ.get("IDENTITY_ENDPOINT", "")
        self.header = os.environ.get("IDENTITY_HEADER", "")
        self.client_id = os.environ.get("AZURE_CLIENT_ID")
        if not self.endpoint or not self.header:
            raise RuntimeError("managed identity requires a header-protected IDENTITY_ENDPOINT")
        # Unlike the synchronous MSAL credential, the async implementation captures
        # the endpoint and header immediately, before protect_process scrubs them.
        self.credential = ManagedIdentityCredential(client_id=self.client_id)
        self.runner = asyncio.Runner()
        self.lock = threading.Lock()
        self.closed = False
        self.redaction = credential_log_redaction(process_credentials())
        self.log_secrets = self.redaction.__enter__()

    def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
        with self.lock:
            if self.closed:
                raise RuntimeError("managed identity provider is closed")
            token = self.runner.run(self.credential.get_token(*scopes))
            remember_log_secret(self.log_secrets, "identity_bearer", token.token)
            return token

    def close(self) -> None:
        with self.lock:
            if not self.closed:
                self.runner.run(self.credential.close())
                self.runner.close()
                self.redaction.__exit__(None, None, None)
                self.closed = True


class AsyncIdentity:
    def __init__(self, provider: CapturedIdentity) -> None:
        self.provider = provider

    async def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
        return await asyncio.to_thread(self.provider.get_token, *scopes, **kwargs)

    async def __aenter__(self) -> AsyncIdentity:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        await asyncio.to_thread(self.provider.close)
