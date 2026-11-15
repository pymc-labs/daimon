"""A switched agent's sandbox is set up as its source environment, as far as the provider can.

OpenAI's tenant environment and Gemini's interaction environment carry the source's
packages and network rules; what one can't recreate stays a named loss
(`finer_drops.kept_environment`), and a session made before is replaced once.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
import yaml
from daimon.core.finer_drops import kept_environment, sandbox_losses
from daimon.core.host_web_fetch import PINNED_HOST_TOOLS
from daimon.core.stores.domain import SkillSizeRow
from daimon.core.turn.openai_environment import environment_name, operation_key, tenant_environment
from daimon.core.turn.session_skills import environment_inline_bytes, fitting_skills
from mux.contracts.ids import Scope, SkillRef
from mux.contracts.resources import Environment, EnvironmentSpec, NetworkPolicy
from mux.drivers.gemini.packages import HOOKS_TARGET, SCRIPT_TARGET
from mux.profiles import PROFILES
from mux.state.memory import MemoryStateStore

from ..test_agent_backend import owned as owned
from .test_declared_drop_preparation import consent_request as consent_request
from .test_gemini_full_source import PARAMS as GEMINI_PARAMS
from .test_gemini_full_source import ROOT
from .test_gemini_template_mcp import template as gemini_template  # noqa: F401
from .test_openai_full_source import PARAMS as OPENAI_PARAMS
from .test_openai_template_mcp import _turn as openai_turn
from .test_openai_template_mcp import handoff_mcp
from .test_openai_template_mcp import template as openai_template  # noqa: F401
from .test_openai_tenant_environment import DEPLOYMENT, PROJECT, TENANT, Clock, FakeEnvironments

GEMINI, OPENAI = PROFILES["gemini.inline_reuse"], PROFILES["openai.persistent_workspace"]
SANDBOX = {"sandbox", "installed_packages", "network_rules", "workspace_files"}
LIMITED = NetworkPolicy(mode="limited", allowed_hosts=("a.test",))


def default_environment() -> dict:
    """The default agent's default environment, as Anthropic reads it back."""
    config = yaml.safe_load((ROOT / "defaults/environments/default.yaml").read_text())["config"]
    packages = {"apt": [], "cargo": [], "gem": [], "go": [], "npm": [], "pip": []}
    return {
        "config": {
            **config,
            "networking": {"type": "unrestricted"},
            "packages": {**packages, **config["packages"]},
        }
    }


#: A source with no packages and egress limited to one host, as Anthropic reads it back.
LIMITED_ENVIRONMENT = {
    "config": {
        "type": "cloud",
        "networking": {
            "type": "limited",
            "allowed_hosts": ["a.test"],
            "allow_mcp_servers": False,
            "allow_package_managers": False,
        },
        "packages": {"apt": [], "cargo": [], "gem": [], "go": [], "npm": [], "pip": []},
    }
}


@pytest.fixture
async def source_mcp(consent_request):
    async with handoff_mcp(consent_request[0], PINNED_HOST_TOOLS) as app:
        yield app


def _spec(**fields: object) -> EnvironmentSpec:
    return EnvironmentSpec.model_validate({"name": "source", **fields})


@pytest.mark.parametrize(
    "profile,source,lost",
    [
        (GEMINI, _spec(packages={"python": ("numpy",)}), ()),
        (GEMINI, _spec(network=LIMITED), ()),
        (GEMINI, _spec(network=NetworkPolicy(mode="none")), ()),
        # Gemini installs packages over the network at the first tool call.
        (GEMINI, _spec(packages={"python": ("numpy",)}, network=LIMITED), ("installed_packages",)),
        (OPENAI, _spec(packages={"system": ("gh",), "npm": ("left-pad",)}, network=LIMITED), ()),
        # OpenAI's restricted network takes at most 100 hosts.
        (
            OPENAI,
            _spec(
                network=NetworkPolicy(
                    mode="limited", allowed_hosts=tuple(f"h{i}.test" for i in range(101))
                )
            ),
            ("network_rules",),
        ),
        # Neither installs cargo, gem or go packages.
        (OPENAI, _spec(packages={"cargo": ("ripgrep",)}), ("installed_packages",)),
        (GEMINI, _spec(packages={"go": ("example.com/tool",)}), ("installed_packages",)),
    ],
)
def test_kept_environment_when_switched_then_only_what_the_provider_cannot_set_up_is_lost(
    profile, source, lost
):
    """The sandbox recreates the packages and network it can; the rest is named by part."""
    kept = kept_environment(profile.profile_id, source)
    assert sandbox_losses(source, kept) == lost, "only what the provider can't recreate is lost"


