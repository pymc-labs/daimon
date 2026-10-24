"""Natural model names over the priced, runnable agent catalog; no authority writes."""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass

from daimon.core.pricing import AGENT_MODEL_PRICING, provider_prices
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import PROVIDERS, ModelRef, Provider
from mux.profiles import (
    MANAGED_AGENTS,
    PROFILES,
    backend_selection,
    runnable_selection,
    selectable_models,
)


def model_name(model: ModelRef) -> str:
    """The name people say: "Claude Opus 5.5", "GPT-6 Astra", "Gemini 4 Argon"."""
    from daimon.core.constants import MODEL_DISPLAY_NAMES

    if model.id in MODEL_DISPLAY_NAMES:
        return "Claude " + MODEL_DISPLAY_NAMES[model.id]
    words: list[str] = []
    for word in model.id.split("-"):
        if word.isdigit() and words and words[-1][-1:].isdigit():
            words[-1] += "." + word  # claude-opus-4-7 -> Opus 4.7
        else:
            words.append(word)
    if words[0] == "gpt" and len(words) > 1:
        words = [f"GPT-{words[1]}", *words[2:]]
    return " ".join(
        word if word.startswith("GPT") or word[:1].isdigit() else word.capitalize()
        for word in words
    )


class AmbiguousModel(ValueError):
    """A name that could mean more than one available model."""


@dataclass(frozen=True)
class AgentModel:
    model: ModelRef
    price: str


def agent_catalog() -> tuple[AgentModel, ...]:
    """Derive availability from runtime selection AND current prices, including B2 additions."""
    entries = [
        AgentModel(
            ModelRef(provider=MANAGED_AGENTS.provider, id=name),
            f"~${rates.input:g} input / ${rates.output:g} output per million tokens",
        )
        for name, rates in AGENT_MODEL_PRICING.items()
        # A "-fast" id is a local preset (model_presets), not a provider model id:
        # only the legacy path expands it, so it is never a catalog/fleet choice.
        if not name.endswith("-fast")
    ]
    for provider in PROVIDERS:
        if provider == MANAGED_AGENTS.provider or not any(
            profile.provider == provider for profile in PROFILES.values()
        ):
            continue
        selection = backend_selection(provider)
        for (_, name), rates in provider_prices(selection.profile).items():
            candidate = selection.model_copy(update={"model": name})
            if runnable_selection(candidate) and name in selectable_models(provider):
                entries.append(
                    AgentModel(
                        ModelRef(provider=provider, id=name),
                        f"~${rates.input} input / ${rates.output} output per million tokens",
                    )
                )
    return tuple(entries)


def _key(value: str) -> str:
    return " ".join(re.findall(r"[a-z]+|\d+", value.casefold()))


def _aliases(model: ModelRef) -> set[str]:
    name = _key(model.id)
    family = " ".join(
        token
        for token in name.split()
        if not token.isdigit() and token not in {"claude", model.provider, "gpt"}
    )
    return {
        name,
        family,
        " ".join(token for token in name.split() if not token.isdigit()),
        name.removeprefix("claude ").removeprefix(model.provider + " ").removeprefix("gpt "),
    }


#: A request for the cheapest Claude by price, not by a family name. "Claude"
#: must be present: bare "cheapest" could mean another provider.
_CHEAPEST_CLAUDE = {"cheapest claude", "cheap claude", "cheapest anthropic", "cheap anthropic"}


