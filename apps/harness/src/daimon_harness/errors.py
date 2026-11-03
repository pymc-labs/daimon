"""Safe error categories carried in the wire journal."""

from pydantic import JsonValue


class HarnessError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class ToolErrorResult(Exception):
    """A tool finished with an error result whose content the model should read."""

    def __init__(self, output: JsonValue) -> None:
        super().__init__("tool returned an error result")
        self.output = output
