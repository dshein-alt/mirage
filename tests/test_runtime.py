import hashlib
import json
import os

import pytest

from sd_workbench.errors import WorkbenchError
from sd_workbench.runtime_manager import RuntimeManager


class FakeEngine:
    engine = "podman"

    def ensure_runtime_image(self, image, dockerfile):
        pass

    def run_short(self, args, devices=False):
        return "sd.cpp fake runtime"


def test_runtime_verifies_exported_artifacts(paths):
    current = paths.data / "runtimes" / "releases" / "test"
    binary = current / "bin" / "sd-cli"
    server = current / "bin" / "sd-server"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binary")
    server.write_bytes(b"server")
    manifest = {"revision": "test", "files": {"bin/sd-cli": {"bytes": 6, "sha256": hashlib.sha256(b"binary").hexdigest()}}}
    (current / "manifest.json").write_text(json.dumps(manifest))
    (paths.data / "runtimes" / "current").symlink_to("releases/test")
    settings = {"runtime": {"image": "test"}}
    manager = RuntimeManager(paths, settings, FakeEngine())
    manager.run_short = lambda args, devices=False: "sd.cpp fake runtime"
    assert manager.verify()["verified"]
    binary.write_bytes(b"broken")
    with pytest.raises(WorkbenchError, match="wrong size|checksum"):
        manager.verify()


def test_runtime_activation_preserves_previous(paths):
    releases = paths.data / "runtimes" / "releases"
    for revision in ("one", "two"):
        (releases / revision / "bin").mkdir(parents=True)
        (releases / revision / "manifest.json").write_text(json.dumps({"revision": revision, "files": {}}))
        (releases / revision / "bin" / "sd-cli").touch()
        (releases / revision / "bin" / "sd-server").touch()
    paths.runtime_current.symlink_to("releases/one")
    manager = RuntimeManager(paths, {"runtime": {"image": "test"}}, FakeEngine())
    manager.activate("two")
    assert os.readlink(paths.runtime_current) == "releases/two"
    manager.rollback()
    assert os.readlink(paths.runtime_current) == "releases/one"
