"""Closed session vault configuration and documented uploaded-file mounts."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict

from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.resources import ResourceBinding, WorkspaceSource
from mux.drivers.openai._common import Context
from mux.drivers.openai.transport import Object


class VaultMounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    vaults: tuple[ResourceRef, ...]


def path(value: str) -> str:
    destination = PurePosixPath(value)
    if (
        not value.startswith("/workspace/")
        or any(part in (".", "..", "") for part in value[1:].split("/"))
        or "\\" in value
        or "\x00" in value
        or str(destination) != value
    ):
        raise ValueError("invalid workspace destination")
    return value


def file(scope: Scope, ref: ResourceRef, destination: str, context: Context) -> Object:
    context.check(scope, ref, "artifact")
    if not ref.id.startswith("file:") or not ref.id[5:]:
        raise context.unsupported("published_artifact_mount")
    return {"type": "file_id", "file_id": ref.id[5:], "path": path(destination)}


def workspace_sources(
    scope: Scope, sources: Sequence[WorkspaceSource], context: Context
) -> list[Object]:
    result: list[Object] = []
    destinations: set[str] = set()
    for source in sources:
        if (
            source.kind != "file"
            or source.artifact is None
            or source.repository_url is not None
            or source.ref is not None
            or source.credential_ref is not None
        ):
            raise context.unsupported("workspace_source")
        value = file(scope, source.artifact, source.target_path, context)
        if source.target_path in destinations:
            raise ValueError("duplicate workspace destination")
        destinations.add(source.target_path)
        result.append(value)
    return result


def resources(scope: Scope, bindings: Sequence[ResourceBinding], context: Context) -> list[Object]:
    result: list[Object] = []
    destinations: set[str] = set()
    ids: set[str] = set()
    for binding in bindings:
        if (
            binding.kind != "artifact"
            or binding.resource is None
            or binding.target_path is None
            or binding.credential_ref is not None
            or binding.native is not None
        ):
            raise context.unsupported("session_resource")
        value = file(scope, binding.resource, binding.target_path, context)
        if binding.id in ids or binding.target_path in destinations:
            raise ValueError("duplicate session resource")
        ids.add(binding.id)
        destinations.add(binding.target_path)
        result.append(value)
    return result


def vault_ids(scope: Scope, extension: ExtensionConfig, context: Context) -> list[str]:
    if extension.namespace != "openai.vaults" or extension.version != 1:
        raise context.unsupported("vault_configuration_version")
    config = VaultMounts.model_validate(dict(extension.value))
    result: list[str] = []
    for vault in config.vaults:
        context.check(scope, vault, "vault")
        if vault.id in result:
            raise ValueError("duplicate vault attachment")
        result.append(vault.id)
    return result
