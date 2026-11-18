"""Backend denials identify the failed gate without exposing private diagnostics."""

import json

import pytest
from daimon.core.channel_backend import AgentBackendRefused, BackendUnsupported, check_backend
from daimon.core.observability import redact_log_event
from daimon.core.scope import ResolvedConfig
from daimon.core.turn.admission import Admission, _log_backend_unsupported, _require_backend_origin
from daimon.core.turn.errors import AdmissionDenied
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.contracts.config import (
    BackendConfig,
    CapabilityRequirement,
    ConfigRevision,
    ResolvedBackend,
    resolve_default,
)
from mux.errors import InvalidConfig, UnsupportedCapability
from mux.profiles import backend_selection
from structlog.testing import capture_logs

from .test_provider_dispatch import REVISION, TENANT


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_disabled_agent_origin_logs_the_gate_without_changing_the_denial(provider):
    with capture_logs() as logs, pytest.raises(AdmissionDenied) as refused:
        _require_backend_origin(backend_selection(provider), "agent", agent_backends=False)
    assert refused.value.reason == "backend_unsupported"
    assert logs == [
        {
            "event": "turn.backend_unsupported",
            "stage": "backend_origin",
            "causes": [],
            "log_level": "info",
        }
    ]


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
def test_supported_agent_origin_adds_no_refusal_log(provider):
    with capture_logs() as logs:
        _require_backend_origin(backend_selection(provider), "agent", agent_backends=True)
    assert logs == []


def test_missing_admission_origin_logs_before_raising():
    with capture_logs() as logs, pytest.raises(AdmissionDenied, match="backend_unsupported"):
        Admission(
            account_id=TENANT,
            agent=ma_agent(),
            environment=ma_environment(),
            config=ResolvedConfig(agent_name="agent", environment_name="default"),
            backend_revision=REVISION,
        )
    assert logs[0]["stage"] == "admission_origin"


def test_workspace_policy_refusal_has_a_structured_reason():
    with capture_logs() as logs, pytest.raises(BackendUnsupported):
        check_backend(REVISION, enabled_backends=("anthropic",))
    assert logs == [
        {
            "event": "turn.backend_unsupported",
            "stage": "workspace_policy",
            "reason": "backend_not_enabled",
            "causes": [],
            "channel_id": REVISION.channel.channel_id,
            "profile": REVISION.profile,
            "log_level": "info",
        }
    ]


def test_nested_private_errors_log_only_closed_reason_and_capability_labels():
    private = "credential-PRIVATE-value source instructions https://private.invalid"
    native = RuntimeError(private)
    capability = UnsupportedCapability(("source_definition", private), private)
    capability.__context__ = native
    owner = AgentBackendRefused("source_unavailable")
    owner.__cause__ = capability
    denied = AdmissionDenied(reason="backend_unsupported")
    denied.__cause__ = owner
    # Even an unexpected provider reason is not safe merely because it is a string.
    unknown = AgentBackendRefused(private)
    unknown.__cause__ = denied
    with capture_logs() as logs:
        _log_backend_unsupported("preparation_counterpart", unknown)
    assert logs[0]["causes"] == [
        {"type": "AgentBackendRefused", "reason": "redacted"},
        {"type": "AdmissionDenied", "reason": "backend_unsupported"},
        {"type": "AgentBackendRefused", "reason": "source_unavailable"},
        {"type": "UnsupportedCapability", "missing": ["redacted", "source_definition"]},
        {"type": "RuntimeError"},
    ]
    assert private not in json.dumps(logs)


def test_a_refusal_detail_is_logged_only_from_the_closed_set():
    gap = AgentBackendRefused("source_unavailable", detail="source")
    gap.__cause__ = AgentBackendRefused("source_unavailable", detail="private-PRIVATE")
    with capture_logs() as logs:
        _log_backend_unsupported("preparation_counterpart", gap)
    assert logs[0]["causes"] == [
        {"type": "AgentBackendRefused", "reason": "source_unavailable", "detail": "source"},
        {"type": "AgentBackendRefused", "reason": "source_unavailable"},
    ], "only a detail from the closed set is logged"


def test_capture_sentences_include_its_nested_helpers():
    from daimon.core.turn.runtime_manifest import _OWN_CODE

    assert {"resolve_latest", "verify_skill"} <= {code.co_name for code in _OWN_CODE}


