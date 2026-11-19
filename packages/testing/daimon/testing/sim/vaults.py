"""Anthropic vaults, simulated: what MCP agent chat stores its per-account credential in."""

from __future__ import annotations

import itertools
import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from anthropic.types.beta.vault_create_params import VaultCreateParams
from anthropic.types.beta.vaults.credential_create_params import CredentialCreateParams
from daimon.testing.sim.network import StrictFake
from mux.testing.drivers.anthropic.ma import list_response

_SECRETS = frozenset({"token", "access_token", "secret_value"})
type Json = dict[str, Any]


class Vaults:
    """Vaults and their credentials; a response never carries a secret, as the API's."""

    def __init__(self) -> None:
        self.vaults: dict[str, Json] = {}
        self.credentials: dict[str, Json] = {}
        self._ids = itertools.count(1)

    def install(self, fake: StrictFake) -> None:
        """Serve /v1/vaults on `fake`."""
        one, creds = "/v1/vaults/([^/]+)", "/v1/vaults/([^/]+)/credentials"
        fake.shape("POST", "/v1/vaults", VaultCreateParams)
        fake.shape("POST", creds, CredentialCreateParams)
        fake.route("GET", "/v1/vaults", self._list)
        fake.route("POST", "/v1/vaults", self._create)
        fake.route("GET", one, lambda r, m: httpx.Response(200, json=self.vaults[m[1]]))
        fake.route("POST", one + "/archive", self._archive)
        fake.route("GET", creds, self._credentials)
        fake.route("POST", creds, self._add)
        fake.route("POST", creds + "/([^/]+)", self._update)
        fake.route("DELETE", creds + "/([^/]+)", self._delete)

    def _stamp(self, kind: str) -> Json:
        now = datetime.now(UTC).isoformat()
        return {"id": f"{kind}_{next(self._ids):04d}", "created_at": now, "updated_at": now}

    def _list(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        archived = request.url.params.get("include_archived") == "true"
        return list_response([v for v in self.vaults.values() if archived or not v["archived_at"]])

    def _create(self, request: httpx.Request, _: re.Match[str]) -> httpx.Response:
        body = json.loads(request.content)
        vault = self._stamp("vlt") | {"type": "vault", "archived_at": None, "metadata": {}} | body
        self.vaults[vault["id"]] = vault
        return httpx.Response(200, json=vault)

    def _archive(self, _: httpx.Request, match: re.Match[str]) -> httpx.Response:
        self.vaults[match[1]]["archived_at"] = datetime.now(UTC).isoformat()
        return httpx.Response(200, json=self.vaults[match[1]])

    def _credentials(self, _: httpx.Request, match: re.Match[str]) -> httpx.Response:
        rows = [c for c in self.credentials.values() if c["vault_id"] == match[1]]
        return list_response([self._public(c) for c in rows])

    def _add(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        stored = self._stamp("vcrd") | {"type": "vault_credential", "vault_id": match[1]}
        stored |= {"archived_at": None, "metadata": {}} | json.loads(request.content)
        self.credentials[stored["id"]] = stored
        return httpx.Response(200, json=self._public(stored))

    def _update(self, request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        stored, body = self.credentials[match[2]], json.loads(request.content)
        stored |= {k: v for k, v in body.items() if k != "auth"}
        stored["auth"] = stored["auth"] | body.get("auth", {})
        return httpx.Response(200, json=self._public(stored))

    def _delete(self, _: httpx.Request, match: re.Match[str]) -> httpx.Response:
        self.credentials.pop(match[2], None)
        return httpx.Response(200, json={"id": match[2], "type": "vault_credential_deleted"})

    @staticmethod
    def _public(credential: Json) -> Json:
        auth = {k: v for k, v in credential["auth"].items() if k not in _SECRETS}
        return credential | {"auth": auth}
