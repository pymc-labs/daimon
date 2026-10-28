from __future__ import annotations

import base64
import io

import pytest
from PIL import Image


def png():
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def workflow(load_workflow):
    return load_workflow("image-workflow", "image_workflow")


def test_generate_requests_exactly_one_png_and_saves(workflow, fake_api):
    api = fake_api([{"data": [{"b64_json": base64.b64encode(png()).decode()}]}])
    saved = []
    result = workflow.run(
        {"operation": "generate", "provider": "openai", "prompt": "A square"},
        lambda *_: api,
        lambda name: "synthetic",
        lambda data: saved.append(data) or "/mnt/session/outputs/generated.png",
    )
    assert api.calls[0][1] == "/v1/images/generations"
    assert api.calls[0][2]["n"] == 1 and api.calls[0][2]["output_format"] == "png"
    assert saved == [png()] and result["file"].endswith("generated.png")
    assert "synthetic" not in str(result)


def test_edit_uses_local_reference_as_data_url(workflow, tmp_path):
    path = tmp_path / "reference.png"
    path.write_bytes(png())
    provider, base, endpoint, body = workflow.plan(
        {
            "operation": "edit",
            "provider": "openai",
            "prompt": "Make it blue",
            "reference": str(path),
        }
    )
    assert endpoint == "/v1/images/edits" and provider == "openai"
    assert body["images"] == [
        {"image_url": "data:image/png;base64," + base64.b64encode(png()).decode()}
    ]


@pytest.mark.parametrize(
    "image_request",
    [
        {"operation": "edit", "provider": "gemini", "prompt": "x", "reference": "reference.png"},
        {"operation": "generate", "provider": "openai", "prompt": "x", "n": 10},
        {"operation": "generate", "provider": "other", "prompt": "x"},
        {"operation": "generate", "provider": "openai", "prompt": "x", "model": "arbitrary"},
        {
            "operation": "generate",
            "provider": "openai",
            "prompt": "x",
            "output": "../../escape.png",
        },
        {
            "operation": "edit",
            "provider": "openai",
            "prompt": "x",
            "reference": "https://example.com/image.png",
        },
    ],
)
def test_refusals_happen_before_key_or_paid_request(workflow, image_request):
    with pytest.raises(workflow.Failure):
        workflow.run(
            image_request, lambda *_: pytest.fail("network"), lambda *_: pytest.fail("key lookup")
        )


def test_invalid_provider_output_never_published(workflow, fake_api):
    api = fake_api([{"data": [{"b64_json": "not base64!!!"}]}])
    with pytest.raises(workflow.Failure, match="encoding"):
        workflow.run(
            {"operation": "generate", "provider": "openai", "prompt": "x"},
            lambda *_: api,
            lambda *_: "synthetic",
            lambda *_: pytest.fail("saved invalid data"),
        )
    assert len(api.calls) == 1


def test_retired_imagen_refused_without_key_or_request(workflow):
    with pytest.raises(workflow.Failure, match="retired Imagen"):
        workflow.run(
            {"operation": "generate", "provider": "gemini", "prompt": "x"},
            lambda *_: pytest.fail("network"),
            lambda *_: pytest.fail("key lookup"),
        )


def test_output_is_unique_and_rejects_symlink_directory(load_workflow, monkeypatch, tmp_path):
    runtime = load_workflow("image-workflow", "workflow_io")
    output = tmp_path / "outputs"
    monkeypatch.setattr(runtime, "OUTPUT_DIR", output)
    first, second = runtime.save_png(png()), runtime.save_png(png())
    assert first != second
    assert output.joinpath(first.split("/")[-1]).read_bytes() == png()
    link = tmp_path / "link"
    link.symlink_to(output, target_is_directory=True)
    monkeypatch.setattr(runtime, "OUTPUT_DIR", link)
    with pytest.raises(runtime.Failure, match="symlink"):
        runtime.save_png(png())


def test_corrupt_image_never_creates_output(load_workflow, monkeypatch, tmp_path):
    runtime = load_workflow("image-workflow", "workflow_io")
    output = tmp_path / "outputs"
    monkeypatch.setattr(runtime, "OUTPUT_DIR", output)
    with pytest.raises(runtime.Failure, match="invalid"):
        runtime.save_png(b"corrupt image")
    assert not output.exists()


def test_reference_symlinks_and_oversized_inputs_refused(workflow, tmp_path):
    path = tmp_path / "reference.png"
    path.write_bytes(png())
    link = tmp_path / "link.png"
    link.symlink_to(path)
    with pytest.raises(workflow.Failure, match="symlink"):
        workflow.reference(str(link))
    path.write_bytes(b"x" * (workflow.MAX_REFERENCE + 1))
    with pytest.raises(workflow.Failure, match="10 MB"):
        workflow.reference(str(path))


def test_named_mounted_key_is_literal_not_executed(load_workflow, monkeypatch, tmp_path):
    runtime = load_workflow("image-workflow", "workflow_io")
    path = tmp_path / "synthetic-mount.txt"
    path.write_text("OTHER_KEY=ignore\nOPENAI_API_KEY='literal$(touch never)`cmd`#value'\n")
    monkeypatch.setattr(runtime, "KEY_FILE", path)
    assert runtime.key("OPENAI_API_KEY") == "literal$(touch never)`cmd`#value"
    with pytest.raises(runtime.Failure, match="Missing GEMINI_API_KEY"):
        runtime.key("GEMINI_API_KEY")
