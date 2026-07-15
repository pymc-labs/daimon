"""CLI integration tests for `daimon skills sync-agent`.

Patterns enforced:
- `_parse_repo_arg` is a pure function — synchronous parametric tests.
- The pipeline test calls `_sync_agent_impl` directly via the
  `http_client` injection seam (no `httpx` monkey-patching).
- Real Postgres via `db_session_factory`; transport-level fake
  `AsyncAnthropic` via `MARouter`; no `model_construct`, no `AsyncMock`.
- `typer.testing.CliRunner` exercises the no-repos error path only —
  it exits before the runtime/network layer.
"""

from __future__ import annotations

import io
import tarfile
from io import StringIO
from typing import cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import SkillListResponse
from cryptography.fernet import Fernet, MultiFernet
from daimon.adapters.cli.commands.skills import (
    _parse_repo_arg,
    skills_app,
    sync_agent,
)
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings
from daimon.core.github_credentials import encrypt_token
from daimon.core.specs import SkillRepo
from daimon.core.stores.github_credentials import upsert_credential
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, list_response
from pydantic import SecretStr
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

# ---------------------------------------------------------------------------
# _parse_repo_arg — pure function
# ---------------------------------------------------------------------------


def test_parse_repo_arg_bare_url_defaults_to_main_branch() -> None:
    spec = _parse_repo_arg("https://github.com/o/r")
    assert spec == SkillRepo(url="https://github.com/o/r", branch="main", path="", split=False), (
        "bare URL should default to branch=main, empty path, split=False"
    )


def test_parse_repo_arg_extracts_branch_from_at_suffix() -> None:
    spec = _parse_repo_arg("https://github.com/o/r@dev")
    assert spec.branch == "dev", "branch must be parsed from @suffix"
    assert spec.url == "https://github.com/o/r"


def test_parse_repo_arg_extracts_branch_and_path() -> None:
    spec = _parse_repo_arg("https://github.com/o/r@dev#skills")
    assert spec.branch == "dev"
    assert spec.path == "skills", "subdirectory path must come from #suffix"
    assert spec.url == "https://github.com/o/r"


def test_parse_repo_arg_split_flag_alone() -> None:
    spec = _parse_repo_arg("https://github.com/o/r?split")
    assert spec.split is True, "?split must enable split mode"
    assert spec.branch == "main"
    assert spec.url == "https://github.com/o/r"


def test_parse_repo_arg_branch_and_split_combined() -> None:
    spec = _parse_repo_arg("https://github.com/o/r@main?split")
    assert spec.branch == "main"
    assert spec.split is True
    assert spec.url == "https://github.com/o/r"


# ---------------------------------------------------------------------------
# Helpers — runtime + settings stubs
# ---------------------------------------------------------------------------


class _FakeCli:
    local_user = "testuser"


class _FakeCrypto:
    def __init__(self, keys: tuple[SecretStr, ...]) -> None:
        self.keys = keys


class _FakeSettings:
    def __init__(self, fernet_key: bytes) -> None:
        self.cli = _FakeCli()
        self.crypto = _FakeCrypto(keys=(SecretStr(fernet_key.decode("utf-8")),))


def _make_rt(
    db_session_factory: async_sessionmaker[AsyncSession],
    anthropic_client: AsyncAnthropic,
    fernet_key: bytes,
) -> CliRuntime:
    rt = cast(CliRuntime, object.__new__(CliRuntime))
    object.__setattr__(rt, "settings", cast(Settings, _FakeSettings(fernet_key)))
    object.__setattr__(rt, "anthropic", anthropic_client)
    object.__setattr__(rt, "sessionmaker", db_session_factory)
    return rt


def _build_anthropic(router: MARouter) -> AsyncAnthropic:
    transport = httpx.MockTransport(router.dispatch)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    return AsyncAnthropic(api_key="test", http_client=http_client)


