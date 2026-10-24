"""Offline contracts for shared Teams QA token caches and refresh."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.api_core.exceptions import NotFound, PermissionDenied
from google.cloud import secretmanager

from qa.live.teams import __main__ as cli
from qa.live.teams.config import TeamsQaConfig
from qa.live.teams.secret_store import SecretManagerCacheBackend
from qa.live.teams.tokens import ReauthRequired, TokenStore

SENTINEL = "PRIVATE_CACHE_PAYLOAD"


def canonical(name: str) -> str:
    """The real API names versions by project number, not project ID."""
    return name.replace("projects/qa-project/", "projects/123456789/")


class FakeSecrets:
    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}
        self.versions: list[SimpleNamespace] = []
        self.destroyed: list[str] = []
        self.deny_prune = False
        self.deny_destroy = False
        self.listing: list[SimpleNamespace] | None = None

    def access_secret_version(self, *, request: dict[str, str]) -> SimpleNamespace:
        name = request["name"].removesuffix("/versions/latest")
        if name not in self.payloads:
            raise NotFound("missing")
        return SimpleNamespace(payload=SimpleNamespace(data=self.payloads[name]))

    def add_secret_version(self, *, request: dict[str, Any]) -> SimpleNamespace:
        name = request["parent"]
        self.payloads[name] = request["payload"]["data"]
        version = SimpleNamespace(
            name=f"{canonical(name)}/versions/{len(self.versions) + 1}",
            state=secretmanager.SecretVersion.State.ENABLED,
        )
        self.versions.append(version)
        return version

    def list_secret_versions(self, *, request: dict[str, str]) -> list[SimpleNamespace]:
        if self.deny_prune:
            raise PermissionDenied("list denied")
        if self.listing is not None:
            return self.listing
        parent = canonical(request["parent"])
        return [v for v in self.versions if v.name.startswith(parent + "/")]

    def destroy_secret_version(self, *, request: dict[str, str]) -> None:
        if self.deny_prune or self.deny_destroy:
            raise PermissionDenied("destroy denied")
        self.destroyed.append(request["name"])
        for version in self.versions:
            if version.name == request["name"]:
                version.state = secretmanager.SecretVersion.State.DESTROYED


def config_source() -> dict[str, Any]:
    source = json.loads((Path(__file__).parents[1] / "teams/config.example.json").read_text())
    source["token_store"] = {
        "kind": "secret_manager",
        "project": "qa-project",
        "secret_prefix": "teams-qa",
    }
    return source


def test_cache_versions_and_prune() -> None:
    api = FakeSecrets()
    backend = SecretManagerCacheBackend("qa-project", "teams-qa", api)
    assert backend.read("admin") is None
    for _ in range(5):
        backend.write("admin", SENTINEL)
    assert backend.read("admin") == SENTINEL
    assert api.destroyed == [version.name for version in api.versions[:2]]
    api.deny_prune = True
    backend.write("admin", SENTINEL)
    assert len(api.versions) == 6
    api.deny_prune = False
    api.deny_destroy = True
    backend.write("admin", SENTINEL)
    assert len(api.versions) == 7
    with pytest.raises(ValueError, match="shared Secret Manager"):
        backend.delete("admin")


ENABLED = secretmanager.SecretVersion.State.ENABLED
DISABLED = secretmanager.SecretVersion.State.DISABLED
CONFIG = "projects/qa-project/secrets/teams-qa-config"
ADMIN = "projects/qa-project/secrets/teams-qa-token-admin"
MEMBER = "projects/qa-project/secrets/teams-qa-token-member"


def enabled(api: FakeSecrets, secret: str) -> list[str]:
    prefix = canonical(secret) + "/"
    return [v.name for v in api.versions if v.name.startswith(prefix) and v.state == ENABLED]


@pytest.mark.parametrize("writes", [1, 2, 3, 4, 5, 8])
def test_prune_boundaries_keep_newest_three(writes: int) -> None:
    api = FakeSecrets()
    backend = SecretManagerCacheBackend("qa-project", "teams-qa", api)
    for _ in range(writes):
        backend.write("admin", SENTINEL)
    kept = enabled(api, ADMIN)
    assert len(kept) == min(writes, 3)
    assert kept == [v.name for v in api.versions[-len(kept) :]]
    assert len(api.destroyed) == max(0, writes - 3)


def test_prune_never_touches_config_or_other_persona() -> None:
    api = FakeSecrets()
    api.add_secret_version(request={"parent": CONFIG, "payload": {"data": b"{}"}})
    backend = SecretManagerCacheBackend("qa-project", "teams-qa", api)
    for _ in range(2):
        backend.write("member", SENTINEL)
    for _ in range(6):
        backend.write("admin", SENTINEL)
    # Even a listing that returns every secret's versions, including the same
    # secret id in another project, must not reach them.
    foreign = [
        SimpleNamespace(
            name=f"projects/{project}/secrets/teams-qa-token-admin/versions/1", state=ENABLED
        )
        for project in ("987654321", "other-project")
    ]
    api.listing = foreign + list(api.versions)
    backend.write("admin", SENTINEL)
    assert enabled(api, CONFIG) == [f"{canonical(CONFIG)}/versions/1"]
    assert len(enabled(api, MEMBER)) == 2
    # The listing predates the last add, so that version survives as a fourth.
    assert len(enabled(api, ADMIN)) == 4
    assert all(name.startswith(canonical(ADMIN) + "/") for name in api.destroyed)
    assert all(version.state == ENABLED for version in foreign)


def test_prune_matches_project_number_names() -> None:
    api = FakeSecrets()
    backend = SecretManagerCacheBackend("qa-project", "teams-qa", api)
    for _ in range(4):
        backend.write("admin", SENTINEL)
    assert api.versions[0].name == "projects/123456789/secrets/teams-qa-token-admin/versions/1"
    assert api.destroyed == [api.versions[0].name]
    assert len(enabled(api, ADMIN)) == 3


def test_prune_skips_when_added_version_is_in_another_project() -> None:
    api = FakeSecrets()
    backend = SecretManagerCacheBackend("qa-project", "teams-qa", api)
    for _ in range(4):
        backend.write("admin", SENTINEL)
    destroyed = list(api.destroyed)
    real_add = api.add_secret_version

    def add_elsewhere(*, request: dict[str, Any]) -> SimpleNamespace:
        real_add(request=request)
        return SimpleNamespace(
            name="projects/other-project/secrets/teams-qa-token-admin/versions/9", state=ENABLED
        )

    api.add_secret_version = add_elsewhere  # type: ignore[method-assign]
    backend.write("admin", SENTINEL)
    assert api.destroyed == destroyed


def test_prune_skips_when_added_version_is_another_secret() -> None:
    api = FakeSecrets()
    backend = SecretManagerCacheBackend("qa-project", "teams-qa", api)
    for _ in range(4):
        backend.write("admin", SENTINEL)
    destroyed = list(api.destroyed)
    real_add = api.add_secret_version

    def add_elsewhere(*, request: dict[str, Any]) -> SimpleNamespace:
        real_add(request=request)
        return SimpleNamespace(name="projects/1/secrets/teams-qa-config/versions/9", state=ENABLED)

    api.add_secret_version = add_elsewhere  # type: ignore[method-assign]
    backend.write("admin", SENTINEL)
    assert api.destroyed == destroyed


def test_prune_ignores_disabled_versions() -> None:
    api = FakeSecrets()
    backend = SecretManagerCacheBackend("qa-project", "teams-qa", api)
    for _ in range(4):
        backend.write("admin", SENTINEL)
    api.versions[-1].state = DISABLED
    backend.write("admin", SENTINEL)
    assert api.versions[3].state == DISABLED
    assert len(enabled(api, ADMIN)) == 3


def test_concurrent_stale_prunes_keep_newest_three() -> None:
    api = FakeSecrets()
    backend = SecretManagerCacheBackend("qa-project", "teams-qa", api)
    snapshots: list[list[SimpleNamespace]] = []
    for _ in range(7):
        api.add_secret_version(request={"parent": ADMIN, "payload": {"data": b"x"}})
        snapshots.append([SimpleNamespace(name=v.name, state=v.state) for v in api.versions])
    newest = [v.name for v in api.versions[-3:]]
    # Writers that listed at different moments prune in any order.
    for snapshot in reversed(snapshots):
        api.listing = snapshot
        backend.write("admin", SENTINEL)
    assert set(newest) <= set(enabled(api, ADMIN))
    assert len(enabled(api, ADMIN)) >= 3


def test_config_secret_that_is_a_token_secret_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeSecrets()
    api.payloads[ADMIN] = json.dumps(config_source()).encode()
    monkeypatch.setattr("qa.live.teams.secret_store.client", lambda: api)
    monkeypatch.delenv("DAIMON_TEAMS_QA_CONFIG", raising=False)
    with pytest.raises(ValueError, match="must not be a token secret"):
        TeamsQaConfig.load(config_secret=ADMIN)


def test_config_secret_and_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeSecrets()
    config_name = "projects/qa-project/secrets/teams-qa-config"
    api.payloads[config_name] = json.dumps(config_source()).encode()
    monkeypatch.setattr("qa.live.teams.secret_store.client", lambda: api)
    monkeypatch.setenv("DAIMON_TEAMS_QA_CONFIG", "/nonexistent/teams-qa.json")
    config = TeamsQaConfig.load(config_secret=config_name)
    assert config.token_dir is None
    store = TokenStore(config, SecretManagerCacheBackend("qa-project", "teams-qa", api))
    for claims, reason in (
        ({"tid": "wrong"}, "tenant mismatch"),
        ({"tid": config.tenant_id, "upn": "wrong"}, "account mismatch"),
        (
            {"tid": config.tenant_id, "upn": config.personas["admin"].upn, "oid": "wrong"},
            "object mismatch",
        ),
    ):
        config.personas["admin"].expected_oid = "expected"
        with pytest.raises(ReauthRequired, match=reason) as error:
            store.validate("admin", {"access_token": SENTINEL, "id_token_claims": claims})
        assert SENTINEL not in str(error.value)
    with pytest.raises(ValueError, match="shared Secret Manager"):
        store.logout("admin")


def test_unchanged_cache_adds_no_version(monkeypatch: pytest.MonkeyPatch) -> None:
    config = TeamsQaConfig.model_validate(config_source())
    api = FakeSecrets()
    store = TokenStore(config, SecretManagerCacheBackend("qa-project", "teams-qa", api))
    claim = {"tid": config.tenant_id, "upn": config.personas["admin"].upn}
    account = {"username": config.personas["admin"].upn, "realm": config.tenant_id}
    cache = SimpleNamespace(has_state_changed=False, serialize=lambda: SENTINEL)
    client = SimpleNamespace(
        get_accounts=lambda: [account],
        acquire_token_silent=lambda *_args, **_kw: {
            "access_token": "opaque",
            "id_token_claims": claim,
        },
    )
    monkeypatch.setattr(store, "_client", lambda _persona: (client, cache))
    store.acquire("admin", ["User.Read"])
    assert not api.versions
    cache.has_state_changed = True
    store.acquire("admin", ["User.Read"])
    assert len(api.versions) == 1
    assert store.version_added


def test_refresh_reports_and_continues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = TeamsQaConfig.model_validate(config_source())
    monkeypatch.setattr(TeamsQaConfig, "load", lambda *_args: config)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    class FakeStore:
        version_added = True

        def __init__(self, _config: TeamsQaConfig) -> None:
            pass

        def acquire(
            self, persona: str, scopes: list[str], *, force_refresh: bool
        ) -> SimpleNamespace:
            assert force_refresh and "Chat.ReadWrite" in scopes
            if persona == "member":
                raise ReauthRequired(persona, "account unavailable")
            return SimpleNamespace(value="opaque")

        def status(self, _persona: str) -> dict[str, str]:
            return {"expiry": "2026-10-19T00:00:00+00:00"}

    monkeypatch.setattr(cli, "TokenStore", FakeStore)
    assert (
        cli.main(["refresh", "--all", "--config-secret", "projects/qa-project/secrets/config"]) == 1
    )
    output = capsys.readouterr()
    lines = [json.loads(line) for line in output.out.splitlines()]
    assert lines == [
        {
            "persona": "admin",
            "ok": True,
            "version_added": True,
            "access_expiry": "2026-10-19T00:00:00+00:00",
        },
        {"persona": "member", "ok": False, "version_added": False, "access_expiry": None},
    ]
    assert "member: reauthentication required (account unavailable)" in output.err
    assert "::error title=Teams QA reauth::" in output.err
    assert "--config-secret <TEAMS_QA_CONFIG_SECRET>" in output.err
    assert "qa-project" not in output.err
    assert SENTINEL not in output.out + output.err
