"""Explicit QA product settings; never change product defaults or process environment."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from importlib import import_module
from typing import Final, Literal, cast

from pydantic import BaseModel, TypeAdapter

type ProductProfileName = Literal["default", "opt-in-product"]
type ProductService = Literal["core", "notebook", "report"]
OPT_IN_PASS: Final = "PASS under opt-in profile"
_FLAGS = (
    ("core", "mcp.require_routine_destination", "daimon.core.config", "McpSettings"),
    ("core", "discord.credential_form_expiry", "daimon.core.config", "DiscordSettings"),
    ("core", "discord.writers_none_notice", "daimon.core.config", "DiscordSettings"),
    ("core", "discord.tenant_cap_waiting_card", "daimon.core.config", "DiscordSettings"),
    ("core", "turn.native_footer_cost", "daimon.core.config", "TurnSettings"),
)


class ProductProfileUnavailable(ValueError):
    """A named product flag or explicit fixture binding is absent; no PASS is available."""

    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__("QA product profile unavailable: " + ", ".join(missing))


def _updated[T: BaseModel](settings: T, updates: dict[str, object]) -> T:
    # Validate each declared update without reconstructing BaseSettings: its
    # constructor would consult ambient environment/.env even with explicit inputs.
    validated: dict[str, object] = {}
    for name, value in updates.items():
        info = type(settings).model_fields.get(name)
        if info is None or info.annotation is None:
            raise ValueError("unknown QA product setting")
        validated[name] = TypeAdapter[object](info.annotation).validate_python(value, strict=True)
    return settings.model_copy(update=validated)


@dataclass(frozen=True)
class QAProductProfile:
    name: ProductProfileName

    def __post_init__(self) -> None:
        if self.name not in ("default", "opt-in-product"):
            raise ValueError("unknown QA product profile")

    @property
    def flags(self) -> dict[str, bool]:
        """Exactly the five granted switches; no table/tools/backend overrides."""
        return {
            f"{service}.{field}": self.name == "opt-in-product" for service, field, *_ in _FLAGS
        }

    def require_available(self) -> None:
        """Inspect declarations only; no settings construction, environment or key reads."""
        if self.name == "default":
            return
        missing: list[str] = []
        for service, field, module, class_name in _FLAGS:
            try:
                model = cast(type[BaseModel], getattr(import_module(module), class_name))
            except (ImportError, AttributeError):
                missing.append(f"{service}.{field}")
                continue
            info = model.model_fields.get(field.rsplit(".", 1)[-1])
            if info is None or info.annotation is not bool or info.default is not False:
                missing.append(f"{service}.{field}")
        if missing:
            raise ProductProfileUnavailable(tuple(missing))

    def apply[T: BaseModel](self, settings: T, *, service: ProductService) -> T:
        """Return validated copies for an explicit QA factory, preserving all other values."""
        if service not in ("core", "notebook", "report"):
            raise ValueError("unknown QA product service")
        enabled = self.name == "opt-in-product"
        updates: dict[str, object] = {}
        applied: set[str] = set()
        for owner, path, *_ in _FLAGS:
            if owner != service:
                continue
            parent, _, leaf = path.rpartition(".")
            child = getattr(settings, parent, None) if parent else settings
            if not isinstance(child, BaseModel) or leaf not in type(child).model_fields:
                if enabled:
                    raise ProductProfileUnavailable((f"{service}.{path}",))
                continue  # An older default profile retains its exact settings.
            if type(child).model_fields[leaf].annotation is not bool:
                raise ProductProfileUnavailable((f"{service}.{path}",))
            if parent:
                previous = updates.get(parent, child)
                if not isinstance(previous, BaseModel):
                    raise ValueError("invalid QA product settings parent")
                updates[parent] = _updated(previous, {leaf: enabled})
            else:
                updates[leaf] = enabled
            applied.add(f"{service}.{path}")
        result = _updated(settings, updates) if updates else settings
        receipt = _APPLIED.get()
        if receipt is not None and current_product_profile() == self:
            receipt.update(applied)
        return result


DEFAULT_PRODUCT_PROFILE = QAProductProfile("default")
OPT_IN_PRODUCT_PROFILE = QAProductProfile("opt-in-product")
_PROFILE: ContextVar[QAProductProfile] = ContextVar(
    "qa_product_profile", default=DEFAULT_PRODUCT_PROFILE
)
_APPLIED: ContextVar[set[str] | None] = ContextVar("qa_product_flags_applied", default=None)


def current_product_profile() -> QAProductProfile:
    return _PROFILE.get()


def applied_product_flags() -> frozenset[str]:
    """Configuration receipt only; it grants no source outcome or execution authority."""
    return frozenset(_APPLIED.get() or ())


@contextmanager
def use_product_profile(profile: QAProductProfile) -> Iterator[None]:
    """Propagate an explicit selection to async QA children; restore it even on refusal."""
    profile.require_available()
    token = _PROFILE.set(profile)
    applied_token = _APPLIED.set(set())
    try:
        yield
    finally:
        _APPLIED.reset(applied_token)
        _PROFILE.reset(token)
