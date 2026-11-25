import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from sd_workbench.downloader import DownloadManager
from sd_workbench.errors import WorkbenchError
from sd_workbench.manifest import ModelRegistry


class RangeHandler(BaseHTTPRequestHandler):
    payload = b"tiny-model-payload"

    def do_GET(self):
        range_header = self.headers.get("Range")
        if range_header:
            offset = int(range_header.removeprefix("bytes=").removesuffix("-"))
            content = self.payload[offset:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {offset}-{len(self.payload) - 1}/{len(self.payload)}")
        else:
            content = self.payload
            self.send_response(200)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *args):
        pass


@pytest.fixture
def tiny_registry(tmp_path):
    payload = RangeHandler.payload
    manifest = tmp_path / "models.json"
    document = {
        "schema_version": 1,
        "models": {
            "tiny": {
                "name": "Tiny", "label": "tiny", "tasks": ["txt2img"], "license": {},
                "components": {
                    "diffusion": {
                        "role": "diffusion", "repository": "example/tiny",
                        "remote_path": "tiny.gguf", "local_path": "tiny/tiny.gguf",
                        "required": True, "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                },
            }
        },
    }
    manifest.write_text(json.dumps(document))
    return ModelRegistry(manifest, tmp_path / "models")


def test_resume_download(tiny_registry, tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        manager = DownloadManager(tiny_registry, tmp_path / "downloads")
        manager.registry.url = lambda component: f"http://127.0.0.1:{server.server_port}/tiny"
        component = tiny_registry.get("tiny")["components"]["diffusion"]
        partial = manager.partial_path("tiny", component)
        partial.parent.mkdir(parents=True)
        partial.write_bytes(RangeHandler.payload[:5])
        manager.install("tiny")
        assert tiny_registry.path(component).read_bytes() == RangeHandler.payload
        assert not partial.exists()
    finally:
        server.shutdown()


def test_checksum_mismatch_removes_partial(tiny_registry, tmp_path):
    component = tiny_registry.get("tiny")["components"]["diffusion"]
    component["sha256"] = "0" * 64
    manager = DownloadManager(tiny_registry, tmp_path / "downloads")
    manager.registry.url = lambda component: "data:application/octet-stream," + RangeHandler.payload.decode()
    with pytest.raises(WorkbenchError, match="Checksum"):
        manager.install("tiny")
    assert not manager.partial_path("tiny", component).exists()