def _make_tarball(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, content in files.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _sync_agent_impl — integration test against real Postgres + fakes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_agent_impl_renders_report_after_first_create(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """First sync uploads one skill, persists the row, and renders 'synced (new)' = 1."""
    tenant = await make_tenant(db_session)
    principal = await get_or_create_cli_principal(
        db_session, tenant_id=tenant.id, os_user="testuser"
    )
    await db_session.commit()

    fernet_key = Fernet.generate_key()
    fernet = MultiFernet([Fernet(fernet_key)])

    async with db_session_factory() as s, s.begin():
        await upsert_credential(
            s,
            principal_id=principal.account_id,
            github_login="tester",
            encrypted_token=encrypt_token(fernet, "test-pat"),
            scopes=("repo",),
        )

    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})
    github_http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, content=tarball))
    )

    router = MARouter()
    router.add(
        "POST",
        r"/v1/skills",
        lambda _req, _m: httpx.Response(
            200,
            json=SkillListResponse(
                id="sk_new",
                type="custom",
                display_title="anything",
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        ),
    )
    # Attach step (fix #40) lists agents to bind synced skills. No daimon-tagged
    # agent exists in this fake workspace, so the attach short-circuits and the
    # sync metrics below are unaffected.
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    rt = _make_rt(db_session_factory, anthropic_client, fernet_key)
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, width=120)

    await sync_agent(
        rt=rt,
        console=console,
        agent_name="test-agent",
        repos=[SkillRepo(url="https://github.com/o/r", branch="main")],
        http_client=github_http,
    )

    out = buf.getvalue()
    assert "synced (new)" in out, "rendered table must include the synced metric label"
    assert "1" in out, "rendered table must include the synced count of 1"


@pytest.mark.asyncio
async def test_sync_agent_impl_proceeds_without_pat_for_public_repo(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No GitHub credential row is PAT-optional: the public repo is still fetched
    (no Authorization header) and its skill syncs. get_pat resolves to None and
    the orchestrator proceeds rather than raising."""
    tenant = await make_tenant(db_session)
    await get_or_create_cli_principal(db_session, tenant_id=tenant.id, os_user="testuser")
    await db_session.commit()

    fernet_key = Fernet.generate_key()

    tarball = _make_tarball({"r-main/SKILL.md": b"---\nname: r\ndescription: d\n---\nbody"})
    fetched_with_auth: list[bool] = []

    def _serve_tarball(req: httpx.Request) -> httpx.Response:
        fetched_with_auth.append("authorization" in req.headers)
        return httpx.Response(200, content=tarball)

    github_http = httpx.AsyncClient(transport=httpx.MockTransport(_serve_tarball))

    router = MARouter()
    router.add(
        "POST",
        r"/v1/skills",
        lambda _req, _m: httpx.Response(
            200,
            json=SkillListResponse(
                id="sk_new",
                type="custom",
                display_title="anything",
                latest_version="1",
                created_at="2026-04-21T00:00:00Z",
                updated_at="2026-04-21T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        ),
    )
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response([]))
    anthropic_client = _build_anthropic(router)

    rt = _make_rt(db_session_factory, anthropic_client, fernet_key)
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, width=120)

    await sync_agent(
        rt=rt,
        console=console,
        agent_name="test-agent",
        repos=[SkillRepo(url="https://github.com/o/r", branch="main")],
        http_client=github_http,
    )

    assert fetched_with_auth == [False], (
        "public repo must be fetched without an Authorization header when no PAT is seeded"
    )
    assert "synced (new)" in buf.getvalue(), "skill from a public repo must sync even without a PAT"


# ---------------------------------------------------------------------------
# Typer-level test: no --repo flag exits before any network use
# ---------------------------------------------------------------------------


def test_skills_sync_agent_command_errors_when_no_repos_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invoking the subcommand without --repo exits with code 2 and a helpful message.

    Sets the env vars `load_settings()` requires so we exercise the no-repos
    guard, not a Settings validation crash.
    """
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://x:y@localhost/z")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "test")
    runner = CliRunner()
    result = runner.invoke(skills_app, ["sync-agent", "test-agent"])
    assert result.exit_code == 2, (
        f"missing --repo must exit with code 2, got {result.exit_code}; stdout={result.stdout!r}"
    )
    assert "No repositories provided" in result.stdout, (
        f"error message must mention missing repos; got {result.stdout!r}"
    )
