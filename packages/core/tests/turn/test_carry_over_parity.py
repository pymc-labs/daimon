"""Every provider move carries the conversation over, in every direction.

Pairs come from the live preparation registry plus Managed Agents; each host
module and the Managed Agents bind path are checked for the one core helper.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from daimon.core.channel_backend import channel_ref
from daimon.core.turn import carry_over, gemini_prepare
from daimon.core.turn import prepare as preparation
from daimon.core.turn.admission import Admission
from daimon.core.turn.carry_over import CarriedOver, carry_on_adoption
from daimon.core.turn.prepare import ContinuityOutcome, ProviderPreparationRequest
from daimon.core.turn.replace_binding import OldSession
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ResourceRef
from mux.contracts.resources import ProviderBinding, ResourceBinding
from mux.profiles import MANAGED_AGENTS, backend_selection
from mux.state.memory import MemoryStateStore
from mux.state.store import RELEASED_REF, released_successor

from tests.turn.test_binding_replacement_parity import (  # pyright: ignore[reportMissingTypeStubs]
    HOSTS,
    SCOPE,
    SLOT,
    THREAD,
    _Backend,  # pyright: ignore[reportPrivateUsage]
)

MA = MANAGED_AGENTS.profile_id
PROFILES = (*sorted(HOSTS), MA)
PAIRS = [(s, t) for s in PROFILES for t in PROFILES if s.split(".")[0] != t.split(".")[0]]
RELEASED_AT = datetime(2026, 11, 1, 12, tzinfo=UTC)


def _provider(profile: str) -> str:
    return MANAGED_AGENTS.provider if profile == MA else profile.split(".", 1)[0]


def _revision(profile: str, local: int) -> ConfigRevision:
    backend = _provider(profile)
    resolved = resolve_default(
        BackendConfig(backend=backend, profile=profile, model=backend_selection(backend).model)  # pyright: ignore[reportArgumentType]
    )
    return ConfigRevision.create(THREAD.channel, local, resolved)


def _old_binding(profile: str, revision: int, refs: dict[str, str]) -> ProviderBinding:
    return ProviderBinding(
        id="binding-1",
        thread=THREAD,
        provider=_provider(profile),  # pyright: ignore[reportArgumentType]
        profile=profile,
        native_refs=refs,
        generation=1,
        config_revision=revision,
        legacy_account_id="a",
    )


def _ref(binding: ProviderBinding) -> ResourceRef:
    return ResourceRef(
        id="old-session",
        kind="session",
        provider=binding.provider,
        account_scope_id="scope-1",
    )


async def _request(
    monkeypatch: pytest.MonkeyPatch,
    store: MemoryStateStore,
    *,
    target: str,
    rows: list[SimpleNamespace],
    runtimes: dict[str, object] | None = None,
    backend: object | None = None,
) -> ProviderPreparationRequest:
    """A preparation into `target`; `rows` are the thread's session rows, newest last."""
    backend = backend if backend is not None else _Backend()

    async def get_row(session: object, **_: object) -> object:
        return rows[-1] if rows else None

    def reader(
        runtime: object, revision: object, scope: object, binding: ProviderBinding, store: object
    ) -> OldSession:
        return OldSession(binding=binding, ref=_ref(binding), backend=backend)  # pyright: ignore[reportArgumentType]

    @contextlib.asynccontextmanager
    async def sessionmaker() -> AsyncIterator[None]:
        yield None

    def ma_backend(*_: object, **__: object) -> _Backend:
        return backend

    def ma_ref(*_: object, **__: object) -> ResourceRef:
        return _ref(_old_binding(MA, 0, {}))

    monkeypatch.setattr(carry_over, "get_thread_session_at", get_row)
    monkeypatch.setattr(carry_over, "managed_agents", ma_backend)
    monkeypatch.setattr(carry_over, "resource_ref", ma_ref)
    for profile in HOSTS:
        monkeypatch.setitem(carry_over._READERS, profile, reader)  # pyright: ignore[reportPrivateUsage]
    request = SimpleNamespace(
        deps=SimpleNamespace(
            sessionmaker=sessionmaker,
            turn_runtimes=runtimes if runtimes is not None else {p: object() for p in HOSTS},
            turn_runtime_factory=None,
            anthropic=None,
        ),
        admission=SimpleNamespace(
            backend_revision=None
            if target == MA
            else await store.put_config_revision(_revision(target, 2))
        ),
        scope=SCOPE,
        tenant_id=uuid.uuid4(),
        platform="slack",
        thread_id="x",
        session_account_id=uuid.uuid4(),
        reuse_existing=True,
        now=lambda: RELEASED_AT + timedelta(minutes=1),
    )
    return cast(ProviderPreparationRequest, request)


