"""Safe error categories carried in the wire journal."""


class HarnessError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
