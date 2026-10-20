"""Request-scoped refusal of auxiliary Anthropic operations during self-hosted turns."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from daimon.core.errors import DaimonError

_disabled: ContextVar[bool] = ContextVar("anthropic_disabled", default=False)


class AnthropicDisabled(DaimonError):
    """An optional provider-specific operation is unavailable in this turn."""


def require_anthropic_allowed() -> None:
    if _disabled.get():
        raise AnthropicDisabled("This operation is unavailable during a self-hosted turn.")


@contextmanager
def without_anthropic() -> Iterator[None]:
    """Inherited by child tasks; restored independently for each concurrent request."""
    token = _disabled.set(True)
    try:
        yield
    finally:
        _disabled.reset(token)