def _row(at: datetime, session_id: str = "old-session") -> SimpleNamespace:
    return SimpleNamespace(ma_session_id=session_id, created_at=at)


async def _move(
    monkeypatch: pytest.MonkeyPatch,
    store: MemoryStateStore,
    *,
    source: str,
    target: str,
    ma_row_at: datetime | None,
    runtimes: dict[str, object] | None = None,
) -> CarriedOver | None:
    rows = [_row(ma_row_at)] if ma_row_at is not None else []
    request = await _request(monkeypatch, store, target=target, rows=rows, runtimes=runtimes)
    return await carry_on_adoption(
        request,
        store=store,
        slot=SLOT,
        model_id="model-1",
        agent_name="Daimon",
    )


async def _released(store: MemoryStateStore, profile: str) -> None:
    revision = await store.put_config_revision(_revision(profile, 1))
    old = _old_binding(
        profile, revision.local, {"session": "old-session", "account_scope": "scope-1"}
    )
    await store.put_binding(old, expected_generation=0)
    await store.put_binding(released_successor(old, released_at=RELEASED_AT), expected_generation=1)


@pytest.mark.parametrize(("source", "target"), PAIRS)
async def test_provider_move_when_confirmed_then_carries_the_excerpt(
    monkeypatch: pytest.MonkeyPatch, source: str, target: str
) -> None:
    store = MemoryStateStore()
    if source != MA:
        await _released(store, source)
    carried = await _move(
        monkeypatch,
        store,
        source=source,
        target=target,
        ma_row_at=RELEASED_AT - timedelta(hours=1) if source == MA else None,
    )
    assert carried is not None, f"{source} -> {target} carried nothing"
    continuity = carried.continuity
    assert continuity.state == "replaced", f"{source} -> {target}: {continuity.state}"
    assert "What is the answer?" in continuity.user_prefix, "excerpt missing"


