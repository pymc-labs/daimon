"""Tests for daimon.core.mcp_credential_sweep.

Validates:
  (a) pure partition selects only daimon-mcp:* vaults.
  (b) shell apply path: delete+recreate the public_url static_bearer without
      is_admin; Copilot credential untouched.
  (c) dry_run=True (default): no delete/create hits the transport; report
      lists planned target.
  (d) unparseable suffix vaults skipped+recorded.

Per guideline:testing: transport-level validated construction only (no
AsyncMock on client.beta.*, no model_construct). EMPTY_CLOUD_CONFIG constant
reused from daimon.testing.ma; everything else inlined inline at the call
site.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import httpx
import jwt as pyjwt
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsVault
from daimon.core.mcp_credential_sweep import (
    partition_daimon_mcp_vault_ids,
    sweep_stale_admin_credentials,
)

# asyncio_mode = "auto" in pyproject.toml — no pytestmark needed; async tests run automatically.

_PUBLIC_URL = "https://mcp.example.com/mcp"
_COPILOT_URL = "https://api.githubcopilot.com/mcp"
_SECRET = b"s" * 32
_NOW = dt.datetime(2026, 6, 29, tzinfo=dt.UTC)


def _vault(vault_id: str, display_name: str) -> BetaManagedAgentsVault:
    """Inline BetaManagedAgentsVault construction — SDK field drift breaks this test."""
    now = dt.datetime(2026, 5, 1, tzinfo=dt.UTC)
    return BetaManagedAgentsVault(
        id=vault_id,
        type="vault",
        display_name=display_name,
        metadata={},
        archived_at=None,
        created_at=now,
        updated_at=now,
    )


def _make_client(handler: httpx.MockTransport) -> AsyncAnthropic:
    return AsyncAnthropic(
        api_key="sk-test",
        http_client=httpx.AsyncClient(transport=handler),
    )


def _vault_wire(vault_id: str, display_name: str) -> dict[str, Any]:
    """Serialized vault for use in transport handler responses."""
    return _vault(vault_id, display_name).model_dump(mode="json")


def _cred_wire(
    *,
    cred_id: str,
    vault_id: str,
    mcp_server_url: str,
) -> dict[str, Any]:
    """Minimal static_bearer credential wire shape."""
    return {
        "id": cred_id,
        "type": "vault_credential",
        "vault_id": vault_id,
        "metadata": {},
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
        "auth": {
            "type": "static_bearer",
            "mcp_server_url": mcp_server_url,
        },
    }


# ---------------------------------------------------------------------------
# (a) Pure partition
# ---------------------------------------------------------------------------


def test_partition_returns_only_daimon_mcp_vaults() -> None:
    """Only vaults prefixed with 'daimon-mcp:' are returned; others are ignored."""
    account_a = uuid.uuid4()
    account_b = uuid.uuid4()
    vaults = [
        _vault("vlt_a", f"daimon-mcp:{account_a}:{uuid.uuid4()}"),
        _vault("vlt_b", f"daimon-mcp:{account_b}"),
        _vault("vlt_other", "anthropic-builtin"),
        _vault("vlt_unrelated", "user-custom"),
    ]
    result = partition_daimon_mcp_vault_ids(vaults)
    assert sorted(result) == ["vlt_a", "vlt_b"], (
        "only daimon-mcp:* vaults must be returned; non-daimon vaults ignored"
    )


def test_partition_returns_empty_when_no_daimon_mcp_vaults() -> None:
    """Empty result when no vaults match the daimon-mcp: prefix."""
    vaults = [
        _vault("vlt_1", "something-else"),
        _vault("vlt_2", "anthropic-copilot"),
    ]
    result = partition_daimon_mcp_vault_ids(vaults)
    assert result == [], "no daimon-mcp vaults must yield empty list"


def test_partition_handles_empty_vault_list() -> None:
    """Empty vault list returns empty result."""
    assert partition_daimon_mcp_vault_ids([]) == [], "empty input must return empty list"


# ---------------------------------------------------------------------------
# (b) Shell apply: delete+recreate without is_admin; Copilot untouched
# ---------------------------------------------------------------------------


async def test_sweep_apply_deletes_and_recreates_public_url_cred_without_is_admin() -> None:
    """dry_run=False: the static_bearer at public_url is deleted then recreated;
    the recreated token decodes WITHOUT is_admin; the Copilot cred is untouched."""
    account_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{uuid.uuid4()}"

    deleted_cred_ids: list[str] = []
    created_bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "data": [_vault_wire("vlt_1", display)],
                    "has_more": False,
                },
            )
        if req.method == "GET" and req.url.path == "/v1/vaults/vlt_1/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        _cred_wire(
                            cred_id="vcrd_stale",
                            vault_id="vlt_1",
                            mcp_server_url=_PUBLIC_URL,
                        ),
                        _cred_wire(
                            cred_id="vcrd_copilot",
                            vault_id="vlt_1",
                            mcp_server_url=_COPILOT_URL,
                        ),
                    ],
                    "has_more": False,
                },
            )
        if req.method == "DELETE" and req.url.path == "/v1/vaults/vlt_1/credentials/vcrd_stale":
            deleted_cred_ids.append("vcrd_stale")
            return httpx.Response(200, json={"id": "vcrd_stale", "deleted": True})
        if req.method == "POST" and req.url.path == "/v1/vaults/vlt_1/credentials":
            body: dict[str, Any] = json.loads(req.content)
            created_bodies.append(body)
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_fresh",
                    "type": "vault_credential",
                    "vault_id": "vlt_1",
                    "metadata": {},
                    "created_at": "2026-06-29T00:00:00Z",
                    "updated_at": "2026-06-29T00:00:00Z",
                    "auth": {
                        "type": "static_bearer",
                        "mcp_server_url": _PUBLIC_URL,
                    },
                },
            )
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    client = _make_client(httpx.MockTransport(handler))
    report = await sweep_stale_admin_credentials(
        client,
        jwt_secret=_SECRET,
        public_url=_PUBLIC_URL,
        now=_NOW,
        dry_run=False,
    )

    # The stale cred was deleted.
    assert deleted_cred_ids == ["vcrd_stale"], "must delete the public_url static_bearer cred"
    # Copilot cred never targeted.
    copilot_deleted = any("copilot" in cid for cid in deleted_cred_ids)
    assert not copilot_deleted, "Copilot credential must NEVER be deleted"

    # A fresh credential was created.
    assert len(created_bodies) == 1, "must POST exactly one new credential"
    token: str = created_bodies[0]["auth"]["token"]
    claims: dict[str, Any] = pyjwt.decode(token, _SECRET, algorithms=["HS256"])
    assert "is_admin" not in claims, (
        "recreated credential must NOT carry is_admin claim (defense-in-depth invariant)"
    )
    assert "internal" not in claims, (
        "recreated credential must NOT carry internal claim — only mint_internal_mcp_token does"
    )
    assert claims["sub"] == str(account_id), (
        "recreated token sub must be the parsed account_id from the vault display_name"
    )

    # Report reflects what happened.
    assert len(report.recreated_cred_ids) == 1, "report must record the new cred id"
    assert len(report.swept_pairs) == 1, "one (vault_id, old_cred_id) pair"
    assert report.swept_pairs[0] == ("vlt_1", "vcrd_stale"), (
        "swept_pairs must record (vault_id, old_cred_id)"
    )
    assert report.unparseable_vault_ids == [], "no unparseable vaults in this scenario"


async def test_sweep_apply_copilot_cred_never_matched() -> None:
    """Copilot URL cred in a daimon-mcp vault is never deleted or recreated."""
    account_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{uuid.uuid4()}"

    deleted_cred_ids: list[str] = []
    created_bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={"data": [_vault_wire("vlt_1", display)], "has_more": False},
            )
        if req.method == "GET" and req.url.path == "/v1/vaults/vlt_1/credentials":
            # Only a Copilot cred — no public_url cred.
            return httpx.Response(
                200,
                json={
                    "data": [
                        _cred_wire(
                            cred_id="vcrd_copilot",
                            vault_id="vlt_1",
                            mcp_server_url=_COPILOT_URL,
                        ),
                    ],
                    "has_more": False,
                },
            )
        if req.method == "DELETE":
            cred_id = req.url.path.rsplit("/", 1)[-1]
            deleted_cred_ids.append(cred_id)
            return httpx.Response(200, json={"id": cred_id, "deleted": True})
        if req.method == "POST" and "/credentials" in req.url.path:
            body: dict[str, Any] = json.loads(req.content)
            created_bodies.append(body)
            return httpx.Response(200, json={"id": "vcrd_x"})
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    client = _make_client(httpx.MockTransport(handler))
    report = await sweep_stale_admin_credentials(
        client,
        jwt_secret=_SECRET,
        public_url=_PUBLIC_URL,
        now=_NOW,
        dry_run=False,
    )

    assert deleted_cred_ids == [], "Copilot-only vault must produce zero deletes"
    assert created_bodies == [], "Copilot-only vault must produce zero creates"
    assert report.swept_pairs == [], "no targets when only Copilot cred present"
    assert report.recreated_cred_ids == [], "no recreated creds when vault has no public_url cred"


# ---------------------------------------------------------------------------
# (c) dry_run=True: no writes; report lists planned target
# ---------------------------------------------------------------------------


async def test_sweep_dry_run_no_writes_but_report_lists_target() -> None:
    """dry_run=True (default): report lists the planned target but NO delete/create
    hits the transport."""
    account_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{uuid.uuid4()}"

    mutating_calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={"data": [_vault_wire("vlt_1", display)], "has_more": False},
            )
        if req.method == "GET" and req.url.path == "/v1/vaults/vlt_1/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        _cred_wire(
                            cred_id="vcrd_stale",
                            vault_id="vlt_1",
                            mcp_server_url=_PUBLIC_URL,
                        ),
                    ],
                    "has_more": False,
                },
            )
        # Record any mutating call — should never happen in dry_run.
        mutating_calls.append(f"{req.method} {req.url.path}")
        return httpx.Response(200, json={})

    client = _make_client(httpx.MockTransport(handler))
    # dry_run is the default, but be explicit for clarity.
    report = await sweep_stale_admin_credentials(
        client,
        jwt_secret=_SECRET,
        public_url=_PUBLIC_URL,
        now=_NOW,
        dry_run=True,
    )

    assert mutating_calls == [], f"dry_run must not issue any DELETE or POST; got: {mutating_calls}"
    assert report.swept_pairs == [("vlt_1", "vcrd_stale")], (
        "dry_run report must list the planned (vault_id, cred_id) target"
    )
    assert report.recreated_cred_ids == [], (
        "dry_run must not populate recreated_cred_ids — no writes occurred"
    )


async def test_sweep_dry_run_is_the_default() -> None:
    """Calling sweep_stale_admin_credentials without dry_run= defaults to dry_run=True."""
    account_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{uuid.uuid4()}"

    mutating_calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={"data": [_vault_wire("vlt_1", display)], "has_more": False},
            )
        if req.method == "GET" and req.url.path == "/v1/vaults/vlt_1/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        _cred_wire(
                            cred_id="vcrd_stale",
                            vault_id="vlt_1",
                            mcp_server_url=_PUBLIC_URL,
                        ),
                    ],
                    "has_more": False,
                },
            )
        mutating_calls.append(f"{req.method} {req.url.path}")
        return httpx.Response(200, json={})

    client = _make_client(httpx.MockTransport(handler))
    # Omit dry_run — should default to True.
    report = await sweep_stale_admin_credentials(
        client,
        jwt_secret=_SECRET,
        public_url=_PUBLIC_URL,
        now=_NOW,
    )

    assert mutating_calls == [], "default (dry_run=True) must not issue any writes"
    assert report.swept_pairs == [("vlt_1", "vcrd_stale")], (
        "default dry_run must still report the planned target"
    )


# ---------------------------------------------------------------------------
# (d) Unparseable vault suffix skipped+recorded
# ---------------------------------------------------------------------------


async def test_sweep_skips_unparseable_suffix_vault() -> None:
    """A daimon-mcp vault with a non-UUID first suffix segment is skipped and
    recorded in unparseable_vault_ids — never deleted or recreated."""
    bad_display = "daimon-mcp:not-a-uuid:whatever"

    mutating_calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={"data": [_vault_wire("vlt_bad", bad_display)], "has_more": False},
            )
        # Any credential listing or mutating calls should never happen.
        mutating_calls.append(f"{req.method} {req.url.path}")
        return httpx.Response(200, json={})

    client = _make_client(httpx.MockTransport(handler))
    report = await sweep_stale_admin_credentials(
        client,
        jwt_secret=_SECRET,
        public_url=_PUBLIC_URL,
        now=_NOW,
        dry_run=False,
    )

    assert report.unparseable_vault_ids == ["vlt_bad"], (
        "unparseable suffix vault must be recorded in unparseable_vault_ids"
    )
    assert report.swept_pairs == [], "unparseable vault must not produce any swept pair"
    assert report.recreated_cred_ids == [], "unparseable vault must not produce any recreated cred"
    # Must not touch credentials endpoint for unparseable vault.
    cred_calls = [c for c in mutating_calls if "credentials" in c]
    assert cred_calls == [], (
        f"credentials endpoint must NOT be called for an unparseable vault; got: {cred_calls}"
    )


async def test_sweep_mixed_vault_list_applies_to_correct_vaults() -> None:
    """Mixed vault list: daimon-mcp, non-daimon, and unparseable.
    Only the valid daimon-mcp vault's public_url cred is swept."""
    valid_account_id = uuid.uuid4()
    valid_display = f"daimon-mcp:{valid_account_id}:{uuid.uuid4()}"

    deleted_cred_ids: list[str] = []
    created_bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "data": [
                        _vault_wire("vlt_valid", valid_display),
                        _vault_wire("vlt_unrelated", "some-other-vault"),
                        _vault_wire("vlt_bad", "daimon-mcp:not-a-uuid"),
                    ],
                    "has_more": False,
                },
            )
        if req.method == "GET" and req.url.path == "/v1/vaults/vlt_valid/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        _cred_wire(
                            cred_id="vcrd_stale",
                            vault_id="vlt_valid",
                            mcp_server_url=_PUBLIC_URL,
                        ),
                    ],
                    "has_more": False,
                },
            )
        if req.method == "DELETE" and "vlt_valid" in req.url.path:
            cred_id = req.url.path.rsplit("/", 1)[-1]
            deleted_cred_ids.append(cred_id)
            return httpx.Response(200, json={"id": cred_id, "deleted": True})
        if req.method == "POST" and req.url.path == "/v1/vaults/vlt_valid/credentials":
            body: dict[str, Any] = json.loads(req.content)
            created_bodies.append(body)
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_fresh",
                    "type": "vault_credential",
                    "vault_id": "vlt_valid",
                    "metadata": {},
                    "created_at": "2026-06-29T00:00:00Z",
                    "updated_at": "2026-06-29T00:00:00Z",
                    "auth": {"type": "static_bearer", "mcp_server_url": _PUBLIC_URL},
                },
            )
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    client = _make_client(httpx.MockTransport(handler))
    report = await sweep_stale_admin_credentials(
        client,
        jwt_secret=_SECRET,
        public_url=_PUBLIC_URL,
        now=_NOW,
        dry_run=False,
    )

    assert deleted_cred_ids == ["vcrd_stale"], "only the valid vault's public_url cred deleted"
    assert len(created_bodies) == 1, "exactly one new credential POSTed"
    assert report.unparseable_vault_ids == ["vlt_bad"], "bad vault recorded as unparseable"
    assert len(report.swept_pairs) == 1