def test_kept_environment_when_plain_then_the_default_sandbox_serves():
    """A plain environment needs no setup: both providers serve their default sandbox."""
    for profile in (GEMINI, OPENAI):
        assert kept_environment(profile.profile_id, _spec()) is None, profile.profile_id
    assert sandbox_losses(_spec(), None) == (), "nothing is lost"


def test_fitting_skills_when_gemini_sets_up_packages_then_their_hook_shares_the_budget():
    """The package hook's inline bytes come out of the 2 MiB that skills share with it."""
    kept = kept_environment(GEMINI.profile_id, _spec(packages={"python": ("numpy",)}))
    reserved = environment_inline_bytes(GEMINI, kept)
    assert reserved > 0, "the hook and its script are inline sources"
    assert environment_inline_bytes(OPENAI, kept) == 0, "OpenAI mounts no inline files"
    assert environment_inline_bytes(GEMINI, None) == 0, "a bare sandbox spends nothing"
    limits = GEMINI.skill_limits
    assert limits is not None and limits.combined_bytes is not None
    half = limits.combined_bytes // 2
    pins = (SkillRef(id="a", version="1"), SkillRef(id="b", version="1"))
    sizes = {
        (pin.id, "1"): SkillSizeRow(
            tenant_id=uuid.uuid4(),
            skill_id=pin.id,
            version="1",
            name=pin.id,
            total_bytes=half,
            largest_file_bytes=half,
            file_count=1,
            text_only=True,
        )
        for pin in pins
    }
    assert fitting_skills(GEMINI, pins, sizes) == pins, "both fill the budget exactly"
    assert fitting_skills(GEMINI, pins, sizes, reserved=reserved) == pins[:1], (
        "with the hook mounted, the second skill is the named loss the turn would hit"
    )


class SetupEnvironments(FakeEnvironments):
    """Records each created spec; the first is the wire's template, in the default project."""

    def __init__(self) -> None:
        super().__init__()
        self.specs: list[EnvironmentSpec] = []

    def _environment(self, tenant_id: str, name: str) -> Environment:
        from daimon.core.turn.runtime_defaults import DEFAULT_PROJECT

        made = super()._environment(tenant_id, name)
        later = f"-{len(self.created) + 1}" if self.created else ""
        ref = made.ref.model_copy(
            update={"id": f"native-template{later}", "account_scope_id": DEFAULT_PROJECT}
        )
        return made.model_copy(update={"ref": ref})

    async def create(self, scope: Scope, spec: EnvironmentSpec, *, key: str) -> Environment:
        self.specs.append(spec)
        return await super().create(scope, spec, key=key)