@pytest.mark.parametrize("target", [p for p in PROFILES if p != "openai.persistent_workspace"])
async def test_legacy_release_when_carried_then_gets_the_lost_framing(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    store = MemoryStateStore()
    old = _old_binding("openai.persistent_workspace", 1, {"session": "old-session"})
    await store.put_binding(old, expected_generation=0)
    legacy = old.model_copy(update={"generation": 2, "native_refs": {RELEASED_REF: "1"}})
    await store.put_binding(legacy, expected_generation=1)
    carried = await _move(monkeypatch, store, source="", target=target, ma_row_at=None)
    assert carried is not None, f"legacy release into {target} carried nothing"
    assert carried.continuity.state == "replaced_after_loss", carried.continuity.state
    assert "was lost" in carried.continuity.user_prefix, "lost framing missing"


async def test_missing_runtime_when_carried_then_degrades_to_the_lost_framing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStateStore()
    await _released(store, "openai.persistent_workspace")
    carried = await _move(
        monkeypatch,
        store,
        source="openai.persistent_workspace",
        target=MA,
        ma_row_at=None,
        runtimes={},
    )
    assert carried is not None, "a missing runtime must still carry the framing"
    assert carried.continuity.state == "replaced_after_loss", carried.continuity.state


async def test_managed_agents_target_when_found_before_the_create_then_carries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The predecessor is found before the new session's row exists; that row is then
    the newest, so the next new session (a fresh start) carries nothing again."""
    store = MemoryStateStore()
    await _released(store, "openai.persistent_workspace")
    rows: list[SimpleNamespace] = []
    request = await _request(monkeypatch, store, target=MA, rows=rows)
    found = await carry_over.adoption_predecessor(request, store, SLOT)
    rows.append(_row(RELEASED_AT + timedelta(seconds=1), "new-session"))
    assert found is not None and found.provider == "openai", f"nothing found: {found}"
    again = await carry_over.adoption_predecessor(request, store, SLOT)
    assert again is None, "a session started after the move carries nothing again"


class _FilesBackend(_Backend):
    """An old session with one file, which is also the new session's upload target."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.mounted: list[str] = []
        listed = SimpleNamespace(
            size_bytes=3,
            ref=ResourceRef(id="f-old", kind="file", provider="openai", account_scope_id="s"),
            filename="notes.txt",
            media_type="text/plain",
        )
        self.artifacts = SimpleNamespace(
            list=self._list_files, download=lambda *_: b"abc", upload=self._upload
        )
        self.session_admin = SimpleNamespace(add_file=self._add_file)
        self._listed = listed

    async def _list_files(self, *_: object, **__: object) -> object:
        return SimpleNamespace(data=[self._listed])

    async def _upload(self, *_: object, **__: object) -> object:
        ref = ResourceRef(id="f-new", kind="file", provider="anthropic", account_scope_id="s")
        return SimpleNamespace(ref=ref)

    async def _add_file(self, scope: object, session: object, mount: object, **_: object) -> None:
        self.mounted.append(cast(ResourceBinding, mount).target_path or "")


@pytest.mark.parametrize("readable", [True, False])
async def test_openai_to_claude_default_when_created_then_carries_transcript_and_files(
    monkeypatch: pytest.MonkeyPatch, readable: bool
) -> None:
    """Found before the new session exists, the OpenAI session's conversation and file
    reach the new Claude session; unreadable, it starts with the loss framing, whose
    `replaced_after_loss` state is what the adapters tell the person."""
    store = MemoryStateStore()
    await _released(store, "openai.persistent_workspace")
    backend = _FilesBackend()
    rows: list[SimpleNamespace] = []
    request = await _request(
        monkeypatch,
        store,
        target=MA,
        rows=rows,
        runtimes=None if readable else {},
        backend=backend,
    )
    predecessor = await carry_over.adoption_predecessor(request, store, SLOT)
    assert predecessor is not None, "the released OpenAI session is the predecessor"
    rows.append(_row(RELEASED_AT + timedelta(seconds=1), "new-session"))
    carried = await carry_over.carry_into_managed_agents(
        request,
        store=store,
        slot=SLOT,
        predecessor=predecessor,
        session_id="new-session",
        model_id="model-1",
        agent_name="Daimon",
    )
    continuity = carried.continuity
    if readable:
        assert continuity.state == "replaced", continuity.state
        assert "What is the answer?" in continuity.user_prefix, "transcript missing"
        assert backend.mounted == ["/workspace/previous/notes.txt"], (
            f"file not mounted: {backend.mounted}"
        )
    else:
        assert continuity.state == "replaced_after_loss", continuity.state
        assert "was lost" in continuity.user_prefix, "loss framing missing"
        assert backend.mounted == [], "nothing is mounted from an unreadable session"


async def test_bound_slot_when_host_prepares_then_carries_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStateStore()
    await store.put_binding(
        _old_binding("openai.persistent_workspace", 1, {"session": "s"}), expected_generation=0
    )
    carried = await _move(
        monkeypatch, store, source="", target="gemini.inline_reuse", ma_row_at=None
    )
    assert carried is None, "a live binding is the host's to replace, not carry over"


def test_every_move_path_when_scanned_then_calls_the_core_helper() -> None:
    modules = {inspect.getmodule(prepare) for prepare in HOSTS.values()}
    paths = {Path(inspect.getfile(m)) for m in modules if m is not None}
    paths.add(Path(inspect.getfile(preparation)))  # the Managed Agents bind path
    for path in paths:
        tree = ast.parse(path.read_text())
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        helpers = {"carry_on_adoption", "carry_into_managed_agents"}
        assert called & helpers, f"{path.name} does not carry over"
    readers = set(carry_over._READERS)  # pyright: ignore[reportPrivateUsage]
    assert set(HOSTS) <= readers, f"no session reader for {set(HOSTS) - readers}"


def test_claude_default_target_when_slot_resolved_then_uses_the_thread_channel() -> None:
    """Claude default has no revision; its slot must still be found, or nothing carries."""
    tenant = uuid.uuid4()
    admission = SimpleNamespace(backend_revision=None, origin_channel_id="C1")
    channel = carry_over.carry_over_channel(
        cast(Admission, admission), tenant_id=tenant, platform="slack"
    )
    assert channel == channel_ref(tenant, "slack", "C1"), f"wrong channel: {channel}"
    revision = _revision("openai.persistent_workspace", 1)
    admission = SimpleNamespace(backend_revision=revision, origin_channel_id="C1")
    channel = carry_over.carry_over_channel(
        cast(Admission, admission), tenant_id=tenant, platform="slack"
    )
    assert channel == revision.channel, "a revision's own channel wins"


async def test_claude_default_target_when_files_carried_then_mounts_them_on_the_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A move back to Claude mounts the carried files on the session just created."""
    store = MemoryStateStore()
    await _released(store, "openai.persistent_workspace")
    mount = ResourceBinding(
        id="m1",
        kind="artifact",
        target_path="carried/a.txt",
        resource=ResourceRef(id="file-1", kind="file", provider="anthropic", account_scope_id="s"),
    )
    added: list[tuple[str, frozenset[tuple[str, str]]]] = []

    async def carried(*_: object, **kwargs: object) -> CarriedOver:
        assert kwargs["target_artifacts"] is not None, "Claude target must take files"
        return CarriedOver(continuity=cast(ContinuityOutcome, None), mounts=(mount,))

    class _Admin:
        def __init__(self, resources: frozenset[tuple[str, str]]) -> None:
            self.resources = resources

        async def add_file(self, scope: object, session: object, binding: object, **_: object):
            added.append((cast(ResourceBinding, binding).id, self.resources))

    def ma_backend(*_: object, resources: frozenset[tuple[str, str]], **__: object) -> object:
        return SimpleNamespace(artifacts=object(), session_admin=_Admin(resources))

    monkeypatch.setattr(carry_over, "carry_over", carried)
    monkeypatch.setattr(carry_over, "managed_agents", ma_backend)
    monkeypatch.setattr(carry_over, "resource_ref", lambda *_, **__: None)
    request = SimpleNamespace(deps=SimpleNamespace(anthropic=object()), scope=SCOPE)
    result = await carry_over.carry_into_managed_agents(
        cast(ProviderPreparationRequest, request),
        store=store,
        slot=SLOT,
        predecessor=_old_binding("openai.persistent_workspace", 1, {"session": "s"}),
        session_id="new-session",
        model_id="m",
        agent_name="Daimon",
    )
    assert result.mounts == (mount,), f"mounts lost: {result}"
    assert added == [("m1", frozenset({("session", "new-session"), ("file", "file-1")}))], added


async def test_replacement_when_host_cannot_read_old_session_then_uses_its_provider_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old session of another provider or account is read by its own reader, so its
    conversation carries and it can be archived; a readable one is used as is."""
    old = _old_binding("gemini.inline_reuse", 1, {"session": "old-session"})
    read = OldSession(binding=old, ref=_ref(old), backend=_Backend())  # pyright: ignore[reportArgumentType]
    calls: list[str] = []

    async def reader(request: object, store: object, binding: ProviderBinding) -> OldSession:
        calls.append(binding.id)
        return read

    monkeypatch.setattr(carry_over, "_old_session", reader)
    request = cast(ProviderPreparationRequest, SimpleNamespace())
    store = MemoryStateStore()
    assert await carry_over.replaced_session(request, store, old, None) is read, "not read"
    own = OldSession(binding=old, ref=None, backend=None)
    assert await carry_over.replaced_session(request, store, old, own) is own, "own replaced"
    assert calls == ["binding-1"], calls


def test_every_host_replacement_when_scanned_then_falls_back_to_the_provider_reader() -> None:
    """A host that replaces a binding reads an old session it cannot read through its own reader."""
    modules = {inspect.getmodule(prepare) for prepare in HOSTS.values()}
    sources = {Path(inspect.getfile(m)).read_text() for m in modules if m is not None}
    sources.add(Path(inspect.getfile(gemini_prepare)).read_text())
    for source in sources:
        called = {
            node.func.id
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        if "replace_binding" in called:
            assert "replaced_session" in called, "a replacement skips the provider reader"
