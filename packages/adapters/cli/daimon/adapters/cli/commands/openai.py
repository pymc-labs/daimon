"""`daimon openai init` / `daimon openai smoke` — self-host Daimon on an OpenAI-only key.

Both commands talk to the OpenAI Agents API directly through the mux OpenAI
driver's resource ports (`mux.drivers.openai`), never through the full
admission/channel pipeline: there is no tenant/channel database state to
provision against yet when an operator first runs `init`, and `smoke` proves
the native OpenAI path end to end (one agent, one hosted environment, one
skill, one tool call) without needing any. This mirrors `daimon smoke`, which
takes the same direct-to-provider shortcut for the Anthropic path.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Annotated

import typer
from daimon.core.smoke import SmokeCheckError
from daimon.core.turn.openai_deployment import OpenAIDeployment
from daimon.core.turn.runtime_manifest import ConfigurationGrant
from mux.contracts.actions import UserMessage, UserToolResult
from mux.contracts.events import TextPart
from mux.contracts.ids import ModelRef, Revision, Scope, SkillRef
from mux.contracts.resources import (
    AgentSpec,
    EnvironmentSpec,
    SessionSpec,
    SkillUpload,
    SkillUploadFile,
    ToolSpec,
)
from mux.drivers.openai import (
    Context,
    MemoryRecoveryJournal,
    OpenAIAgents,
    OpenAIEnvironments,
    OpenAIEvents,
    OpenAISessions,
    OpenAISkills,
    bundle_digest,
    configured_transport,
)
from mux.errors import MuxError
from mux.profiles.openai import PERSISTENT_WORKSPACE
from rich.console import Console

PROVIDER_NAME = PERSISTENT_WORKSPACE.provider
"""The subcommand name this module registers under — derived from the
profile contract, never hand-typed, so it can't drift from it."""

openai_app = typer.Typer(help="Self-host Daimon on an OpenAI-only key.")

_PERSISTENT_WORKSPACE = PERSISTENT_WORKSPACE.profile_id
_WILDCARD = "*"
_MANIFEST_ENV = "DAIMON_TURN__PROVIDER_RUNTIME_FILE"
_SMOKE_TENANT_ID = uuid.uuid5(uuid.NAMESPACE_URL, "daimon-openai-smoke")
_SMOKE_TOOL_NAME = "daimon_smoke_echo"
_SMOKE_SKILL_CONTENT = b"""---
name: daimon-smoke
description: Smoke-test skill used only by `daimon openai smoke`.
---

# Daimon smoke skill

This skill exists only to prove skill upload, download and digest
verification work end to end against the real OpenAI API.
"""


def _console() -> Console:
    return Console(highlight=False)


def _run(coro: object, *, console: Console) -> None:
    try:
        asyncio.run(coro)  # type: ignore[arg-type]
    except MuxError as err:
        console.print(f"✗ {err}", style="red", markup=False)
        raise typer.Exit(code=1) from err
    except SmokeCheckError as err:
        console.print(f"smoke: FAIL {err}", style="red", markup=False)
        raise typer.Exit(code=1) from err


def _api_key(api_key_env: str) -> str:
    value = os.environ.get(api_key_env)
    if not value:
        raise typer.BadParameter(f"Export {api_key_env} with your OpenAI API key first.")
    return value


def _manifest_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    env = os.environ.get(_MANIFEST_ENV)
    if not env:
        raise typer.BadParameter(
            f"Pass --manifest or export {_MANIFEST_ENV} with the runtime file path."
        )
    return Path(env)