async def test_tenant_environment_when_set_up_then_each_setup_is_its_own_environment():
    """A setup gets its own key and name; a bare one keeps the tenant's shared default."""
    store, environments, clock = MemoryStateStore(), SetupEnvironments(), Clock()

    async def get(spec):
        return await tenant_environment(
            store,
            environments,
            tenant_id=TENANT,
            project=PROJECT,
            deployment=DEPLOYMENT,
            now=clock,
            sleep=clock.sleep,
            spec=spec,
        )

    packages = _spec(packages={"python": ("numpy",)})
    await get(None)
    await get(packages)
    await get(packages.model_copy(update={"name": "renamed"}))
    await get(_spec(network=LIMITED))
    assert environments.creates == 3, "one per setup; a name alone is not a setup"
    bare, first, second = environments.specs
    assert bare == EnvironmentSpec(name=environment_name(DEPLOYMENT, TENANT)), "unchanged"
    assert environments.keys[0] == operation_key(PROJECT), "existing tenants keep their record"
    assert first.packages == {"python": ("numpy",)} and second.network == LIMITED
    assert first.name.startswith(f"{bare.name}-") and first.name != second.name
    assert len(set(environments.keys)) == 3, "each setup is recorded under its own key"


async def _deployment(*_args: object) -> str:
    return DEPLOYMENT


@pytest.mark.parametrize(
    "consent_request", [dict(OPENAI_PARAMS, environment=default_environment())], indirect=True
)
@pytest.mark.parametrize("openai_template", [{"code_default": True}], indirect=True)
async def test_default_environment_when_switched_to_openai_then_its_turn_installs_it(
    openai_template,  # noqa: F811
    monkeypatch,
):
    """The tenant's own environment for the agent is created with the source's setup."""
    from daimon.core.turn import openai_deployment

    request, quote, wire, _ = openai_template
    assert not SANDBOX & set(quote.declared_drops), quote.declared_drops
    environments = SetupEnvironments()
    monkeypatch.setattr(openai_deployment, "_environments", lambda _key: environments)
    monkeypatch.setattr(openai_deployment, "_deployment_id", _deployment)
    await openai_turn(request, 0)
    await openai_turn(request, 1)
    (spec,) = environments.specs
    assert spec.packages == {"system": ("gh",), "python": ("typst", "duckdb", "pyarrow")}, (
        "the default environment's packages are set up"
    )
    assert spec.network == NetworkPolicy(), "its network stays open"
    assert spec.name.startswith(environment_name(DEPLOYMENT, request.scope.tenant_id) + "-")
    assert wire.posts[0]["environment"]["environment_template_id"] == "native-template"


@pytest.mark.parametrize(
    "consent_request", [dict(OPENAI_PARAMS, environment=default_environment())], indirect=True
)
@pytest.mark.parametrize("openai_template", [{"code_default": True}], indirect=True)
async def test_openai_thread_when_made_on_the_bare_environment_then_it_moves_once(
    openai_template,  # noqa: F811
    monkeypatch,
):
    """A thread whose session runs on the tenant's bare environment moves to the set-up one."""
    from daimon.core.turn import openai_deployment, openai_environment
    from mux.drivers.openai import deployment

    request, _, wire, _ = openai_template
    environments, carried = SetupEnvironments(), [False]
    monkeypatch.setattr(openai_deployment, "_environments", lambda _key: environments)
    monkeypatch.setattr(openai_deployment, "_deployment_id", _deployment)
    original = openai_environment.tenant_environment

    async def until_now(*args, spec=None, **kwargs):
        # Before environments were carried, every turn asked for the bare one.
        return await original(*args, spec=spec if carried[0] else None, **kwargs)

    def handle(call):
        if call.url.path.endswith("/environments/templates/native-template-2"):
            return httpx.Response(200, json={**wire.template, "id": "native-template-2"})
        return wire.handle(call)

    monkeypatch.setattr(openai_environment, "tenant_environment", until_now)
    monkeypatch.setattr(deployment, "_http_transport", lambda: httpx.MockTransport(handle))
    await openai_turn(request, 0)
    carried[0] = True
    await openai_turn(request, 1)
    await openai_turn(request, 2)
    used = [post["environment"]["environment_template_id"] for post in wire.posts]
    assert used == ["native-template", "native-template-2"], "replaced once, then reused"
    assert environments.specs[1].packages, "the new session's environment is set up"


