from collections.abc import Iterator, Mapping

class ValidationError(Exception):
    message: str
    json_path: str

class FormatChecker:
    def __init__(self) -> None: ...

class Draft202012Validator:
    def __init__(
        self, schema: Mapping[str, object], *, format_checker: FormatChecker | None = None
    ) -> None: ...
    def iter_errors(self, instance: object) -> Iterator[ValidationError]: ...