def test_cyclic_exception_context_cannot_block_a_denial_log():
    error = RuntimeError("private")
    error.__context__ = error
    with capture_logs() as logs:
        _log_backend_unsupported("preparation_counterpart", error)
    assert logs[0]["causes"] == [{"type": "RuntimeError"}]


@pytest.mark.parametrize("enabled", [None, ("anthropic",)])
def test_unregistered_profile_is_redacted_before_and_after_production_scrubbing(enabled):
    private = "PRIVATE_DIAGNOSTIC_VALUE"
    revision = ConfigRevision.create(
        REVISION.channel,
        1,
        ResolvedBackend(backend="openai", profile="openai." + private, model="gpt-6-luna"),
    )
    with capture_logs() as logs, pytest.raises(BackendUnsupported) as refused:
        check_backend(revision, enabled_backends=enabled)
    assert len(logs) == 1
    assert logs[0]["profile"] == "redacted"
    assert logs[0]["stage"] == ("workspace_policy" if enabled else "backend_configuration")
    assert private not in json.dumps(logs)
    assert private not in json.dumps([redact_log_event(None, "info", row) for row in logs])
    # Diagnostic redaction does not rewrite the original exception or its chain.
    if enabled:
        assert refused.value.__cause__ is None
        assert str(refused.value) == "That model provider isn't turned on here. Pick another."
    else:
        assert isinstance(refused.value.__cause__, InvalidConfig)
        assert private in str(refused.value.__cause__)
        assert str(refused.value) == str(refused.value.__cause__)


def test_private_exception_class_is_not_named_and_the_original_chain_is_preserved():
    private = "PRIVATE_EXCEPTION_CLASS"
    error = type(private, (RuntimeError,), {})("private message")
    parent = AdmissionDenied(reason="backend_unsupported")
    parent.__context__ = error
    with capture_logs() as logs:
        _log_backend_unsupported("preparation_counterpart", parent)
    # Unlisted, and its class (made at run time, here by the test) is not named.
    assert logs[0]["causes"] == [
        {"type": "AdmissionDenied", "reason": "backend_unsupported"},
        {"type": "unlisted"},
    ]
    assert private not in json.dumps(logs)
    assert parent.__context__ is error


@pytest.mark.parametrize(
    ("config", "stage", "cause_type"),
    [
        (
            BackendConfig(
                backend="openai", profile="openai.conversation_only", model="unsupported-model"
            ),
            "backend_runnable",
            None,
        ),
        (
            BackendConfig(
                backend="openai",
                profile="openai.conversation_only",
                model="gpt-6-luna",
                requires={"thread_workspace_persistence": CapabilityRequirement(level="required")},
            ),
            "backend_capabilities",
            UnsupportedCapability,
        ),
    ],
)
def test_nonrunnable_and_missing_capabilities_have_distinct_stages_and_unchanged_causes(
    config,
    stage,
    cause_type,
):
    revision = ConfigRevision.create(REVISION.channel, 1, resolve_default(config))
    with capture_logs() as logs, pytest.raises(BackendUnsupported) as refused:
        check_backend(revision)
    assert len(logs) == 1 and logs[0]["stage"] == stage
    assert "error" not in logs[0]
    if cause_type is None:
        assert refused.value.__cause__ is None
        assert logs[0]["reason"] == "profile_not_runnable"
    else:
        assert isinstance(refused.value.__cause__, cause_type)
        assert logs[0]["causes"] == [
            {"type": "UnsupportedCapability", "missing": ["thread_workspace_persistence"]},
        ]


def test_private_stage_and_reason_cannot_escape_the_shared_diagnostic_owner():
    with capture_logs() as logs:
        _log_backend_unsupported("PRIVATE_STAGE", reason="PRIVATE_REASON")
    assert logs[0]["stage"] == logs[0]["reason"] == "redacted"


@pytest.mark.parametrize(
    "reason", ["balance_depleted", "allowance_exhausted", "backend_unsupported"]
)
def test_shared_refusal_labels_retain_only_typed_admission_codes(reason):
    from daimon.core.channel_backend import backend_refusal_causes

    error = AdmissionDenied(reason=reason)
    assert backend_refusal_causes(error) == [{"type": "AdmissionDenied", "reason": reason}]
    assert error.reason == reason


