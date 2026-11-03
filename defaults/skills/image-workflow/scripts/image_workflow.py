"""Bounded provider image generation/editing into reply attachment outputs."""

from __future__ import annotations

import base64
import os
from pathlib import Path

from workflow_io import (
    MAX_BYTES,
    Api,
    Failure,
    fields,
    image_bytes,
    key,
    main,
    require,
    save_png,
    text,
)

OPENAI_MODELS = {"gpt-image-2", "gpt-image-1.5"}
MAX_REFERENCE = 10 * 1024 * 1024


def reference(path):
    require(
        isinstance(path, str) and not path.startswith(("https:", "http:")),
        "Reference must be a local image path",
    )
    path = Path(path).absolute()
    require(
        path.resolve() == path and not path.name.startswith("."),
        "Reference cannot be a hidden file or symlink",
    )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        import stat

        require(stat.S_ISREG(os.fstat(fd).st_mode), "Reference must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_REFERENCE + 1)
        require(len(data) <= MAX_REFERENCE, "Reference exceeds 10 MB")
        image_bytes(data)
    finally:
        os.close(fd)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8"):
        mime = "image/jpeg"
    else:
        mime = "image/webp"
    return "data:" + mime + ";base64," + base64.b64encode(data).decode()


def plan(request):
    fields(
        request,
        ("operation", "provider", "model", "prompt", "reference", "size", "quality"),
        ("operation", "provider", "prompt"),
    )
    operation, provider = request["operation"], request["provider"]
    require(operation in ("generate", "edit"), "Only image generate/edit are supported")
    prompt = text(request["prompt"])
    require(len(prompt) <= 32000, "Prompt exceeds 32000 characters")
    if provider == "openai":
        model = request.get("model", "gpt-image-2")
        require(model in OPENAI_MODELS, "Unsupported OpenAI image model")
        size, quality = request.get("size", "1024x1024"), request.get("quality", "medium")
        require(size in ("1024x1024", "1024x1536", "1536x1024"), "Unsupported bounded image size")
        require(quality in ("low", "medium", "high"), "Unsupported quality")
        body = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": quality,
            "output_format": "png",
        }
        path = "/v1/images/generations"
        if operation == "edit":
            body["images"] = [{"image_url": reference(request.get("reference"))}]
            path = "/v1/images/edits"
        else:
            require("reference" not in request, "Use edit for reference images")
        return provider, "https://api.openai.com", path, body
    raise Failure("Only OpenAI is supported; Google has retired Imagen in the Gemini API")


def run(request, api_factory=Api, read_key=key, save=save_png):
    provider, base, path, body = plan(request)  # Validate before key lookup or paid request.
    secret = read_key("OPENAI_API_KEY")
    api = api_factory(base, "Bearer " + secret)
    response = api.request("POST", path, body)
    require(isinstance(response, dict), "Invalid image provider response")
    images = response.get("data")
    require(isinstance(images, list) and len(images) == 1, "Expected exactly one provider image")
    encoded = images[0].get("b64_json")
    require(
        isinstance(encoded, str) and len(encoded) <= MAX_BYTES, "Missing or oversized base64 image"
    )
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise Failure(
            "Provider returned invalid image encoding; request may have been charged"
        ) from None
    # No file is published until decoding/validation succeeds. No provider URL downloads.
    try:
        filename = save(data)
    except (Failure, OSError, ImportError):
        raise Failure(
            "Image request completed but output could not be validated/saved; "
            "do not automatically repeat the paid request"
        ) from None
    return {"provider": provider, "model": body["model"], "file": filename}


if __name__ == "__main__":
    raise SystemExit(main(run))
