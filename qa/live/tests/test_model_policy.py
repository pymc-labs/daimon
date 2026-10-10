"""Offline model admission and fallback tests; no provider calls."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from qa.live.config import Config, Pricing
from qa.live.models import BackendName, ModelPolicy


@pytest.mark.parametrize(
    "backend,model",
    [("anthropic", "claude-haiku-5-5"), ("openai", "gpt-6-luna"), ("gemini", "gemini-3.8-flash")],
)
def test_exact_primary_and_unknown_model_refusal(backend: BackendName, model: str) -> None:
    policy = ModelPolicy()
    assert policy.backends[backend].primary == model
    assert policy.accepts(backend, model, "prod")
    for refused in ("", "unknown", "claude-sonnet-4-5", "claude-opus-4-6"):
        assert not policy.accepts(backend, refused, "prod")


def test_temporary_staging_override_never_admits_old_haiku_to_prod() -> None:
    policy = ModelPolicy()
    for model in ("claude-haiku-4-5", "claude-haiku-4-5-20251001"):
        assert policy.accepts("anthropic", model, "staging")
        assert not policy.accepts("anthropic", model, "prod")
    assert policy.accepts("anthropic", "claude-haiku-5-5", "staging")
    assert policy.backends["anthropic"].primary == "claude-haiku-5-5"


@pytest.mark.parametrize("status", [400, 401, 404, 429, 500, 502, 504])
def test_gemini_fallback_refuses_non_503_errors(status: int) -> None:
    gemini = ModelPolicy().backends["gemini"]
    assert gemini.next_model(gemini.primary, status) is None


def test_gemini_503_fallback_order_and_exhaustion() -> None:
    policy = ModelPolicy()
    gemini = policy.backends["gemini"]
    assert gemini.next_model(gemini.primary, 503) == "gemini-flash-latest"
    assert gemini.next_model("gemini-flash-latest", 503) == "gemini-3.5-flash-lite"
    assert gemini.next_model("gemini-3.5-flash-lite", 503) is None
    assert gemini.next_model("unknown", 503) is None
    # A fallback name without HTTP 503 provenance never passes a deployment pin.
    assert not policy.accepts("gemini", "gemini-flash-latest", "prod")


def test_config_rejects_changed_missing_and_extra_backend_maps(pricing: Pricing) -> None:
    config = Config(pricing=pricing)
    payload = config.model_dump(mode="json")
    for backend in ("anthropic", "openai", "gemini"):
        bad = config.model_dump(mode="json")
        bad["models"]["backends"][backend]["primary"] = "expensive-model"
        with pytest.raises(ValidationError):
            Config.model_validate_json(json.dumps(bad))
    del payload["models"]["backends"]["gemini"]
    with pytest.raises(ValidationError):
        Config.model_validate_json(json.dumps(payload))
    assert Config.model_validate_json(config.model_dump_json()).models == config.models
    assert Config.model_validate(config.model_dump(mode="json")).models == config.models


def test_mutated_policy_refuses_before_judge_or_model_admission(pricing: Pricing) -> None:
    from qa.live.judge import HaikuJudge

    policy = ModelPolicy()
    policy.backends["anthropic"] = policy.backends["openai"]
    with pytest.raises(ValueError, match="root-approved"):
        HaikuJudge(pricing, go=True, models=policy)
    with pytest.raises(ValueError, match="root-approved"):
        policy.accepts("anthropic", "gpt-6-luna", "prod")


def test_scenario_b_prod_canary_remains_haiku_only(pricing: Pricing) -> None:
    from qa.live.config import Target

    with pytest.raises(ValidationError, match="Claude Haiku"):
        Config(
            pricing=pricing,
            prod=Target(
                backend="openai",
                enabled=True,
                guild_id="745261709622771773",
                guild_allowlist=["745261709622771773"],
            ),
        )


@pytest.mark.parametrize(
    "model", ["claude-haiku-5-5", "claude-haiku-5-5-20261001", "claude-haiku-5-5-20261010"]
)
def test_primary_and_exact_dated_snapshots_pass_both_environments(model: str) -> None:
    policy = ModelPolicy()
    for env in ("staging", "prod"):
        assert policy.accepts("anthropic", model, env)
    assert policy.policy("anthropic").matches_primary(model)


@pytest.mark.parametrize(
    "model",
    [
        "claude-haiku-5-5-2026101",
        "claude-haiku-5-5-202610010",
        "claude-haiku-5-5-20261301",
        "claude-haiku-5-5-20260230",
        "claude-haiku-5-5-20261001-extra",
        "claude-haiku-5-5-latest",
        "claude-sonnet-5-5-20261001",
        "claude-haiku-5-5-２０２６１００１",
        "claude-haiku-5-5-20261001\n",
    ],
)
def test_non_exact_and_invalid_date_snapshots_refuse(model: str) -> None:
    policy = ModelPolicy()
    assert not policy.accepts("anthropic", model, "staging")
    assert not policy.accepts("anthropic", model, "prod")
    assert not policy.policy("anthropic").matches_primary(model)


@pytest.mark.parametrize("missing", [("anthropic",), ("anthropic", "openai", "gemini")])
def test_older_operator_configs_inherit_approved_snapshot_defaults(
    missing: tuple[str, ...], pricing: Pricing
) -> None:
    config = Config(pricing=pricing)
    legacy = config.model_dump(mode="json")
    for backend in missing:
        del legacy["models"]["backends"][backend]["dated_snapshots"]
    original = json.dumps(legacy, sort_keys=True)
    for loaded in (Config.model_validate_json(json.dumps(legacy)), Config.model_validate(legacy)):
        assert loaded.models == config.models
        assert loaded.models.accepts("anthropic", "claude-haiku-5-5-20261001", "prod")
    assert json.dumps(legacy, sort_keys=True) == original


@pytest.mark.parametrize(
    "backend,enabled", [("anthropic", False), ("openai", True), ("gemini", True)]
)
def test_explicit_conflicting_snapshot_config_is_still_refused(
    backend: str, enabled: bool, pricing: Pricing
) -> None:
    config = Config(pricing=pricing).model_dump(mode="json")
    config["models"]["backends"][backend]["dated_snapshots"] = enabled
    with pytest.raises(ValidationError, match="root-approved"):
        Config.model_validate_json(json.dumps(config))