@pytest.mark.parametrize(
    ("sqlstate", "busy"),
    [
        ("55P03", True),  # lock not available
        ("40001", True),  # serialization failure
        ("40P01", True),  # deadlock
        ("57014", True),  # statement timeout
        ("08006", True),  # connection failure
        ("23505", False),  # a unique violation is a bug, not "busy"
    ],
)
def test_a_database_failure_is_named_by_class_and_sqlstate_and_busy_only_when_transient(
    sqlstate, busy
):
    from daimon.core.channel_backend import backend_refusal_causes, transient_failure
    from sqlalchemy.exc import DBAPIError

    private = "PRIVATE row value"
    driver = type("DriverError", (Exception,), {"sqlstate": sqlstate})(private)
    error = DBAPIError(f"UPDATE ... {private}", None, driver)
    (cause,) = backend_refusal_causes(error)
    assert cause == {"type": "unlisted", "class": "sqlalchemy.exc.DBAPIError", "sqlstate": sqlstate}
    assert transient_failure(error) is busy
    assert private not in json.dumps(cause)


class _Status(Exception):
    """An SDK status error, as its `status_code` shows it."""

    def __init__(self, status_code: int) -> None:
        super().__init__("PRIVATE body")
        self.status_code = status_code


@pytest.mark.parametrize(
    ("cause", "busy"),
    [
        (_Status(503), True),
        (_Status(429), True),
        (TimeoutError(), True),
        (ConnectionResetError(), True),
        (_Status(401), False),  # a bad key: retrying never fixes it
        (_Status(403), False),
        (_Status(404), False),  # a missing resource
        (None, False),
    ],
)
def test_an_upstream_failure_is_busy_only_when_a_retry_can_clear_it(cause, busy):
    """#1558 review: a provider 401/403/404 must give the reference, not "busy, try
    again" forever; a 5xx, 429, timeout or lost connection is busy."""
    from daimon.core.channel_backend import transient_failure
    from daimon.core.errors import TurnError

    as_attribute = TurnError(kind="upstream", message="upstream", cause=cause)
    assert transient_failure(as_attribute) is busy
    raised = TurnError(kind="upstream", message="upstream")
    raised.__cause__ = cause
    assert transient_failure(raised) is busy


def _raised_names() -> set[str]:
    """Every literal name our code raises: `.unsupported("x")`, `UnsupportedCapability(("x",)`."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    names: set[str] = set()
    for base in ("packages/mux/mux", "packages/core/daimon"):
        for path in (root / base).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func, arg = node.func, node.args[0]
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if called in ("unsupported", "_unsupported") and isinstance(arg, ast.Constant):
                    names.add(arg.value)
                elif called == "UnsupportedCapability" and isinstance(arg, ast.Tuple):
                    names |= {e.value for e in arg.elts if isinstance(e, ast.Constant)}
    return {n for n in names if isinstance(n, str)}


def test_every_name_our_code_raises_is_logged_by_name():
    from daimon.core.channel_backend import _raised_capability

    names = _raised_names()
    assert len(names) > 100  # the scan finds the drivers' names
    assert sorted(n for n in names if not _raised_capability(n)) == []


def test_every_logged_provider_failure_handler_logs_its_causes() -> None:
    """A provider error handler that logs must log why: `backend_refusal_causes`
    carries the redacted native code, param and message (incident: a skill
    archive mismatch logged only as `invalid_request (400)`)."""
    import ast
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[4] / "packages"
    provider = {"ProviderError", "MuxError", "APIStatusError", "APIError", "AnthropicError"}
    # Wired when the in-flight session_skills.py change merges.
    pending = {"session_skills.py"}
    missing: list[str] = []
    for path in [*root.glob("core/daimon/**/*.py"), *root.glob("adapters/*/daimon/**/*.py")]:
        if path.name in pending:
            continue
        for handler in ast.walk(ast.parse(path.read_text())):
            if not isinstance(handler, ast.ExceptHandler) or handler.type is None:
                continue
            if not provider & set(re.findall(r"\w+", ast.unparse(handler.type))):
                continue
            body = "\n".join(ast.unparse(statement) for statement in handler.body)
            logs = re.search(r"log\w*(\(\))?\.(debug|info|warning|error|exception)\(", body)
            if logs and "backend_refusal_causes(" not in body:
                missing.append(f"{path.relative_to(root)}:{handler.lineno}")
    assert not missing, f"provider failure logs without causes: {missing}"
