import asyncio
from io import BytesIO

import pytest
from PIL import Image

from conftest import install_sparse
from sd_workbench.errors import WorkbenchError
from sd_workbench.security import bounded_upload


def test_all_manifest_urls_and_sizes(registry):
    statuses = registry.all_status()
    assert len(statuses) == 3
    assert sum(len(model["components"]) for model in statuses) == 11
    for model in statuses:
        for component in model["components"]:
            assert component["url"].startswith("https://huggingface.co/")
            assert component["bytes"] > 0
            assert len(component["sha256"]) == 64


def test_qwen_projector_required_only_for_editing(app, registry):
    install_sparse(registry, "qwen", editing=False)
    assert app.registry.verify("qwen")
    with pytest.raises(WorkbenchError, match="mmproj"):
        app.registry.verify("qwen", editing=True)


def test_model_remove_blocked_by_active_job(app, registry):
    install_sparse(registry, "zimage")
    app.submit_generation({"prompt": "Active", "model": "zimage"})
    with pytest.raises(WorkbenchError, match="queued or running"):
        app.remove_model("zimage")


def test_streaming_upload_limit_without_content_length():
    class StreamRequest:
        headers = {}
        async def stream(self):
            yield b"12345"
            yield b"67890"
    with pytest.raises(WorkbenchError) as error:
        asyncio.run(bounded_upload(StreamRequest(), 8))
    assert error.value.status == 413


def test_streaming_upload_rejects_false_large_declaration():
    class StreamRequest:
        headers = {"Content-Length": "100"}
        async def stream(self):
            raise AssertionError("declared oversized body must not be read")
    with pytest.raises(WorkbenchError) as error:
        asyncio.run(bounded_upload(StreamRequest(), 8))
    assert error.value.status == 413


def test_upload_validates_decoded_content(app):
    with pytest.raises(WorkbenchError):
        app.media.upload(b"not an image")
    buffer = BytesIO()
    Image.new("RGB", (64, 64), "blue").save(buffer, "JPEG")
    record = app.media.upload(buffer.getvalue(), "../../unsafe.jpg")
    assert record["origin"] == "unsafe.jpg"
    assert app.media.input_path(record["id"]).parent == app.paths.inputs