def _read_rows(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return list(json.loads(path.read_text()))


def _write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    # Atomic write: the serving services read this same file live from a
    # shared volume on every admission check. A plain write_text would let
    # one briefly observe a truncated or partial document mid-write.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(rows, indent=2) + "\n")
    tmp.replace(path)


def _skill_bundle(skill_dir: Path) -> SkillUpload:
    files = tuple(
        SkillUploadFile(path=file.relative_to(skill_dir).as_posix(), content=file.read_bytes())
        for file in sorted(skill_dir.rglob("*"))
        if file.is_file()
    )
    return SkillUpload(files=files)


async def _seed_default_skills(
    skills: OpenAISkills, scope: Scope, defaults_root: Path, *, key_prefix: str
) -> tuple[SkillRef, ...]:
    """Upload every `defaults/skills/*` directory as a native OpenAI skill,
    matching what `daimon defaults apply` seeds on the Anthropic path.

    A skill a default agent limits to some platforms (`microsoft-365`) is left
    out: only the Anthropic backend adds those, per session on their platforms.
    """
    from daimon.core.defaults.loader import load_platform_skills, load_skill_paths

    limited = {
        ref.skill_id for refs in load_platform_skills(defaults_root).values() for ref in refs
    }
    refs: list[SkillRef] = []
    for skill_dir in load_skill_paths(defaults_root / "skills"):
        if skill_dir.name in limited:
            continue
        bundle = _skill_bundle(skill_dir)
        skill = await skills.create(scope, bundle, key=f"{key_prefix}:{skill_dir.name}")
        assert skill.latest_version is not None
        refs.append(
            SkillRef(
                id=skill.id, version=skill.latest_version.version, digest=bundle_digest(bundle)
            )
        )
    return tuple(refs)


@openai_app.command("init")
def openai_init_command(
    tenant_id: Annotated[
        uuid.UUID, typer.Option("--tenant-id", help="The tenant this OpenAI deployment serves.")
    ],
    project: Annotated[
        str, typer.Option("--project", help="Your OpenAI project ID (from the OpenAI dashboard).")
    ],
    model: Annotated[str, typer.Option("--model", help="OpenAI model ID for the saved agent.")],
    name: Annotated[
        str, typer.Option("--name", help="Display name for the saved agent.")
    ] = "daimon",
    platform: Annotated[
        str,
        typer.Option(
            "--platform", help='Channel platform to authorize, or "*" for every platform.'
        ),
    ] = _WILDCARD,
    channel_id: Annotated[
        str,
        typer.Option("--channel-id", help='Channel ID to authorize, or "*" for every channel.'),
    ] = _WILDCARD,
    account_id: Annotated[
        str,
        typer.Option("--account-id", help='Account ID to authorize, or "*" for every account.'),
    ] = _WILDCARD,
    api_key_env: Annotated[
        str,
        typer.Option(
            "--api-key-env",
            help="Env var carrying the OpenAI key: read now to provision, recorded by name only.",
        ),
    ] = "DAIMON_CHANNEL_OPENAI_KEY",
    manifest: Annotated[
        Path | None,
        typer.Option("--manifest", help=f"Runtime file path; defaults to ${_MANIFEST_ENV}."),
    ] = None,
    defaults_root: Annotated[
        Path,
        typer.Option("--defaults-root", help="Defaults tree to seed the new agent's skills from."),
    ] = Path("defaults"),
    skip_default_skills: Annotated[
        bool,
        typer.Option("--skip-default-skills", help="Create the agent with no skills attached."),
    ] = False,
) -> None:
    """Create (or reuse) a project-scoped OpenAI agent, plus a hosted
    environment for `openai.persistent_workspace`, and record them as one row
    in the provider runtime file. Idempotent: an existing row with the same
    profile/tenant/platform/channel/account is reused unchanged. Prints only
    IDs — never the API key.

    A fresh `openai.persistent_workspace` agent is seeded with every skill
    under `--defaults-root/skills` (the same ones `daimon defaults apply`
    seeds on the Anthropic path), unless `--skip-default-skills` is given.
    Reusing an existing row does not re-sync skills.
    """
    console = _console()
    profile = _PERSISTENT_WORKSPACE
    path = _manifest_path(manifest)
    rows = _read_rows(path)
    selector = {
        "profile": profile,
        "tenant_id": str(tenant_id),
        "platform": platform,
        "channel_id": channel_id,
        "account_id": account_id,
    }
    existing = next(
        (row for row in rows if all(row.get(k) == v for k, v in selector.items())), None
    )
    if existing is not None:
        if existing.get("api_key_env") != api_key_env:
            console.print(
                f"openai init: this row still uses api_key_env={existing.get('api_key_env')!r} — "
                f"--api-key-env {api_key_env!r} was not applied. Edit the manifest row directly "
                "to change it; init never rewrites an existing row."
            )
        console.print(
            "openai init: reused existing row "
            f"agent_id={existing.get('agent_id')} environment_id={existing.get('environment_id')}"
        )
        return

    key = _api_key(api_key_env)

    async def _do_init() -> None:
        transport = configured_transport(api_key=key, project=project)
        context = Context(transport, project, profile, None)
        scope = Scope(
            tenant_id=str(tenant_id),
            account_id="daimon-init",
            principal_id="daimon-cli",
            authorization_id="openai-init",
            platform_reason="daimon openai init: operator-authorized bootstrap",
        )
        default_skills: tuple[SkillRef, ...] = ()
        if not skip_default_skills:
            # Tenant-scoped, not per-channel: skills are a shared resource an
            # operator's narrower, per-channel `init` runs (step 3's
            # non-wildcard alternative) should reuse rather than re-upload a
            # fresh duplicate set for every channel.
            default_skills = await _seed_default_skills(
                OpenAISkills(context),
                scope,
                defaults_root,
                key_prefix=f"daimon-openai-init-skill:{tenant_id}",
            )

        agents = OpenAIAgents(context)
        agent_key = f"daimon-openai-init-agent:{tenant_id}:{platform}:{channel_id}:{account_id}"
        agent = await agents.create(
            scope,
            AgentSpec(
                name=name,
                model=ModelRef(provider=PROVIDER_NAME, id=model),
                skills=default_skills or None,
            ),
            key=agent_key,
        )

        environment_id: str | None = None
        environments = OpenAIEnvironments(context)
        environment_key = (
            f"daimon-openai-init-environment:{tenant_id}:{platform}:{channel_id}:{account_id}"
        )
        environment = await environments.create(
            scope, EnvironmentSpec(name=name), key=environment_key
        )
        environment_id = environment.ref.id

        grant = ConfigurationGrant(
            profiles=(profile,),
            models=(model,),
            thread_modes=("per_caller", "shared"),
            requires={},
        )
        row = OpenAIDeployment.model_validate(
            {
                "profile": profile,
                "tenant_id": str(tenant_id),
                "platform": platform,
                "channel_id": channel_id,
                "account_id": account_id,
                "configuration_grant": grant.model_dump(mode="json"),
                "project": project,
                "agent_id": agent.ref.id,
                "environment_id": environment_id,
                "api_key_env": api_key_env,
                "skill_ids": [ref.id for ref in default_skills],
            }
        )
        rows.append(row.model_dump(mode="json", exclude_none=True))
        _write_rows(path, rows)
        console.print(
            f"openai init: OK project={project} agent_id={agent.ref.id} "
            f"environment_id={environment_id} skills={len(default_skills)}"
        )

    _run(_do_init(), console=console)


@openai_app.command("smoke")
def openai_smoke_command(
    project: Annotated[
        str, typer.Option("--project", help="Your OpenAI project ID (from the OpenAI dashboard).")
    ],
    model: Annotated[str, typer.Option("--model", help="OpenAI model ID for the smoke agent.")],
    api_key_env: Annotated[
        str,
        typer.Option("--api-key-env", help="Env var carrying the OpenAI key."),
    ] = "DAIMON_CHANNEL_OPENAI_KEY",
    tenant_id: Annotated[
        uuid.UUID,
        typer.Option(
            "--tenant-id",
            help="Tenant stamped on the smoke agent. A fixed dedicated identity by default.",
        ),
    ] = _SMOKE_TENANT_ID,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds to bound the smoke turn.")
    ] = 120.0,
) -> None:
    """Provision a dedicated smoke agent, environment and skill, run one
    billed chat turn that must call one tool, and exit nonzero unless it
    completed with exactly one tool call. Proves the real OpenAI driver path
    — including skill upload, publish and content verification — end to end.
    """
    console = _console()
    key = _api_key(api_key_env)

    async def _do_smoke() -> None:
        transport = configured_transport(api_key=key, project=project)
        context = Context(transport, project, _PERSISTENT_WORKSPACE, None)
        scope = Scope(
            tenant_id=str(tenant_id),
            account_id="daimon-smoke",
            principal_id="daimon-cli",
            authorization_id="openai-smoke",
            platform_reason="daimon openai smoke: self-contained post-deploy check",
        )
        agents = OpenAIAgents(context)
        environments = OpenAIEnvironments(context)
        skills = OpenAISkills(context)
        sessions = OpenAISessions(context)
        events = OpenAIEvents(context, sessions, MemoryRecoveryJournal())

        console.print("smoke: creating skill")
        bundle = SkillUpload(
            files=(SkillUploadFile(path="SKILL.md", content=_SMOKE_SKILL_CONTENT),)
        )
        skill = await skills.create(scope, bundle, key=f"daimon-openai-smoke-skill:{tenant_id}")
        # `create` already establishes version 1; a separate `publish_version`
        # call would be redundant and double the post-publish read-lag window.
        assert skill.latest_version is not None
        version = skill.latest_version.version
        digest = bundle_digest(bundle)

        console.print("smoke: creating agent")
        agent = await agents.create(
            scope,
            AgentSpec(
                name="daimon-smoke",
                model=ModelRef(provider=PROVIDER_NAME, id=model),
                system=(
                    "You are a deployment smoke test. When the user says "
                    f"'run smoke check', call the {_SMOKE_TOOL_NAME} tool exactly once "
                    "with text='ok', then reply with the single word done."
                ),
                tools=(
                    ToolSpec(
                        name=_SMOKE_TOOL_NAME,
                        kind="custom",
                        description="Echo back the given text. Call this exactly once.",
                        input_schema={
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    ),
                ),
                skills=(SkillRef(id=skill.id, version=version, digest=digest),),
            ),
            key=f"daimon-openai-smoke-agent:{tenant_id}",
        )

        console.print("smoke: creating environment")
        environment = await environments.create(
            scope,
            EnvironmentSpec(name="daimon-smoke"),
            key=f"daimon-openai-smoke-environment:{tenant_id}",
        )

        console.print("smoke: starting session")
        session = await sessions.create(
            scope,
            SessionSpec(
                agent=agent.ref,
                agent_revision=Revision(local=0, native=None),
                environment=environment.ref,
                config_revision=0,
            ),
            key=f"daimon-openai-smoke-session:{tenant_id}:{uuid.uuid4()}",
            initial=UserMessage(content=(TextPart(text="run smoke check"),)),
        )

        console.print("smoke: waiting for the turn")
        deadline = time.monotonic() + timeout
        answered: set[str] = set()
        # Observed live: `create`'s own response can read "idle" with no
        # required actions even though the initial message was just queued —
        # the turn hasn't started processing yet. Never treat "idle" as
        # terminal until we've first observed the turn actually in flight
        # (running or requires_action), or a fast turn can look like zero
        # tool calls happened before it ever started.
        seen_in_flight = False
        while not (seen_in_flight and session.state in ("idle", "terminated")):
            if session.state not in ("idle", "terminated"):
                seen_in_flight = True
            pending = [
                action
                for action in session.required_actions
                if action.kind == "function_result" and action.id not in answered
            ]
            for action in pending:
                answered.add(action.id)
                await events.send(
                    scope,
                    session.ref,
                    (UserToolResult(action_id=action.id, content=(TextPart(text="ok"),)),),
                    key=f"daimon-openai-smoke-result:{tenant_id}:{action.id}",
                )
            if time.monotonic() > deadline:
                raise SmokeCheckError(f"smoke turn timed out after {timeout}s")
            await asyncio.sleep(1.0)
            session = await sessions.retrieve(scope, session.ref)

        if session.state != "idle":
            raise SmokeCheckError(f"smoke turn ended in state {session.state!r}, not idle")
        if len(answered) != 1:
            raise SmokeCheckError(f"expected exactly one tool call, observed {len(answered)}")

        console.print(
            "smoke: OK "
            f"agent_id={agent.ref.id} environment_id={environment.ref.id} "
            f"skill_id={skill.id} tool_calls={len(answered)}"
        )

    _run(_do_smoke(), console=console)
