"""Pure Gemini usage_metadata -> plain-int mapping for media-tool billing.

Adapter-side because `daimon.core` cannot import `google-genai`. The
three media services map their raw `types.GenerateContentResponseUsageMetadata`
into this plain-int shape; `tools/media.py` passes the fields straight to
`record_media_usage`.
"""

from __future__ import annotations

from dataclasses import dataclass

from google.genai import types


@dataclass(frozen=True)
class MediaUsage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int

    def __add__(self, other: MediaUsage) -> MediaUsage:
        return MediaUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
        )


EMPTY_MEDIA_USAGE = MediaUsage(input_tokens=0, output_tokens=0, cache_read_input_tokens=0)


def from_metadata(meta: types.GenerateContentResponseUsageMetadata | None) -> MediaUsage:
    """Map a (possibly None/partial) usage_metadata into plain, non-None ints.

    Every field on `GenerateContentResponseUsageMetadata` is `Optional[int]`,
    and the response's `usage_metadata` itself can be None — coalesce every
    field with `or 0` rather than let a `None + int` TypeError escape.

    Thinking tokens fold into `output_tokens` (candidates + thoughts) since
    Gemini bills them at the output rate.
    """
    if meta is None:
        return EMPTY_MEDIA_USAGE
    return MediaUsage(
        input_tokens=meta.prompt_token_count or 0,
        output_tokens=(meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0),
        cache_read_input_tokens=meta.cached_content_token_count or 0,
    )


def sum_media_usage(usages: list[MediaUsage]) -> MediaUsage:
    """Aggregate a list of per-call MediaUsage into one invocation-level total."""
    total = EMPTY_MEDIA_USAGE
    for usage in usages:
        total = total + usage
    return total
