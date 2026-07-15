from typing import IO, ClassVar

class AudioSegment:
    converter: ClassVar[str]
    def __init__(
        self,
        data: bytes | None = ...,
        *,
        sample_width: int = ...,
        frame_rate: int = ...,
        channels: int = ...,
    ) -> None: ...
    def export(
        self,
        out_f: IO[bytes] | str,
        format: str = ...,
        bitrate: str = ...,
    ) -> IO[bytes]: ...
