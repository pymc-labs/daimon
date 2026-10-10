"""Write-only credential string with redacted SDK debug representation."""


class RedactedCredential(str):
    """Keep native JSON intact while redacting the pinned SDK's options repr.

    The SDK logs its Python request-options mapping at DEBUG. JSON encoding
    preserves this string's actual value; repr of that mapping must not.
    The real SDK log and wire behavior are tested together.
    """

    def __repr__(self) -> str:
        return repr("[redacted]")
