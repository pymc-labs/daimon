"""One Gemini and one OpenAI SDK client per process, shared by every turn.

A holder lives on `TurnDeps` and the runtime owner closes it at shutdown. Each
provider's driver package is imported only when its client is first needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mux.drivers.gemini import GeminiClients
    from mux.drivers.openai import OpenAIClients


class ProviderClients:
    def __init__(self) -> None:
        self._gemini: GeminiClients | None = None
        self._openai: OpenAIClients | None = None

    @property
    def gemini(self) -> GeminiClients:
        if self._gemini is None:
            from mux.drivers.gemini import GeminiClients

            self._gemini = GeminiClients()
        return self._gemini

    @property
    def openai(self) -> OpenAIClients:
        if self._openai is None:
            from mux.drivers.openai import OpenAIClients

            self._openai = OpenAIClients()
        return self._openai

    async def aclose(self) -> None:
        """Close what was opened; the holder can serve again afterwards."""
        gemini, openai = self._gemini, self._openai
        self._gemini = self._openai = None
        if gemini is not None:
            await gemini.aclose()
        if openai is not None:
            await openai.aclose()
