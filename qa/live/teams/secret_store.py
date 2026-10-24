"""Secret Manager cache IO. Concurrent writers use last-write-wins semantics."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, cast

from google.api_core.exceptions import FailedPrecondition, NotFound
from google.cloud import secretmanager


class VersionInfo(Protocol):
    name: str
    state: int


class SecretPayload(Protocol):
    data: bytes


class SecretResponse(Protocol):
    payload: SecretPayload


class SecretClient(Protocol):
    def access_secret_version(self, *, request: dict[str, str]) -> SecretResponse: ...

    def add_secret_version(self, *, request: dict[str, object]) -> VersionInfo: ...

    def list_secret_versions(self, *, request: dict[str, str]) -> Iterable[VersionInfo]: ...

    def destroy_secret_version(self, *, request: dict[str, str]) -> object: ...


def client() -> SecretClient:
    """Use application default credentials, including workload identity."""
    return cast(SecretClient, secretmanager.SecretManagerServiceClient())


def read_secret(name: str, api: SecretClient | None = None) -> str:
    """Read a required UTF-8 config secret without putting payloads in errors."""
    service = api or client()
    try:
        response = service.access_secret_version(request={"name": f"{name}/versions/latest"})
        return response.payload.data.decode("utf-8")
    except Exception:
        raise RuntimeError("Teams QA config secret unavailable") from None


class SecretManagerCacheBackend:
    def __init__(self, project: str, prefix: str, api: SecretClient | None = None) -> None:
        self.project = project
        self.prefix = prefix
        self.api = api or client()

    def name(self, persona: str) -> str:
        return f"projects/{self.project}/secrets/{self.prefix}-token-{persona}"

    def read(self, persona: str) -> str | None:
        try:
            response = self.api.access_secret_version(
                request={"name": f"{self.name(persona)}/versions/latest"}
            )
            return response.payload.data.decode("utf-8")
        except (NotFound, FailedPrecondition):
            return None
        except Exception:
            raise RuntimeError("Teams QA token cache read failed") from None

    def write(self, persona: str, serialized: str) -> None:
        name = self.name(persona)
        try:
            added = self.api.add_secret_version(
                request={"parent": name, "payload": {"data": serialized.encode("utf-8")}}
            )
        except Exception:
            raise RuntimeError("Teams QA token cache write failed") from None
        try:
            # Destroy only this persona's enabled versions beyond the newest
            # three; never another secret's, and never a disabled version.
            # The API names versions by project number, so the version just
            # added gives this secret's canonical name in this project.
            canonical = added.name.rsplit("/versions/", 1)[0]
            parts = canonical.split("/")
            if (
                len(parts) != 4
                or parts[0] != "projects"
                or not (parts[1] == self.project or parts[1].isdigit())
                or parts[2:] != ["secrets", name.split("/")[-1]]
            ):
                return
            own = (f"{name}/versions/", f"{canonical}/versions/")
            versions = self.api.list_secret_versions(request={"parent": name})
            enabled = sorted(
                (
                    version
                    for version in versions
                    if version.name.startswith(own)
                    and version.state == secretmanager.SecretVersion.State.ENABLED
                ),
                key=lambda version: int(version.name.rsplit("/", 1)[-1]),
                reverse=True,
            )
            for version in enabled[3:]:
                self.api.destroy_secret_version(request={"name": version.name})
        except Exception:
            # Pruning is best-effort: teammates may only add versions, and the
            # new version is already stored.
            pass

    def delete(self, persona: str) -> None:
        raise ValueError("logout is unavailable for shared Secret Manager caches")