async def _bind(request, *, reuse: bool):
    from daimon.core.turn.prepare import bind_session_impl

    return await bind_session_impl(
        request.deps,
        request.admission,
        tenant_id=request.tenant_id,
        platform=request.platform,
        external_user_id=request.external_user_id,
        thread_id=request.thread_id,
        session_account_id=request.session_account_id,
        reuse_existing=reuse,
    )


async def _run(request, prepared) -> None:
    from daimon.core.turn.run import run_prepared_turn
    from daimon.testing.turn_fakes import RecordingLifecycle

    async def no_recovery():
        pytest.fail("the turn changed its admitted source/session")

    outcome = await run_prepared_turn(
        request.deps,
        prepared,
        tenant_id=request.tenant_id,
        platform=request.platform,
        thread_id=request.thread_id,
        external_user_id=request.external_user_id,
        user_message="Use the sandbox",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=no_recovery,
        recovery_lifecycle=lambda _: RecordingLifecycle(),
        operation_key="template-environment",
    )
    assert outcome.state.error is None, outcome.state.error


@pytest.mark.parametrize(
    "consent_request,setup",
    [
        (dict(GEMINI_PARAMS, environment=default_environment()), "packages"),
        (dict(GEMINI_PARAMS, environment=LIMITED_ENVIRONMENT), "network"),
    ],
    ids=["packages", "network"],
    indirect=["consent_request"],
)
async def test_source_environment_when_switched_to_gemini_then_its_turn_sets_it_up(
    gemini_template,  # noqa: F811
    setup,
):
    """The interaction's environment installs the packages, or limits egress, as the source."""
    request, quote, _native, calls, _posts = gemini_template
    assert not SANDBOX & set(quote.declared_drops), quote.declared_drops
    await _run(request, await _bind(request, reuse=False))
    (sent, *_) = [
        json.loads(c.content) for c in calls if c.method == "POST" and "interactions" in c.url.path
    ]
    environment = sent["environment"]
    inline = {s["target"]: s["content"] for s in environment["sources"] if s["type"] == "inline"}
    if setup == "packages":
        assert {HOOKS_TARGET, SCRIPT_TARGET} <= set(inline), "the install hook is mounted"
        assert "apt-get install -y -qq gh" in inline[SCRIPT_TARGET], "system packages"
        assert "pip install -q typst duckdb pyarrow" in inline[SCRIPT_TARGET], "python packages"
        assert "network" not in environment, "egress stays open"
    else:
        assert environment["network"] == {"allowlist": [{"domain": "a.test"}]}, "egress limited"
        assert HOOKS_TARGET not in inline, "nothing to install"


@pytest.mark.parametrize(
    "consent_request", [dict(GEMINI_PARAMS, environment=default_environment())], indirect=True
)
async def test_gemini_session_when_made_before_setups_carried_then_it_is_replaced_once(
    gemini_template,  # noqa: F811
):
    """A thread whose session has the bare sandbox moves to one set up as the source, once."""
    from daimon.core.turn.gemini_prepare import SETUP_REF
    from mux.contracts.ids import ThreadRef
    from mux.state.lease import Slot

    request, *_ = gemini_template
    first = await _bind(request, reuse=False)
    store = request.deps.state_store
    slot = Slot(
        thread=ThreadRef(
            channel=request.admission.backend_revision.channel, thread_id=request.thread_id
        ),
        account_id=request.scope.account_id,
    )
    bound = await store.get_binding(slot)
    assert bound is not None and bound.native_refs.get(SETUP_REF), "the setup is recorded"
    # A session an earlier release created: the same binding, with no setup recorded.
    refs = {k: v for k, v in bound.native_refs.items() if k != SETUP_REF}
    old = bound.model_copy(update={"native_refs": refs, "generation": bound.generation + 1})
    await store.put_binding(old, expected_generation=bound.generation)
    moved = await _bind(request, reuse=True)
    assert moved.ma_session_id != first.ma_session_id, "the bare session is replaced"
    again = await _bind(request, reuse=True)
    assert again.ma_session_id == moved.ma_session_id, "and the set-up one is reused"
