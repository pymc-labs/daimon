"""Explicit full-byte skill deployment through documented GCS directory sources.

The host supplies an authorized publisher; the driver never discovers storage
credentials, makes a bucket public, or silently chooses a publisher. Its receipt
attests an immutable directory containing exactly the supplied file manifest.
"""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol, cast
from urllib.parse import urlsplit

from pydantic import JsonValue

from mux.contracts.ids import Scope
from mux.contracts.resources import SkillUpload
from mux.errors import ProviderError, ScopeViolation


def file_manifest(bundle: SkillUpload) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((file.path, sha256(file.content).hexdigest()) for file in bundle.files))


@dataclass(frozen=True)
class GCSBundleDeployment:
    tenant_id: str
    account_id: str
    account_scope_id: str
    version: str
    source: str
    files: tuple[tuple[str, str], ...]


class SkillBundlePublisher(Protocol):
    async def publish(
        self, scope: Scope, *, account_scope_id: str, version: str, bundle: SkillUpload
    ) -> GCSBundleDeployment:
        """Publish every original byte; idempotent on owner/account/version.

        Keep the versioned directory immutable and readable by the environment
        for the lifetime of its pins. Include no extra files or credentials in
        the source URI. Failure must not acknowledge a successful deployment.
        Bucket access/authorization and retention are supplied by the host.
        """
        ...


class RetainedSkillDeployments:
    """Use explicitly provisioned immutable directories; never upload or discover credentials.

    The provisioning owner must supply independently read-back receipts. This
    adapter lets the existing skill owner consume them with the same complete
    scope/version/file checks as a publisher that performed the upload itself.
    """

    def __init__(self, deployments: tuple[GCSBundleDeployment, ...]) -> None:
        self._deployments = {
            (item.tenant_id, item.account_id, item.account_scope_id, item.version): item
            for item in deployments
        }
        if len(self._deployments) != len(deployments):
            raise ValueError("duplicate skill deployment receipt")

    async def publish(
        self, scope: Scope, *, account_scope_id: str, version: str, bundle: SkillUpload
    ) -> GCSBundleDeployment:
        deployment = self._deployments.get(
            (scope.tenant_id, scope.account_id, account_scope_id, version)
        )
        if deployment is None:
            raise ScopeViolation(version, "no authorized retained skill deployment")
        validate_deployment(deployment, scope, account_scope_id, version, bundle)
        return deployment


def validate_deployment(
    deployment: GCSBundleDeployment, scope: Scope, account: str, version: str, bundle: SkillUpload
) -> None:
    if (deployment.tenant_id, deployment.account_id, deployment.account_scope_id) != (
        scope.tenant_id,
        scope.account_id,
        account,
    ):
        raise ScopeViolation(version, "skill deployment is outside the authorized scope")
    if deployment.version != version or deployment.files != file_manifest(bundle):
        raise ProviderError("conflict", retryable=False, native_code="skill_deployment_mismatch")
    uri = urlsplit(deployment.source)
    # Versioned directory, not a mutable latest alias, URL token or file mount.
    if (
        uri.scheme != "gs"
        or not uri.netloc
        or uri.username is not None
        or ":" in uri.netloc
        or uri.query
        or uri.fragment
        or not uri.path.endswith(f"/{version}/")
        or any(part in (".", "..") for part in uri.path.split("/"))
        or any(char.isspace() or char in "%\\\x00" for char in deployment.source)
    ):
        raise ValueError("skill deployment needs a credential-free versioned GCS directory")


async def verify_retained_deployments(
    deployments: tuple[GCSBundleDeployment, ...], *, token: str, retained_until: datetime
) -> None:
    """Read exact live object sets, locked retention and generation-pinned bytes.

    Only the explicitly supplied credential is used, with no redirects, ADC,
    discovery or writes. A local receipt alone is never storage evidence.
    """
    from urllib.parse import quote

    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {token}"},
        ) as http:
            for deployment in deployments:
                uri = urlsplit(deployment.source)
                endpoint = (
                    f"https://storage.googleapis.com/storage/v1/b/{quote(uri.netloc, safe='')}/o"
                )
                prefix = uri.path.lstrip("/")
                expected = {prefix + path: digest for path, digest in deployment.files}
                seen: set[str] = set()
                cursor: str | None = None
                cursors: set[str] = set()
                for _ in range(100):
                    params = {"prefix": prefix}
                    if cursor:
                        params["pageToken"] = cursor
                    response = await http.get(endpoint, params=params)
                    response.raise_for_status()
                    page = cast(JsonValue, response.json())
                    if not isinstance(page, dict):
                        raise ValueError("invalid storage listing")
                    items = page.get("items", [])
                    if not isinstance(items, list):
                        raise ValueError("invalid storage items")
                    for item in items:
                        if not isinstance(item, dict):
                            raise ValueError("invalid storage object")
                        name, generation = item.get("name"), item.get("generation")
                        retention = item.get("retention", {})
                        if (
                            not isinstance(name, str)
                            or name not in expected
                            or name in seen
                            or item.get("bucket") != uri.netloc
                            or not isinstance(generation, str)
                            or not generation.isdecimal()
                            or not isinstance(retention, dict)
                            or retention.get("mode") != "Locked"
                            or not isinstance(retention.get("retainUntilTime"), str)
                            or datetime.fromisoformat(cast(str, retention["retainUntilTime"]))
                            < retained_until
                        ):
                            raise ValueError("object set or locked retention differs")
                        seen.add(name)
                        async with http.stream(
                            "GET",
                            f"{endpoint}/{quote(name, safe='')}",
                            params={"alt": "media", "generation": generation},
                        ) as body:
                            body.raise_for_status()
                            digest = sha256()
                            count = 0
                            async for chunk in body.aiter_bytes():
                                count += len(chunk)
                                if count > 64 * 1024 * 1024:
                                    raise ValueError("storage object exceeds bundle bound")
                                digest.update(chunk)
                            if digest.hexdigest() != expected[name]:
                                raise ValueError("stored skill bytes differ")
                    following = page.get("nextPageToken")
                    if following is None:
                        break
                    if not isinstance(following, str) or not following or following in cursors:
                        raise ValueError("invalid storage cursor")
                    cursors.add(following)
                    cursor = following
                else:
                    raise ValueError("storage listing exceeds page bound")
                if seen != set(expected):
                    raise ValueError("storage object set is incomplete")
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        raise ProviderError(
            "conflict", retryable=False, native_code="skill_deployment_readback_mismatch"
        ) from None