def resolve_model(value: str, *, available: Sequence[ModelRef] | None = None) -> ModelRef:
    """Resolve names, preserving ambiguity; never silently choose a different provider."""
    models = (
        tuple(entry.model for entry in agent_catalog()) if available is None else tuple(available)
    )
    key = _key(value)
    if key in _CHEAPEST_CLAUDE:
        # Price, then generation: "cheapest Claude" must be the current Haiku,
        # never the older, pricier 4.5 row the model used to guess (SMOKE J5).
        claude = [
            model
            for model in models
            if model.provider == MANAGED_AGENTS.provider and "fast" not in _key(model.id).split()
        ]
        priced = [
            (AGENT_MODEL_PRICING[model.id], model)
            for model in claude
            if model.id in AGENT_MODEL_PRICING
        ]
        if priced:
            return min(
                priced,
                key=lambda pair: (
                    pair[0].input,
                    pair[0].output,
                    tuple(-int(part) for part in re.findall(r"\d+", pair[1].id)),
                ),
            )[1]
    exact = [model for model in models if key in _aliases(model)]
    if len(exact) == 1:
        return exact[0]
    tokens = set(key.split())
    matches = exact or [
        model for model in models if tokens and tokens <= set(_key(model.id).split())
    ]
    # Family shorthand means the latest catalog version within that one family.
    family = tokens - ({profile.provider for profile in PROFILES.values()} | {"claude", "gpt"})
    if len(matches) > 1 and family in (
        {"opus"},
        {"sonnet"},
        {"haiku"},
        {"flash"},
        {"flash", "lite"},
    ):
        if "lite" not in family:
            matches = [model for model in matches if "lite" not in _key(model.id).split()]
        # Defensive: agent_catalog() excludes the fast preset, but a caller-supplied
        # `available` may carry it; "opus" must never mean that pricier variant.
        matches = [model for model in matches if "fast" not in _key(model.id).split()]
        if matches:
            return max(matches, key=lambda model: tuple(map(int, re.findall(r"\d+", model.id))))
    if not matches:
        # A family alias may name several releases. Keep all of them; a
        # spelling error must never select the last (possibly pricier) row.
        aliases: dict[str, list[ModelRef]] = {}
        for model in models:
            for alias in _aliases(model):
                aliases.setdefault(alias, []).append(model)
        numeric = re.findall(r"\d+", key)
        eligible = sorted(alias for alias in aliases if re.findall(r"\d+", alias) == numeric)
        close = difflib.get_close_matches(key, eligible, n=max(1, len(eligible)), cutoff=0.85)
        matches = list(
            {
                (model.provider, model.id): model for alias in close for model in aliases[alias]
            }.values()
        )
        matches.sort(key=lambda model: (model.provider, model.id))
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise AmbiguousModel(
            f'"{value}" could mean '
            + ", ".join(model_name(model) for model in matches)
            + ". Choose a model by name."
        )
    known = {"astra", "argon", "sol", "gemini 4", "gemini 4 argon"}
    explanation = (
        "isn't available for agents here yet"
        if key in known
        else "isn't a known available agent model"
    )
    raise ValueError(f'"{value}" {explanation}. Ask which models are available for this agent.')


def resolve_models(value: str) -> tuple[ModelRef, ...]:
    """Accept a list or common requests such as '3 workers: sonnet, luna, flash'."""
    value = re.sub(r"^\s*(?:use|compare with)\s+|^\s*\d+\s+workers?\s*:\s*", "", value, flags=re.I)
    parts = re.split(r"\s*,\s*|\s+and\s+", value, flags=re.I)
    return tuple(
        {
            (model.provider, model.id): model
            for part in parts
            if part.strip()
            for model in (resolve_model(part.strip()),)
        }.values()
    )


def _provider_words() -> dict[str, Provider]:
    """A provider word alone ("switch to GPT") means that provider's default model.

    From the model catalog: each provider's id, display name and model family
    ("claude", "gpt", "gemini"), so no provider is written out here.
    """
    from daimon.core.turn.notices import provider_name
    from mux.profiles import PROVIDER_ALIASES

    words: dict[str, Provider] = dict(PROVIDER_ALIASES)
    for entry in agent_catalog():
        provider = entry.model.provider
        for word in (provider, provider_name(provider), entry.model.id.split("-")[0]):
            words[word.lower()] = provider
    return words


def resolve_switch_model(backend: Provider | None, model: str | None) -> str | None:
    """A switch's model as said ("argon", "luna", "GPT") to its id; None means the default.

    The tool and the card that confirms it both resolve here, so the card
    re-previews the very model the tool previewed. Raises `ValueError` with
    words for the person.
    """
    word = model.strip().lower() if model is not None else None
    if word == "default":
        return None
    named = _provider_words().get(word or "")
    if named is not None:
        # No provider means clearing to the default provider.
        meant = backend or DEFAULT_BACKEND
        if named != meant:
            from daimon.core.turn.notices import provider_name

            raise ValueError(
                f"'{model}' is {provider_name(named)}, but this switch names "
                f"{provider_name(meant)}. Nothing changed. Say which one you want."
            )
        return None
    if model is None or backend is None or model in selectable_models(backend):
        return model
    candidates = tuple(entry.model for entry in agent_catalog() if entry.model.provider == backend)
    return resolve_model(model, available=candidates).id
