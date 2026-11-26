from io import BytesIO, StringIO
import hashlib
import json
import os
from pathlib import Path

import pytest

from mirage.errors import WorkbenchError
from mirage.runtime_build import build_runtime
from mirage.runtime_manager import RuntimeManager


class FakeEngine:
    engine = "podman"

    def require_runtime_image(self, image):
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
    manager.run_short = lambda args, devices=False, revision=None, image=None: "sd.cpp fake runtime"
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
    manager.run_short = lambda args, devices=False, revision=None, image=None: "sd.cpp fake runtime"
    manager.activate("two")
    assert os.readlink(paths.runtime_current) == "releases/two"
    manager.rollback()
    assert os.readlink(paths.runtime_current) == "releases/one"


def test_cross_backend_runtime_activation_requires_installer(paths):
    for revision, backend in (("vulkan-release", "vulkan"), ("cuda-release", "cuda")):
        release = paths.data / "runtimes" / "releases" / revision
        release.mkdir(parents=True)
        (release / "manifest.json").write_text(json.dumps({"revision": revision, "backend": backend, "files": {}}))
    paths.runtime_current.symlink_to("releases/vulkan-release")
    manager = RuntimeManager(paths, {"runtime": {"image": "test"}}, FakeEngine())

    with pytest.raises(WorkbenchError, match="installer") as error:
        manager.activate("cuda-release")

    assert error.value.status == 409
    assert os.readlink(paths.runtime_current) == "releases/vulkan-release"

    with pytest.raises(RuntimeError, match="installer"):
        build_runtime(Path(__file__).resolve().parents[1], paths.data, "podman", activate=True, backend="cuda")


def test_cuda_build_uses_cuda_images_and_records_backend(tmp_path, monkeypatch):
    import mirage.runtime_build as runtime_build

    commands = []
    identity = {
        "sd_cpp_commit": "cuda-revision",
        "build_id": "cuda-build",
        "build_environment": {},
    }

    def fake_identity(root, environment, git_command=None):
        return {**identity, "build_environment": environment}

    def fake_image_id(engine, reference):
        return f"image-id:{reference}"

    def fake_run(arguments, capture=False):
        arguments = [str(value) for value in arguments]
        commands.append(arguments)
        mount = next((value for value in arguments if value.endswith(":/export:rw")), None)
        if mount:
            export = Path(mount.split(":", 1)[0])
            binary = export / "bin" / "sd-cli"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"cuda binary")
            (export / "manifest.json").write_text(json.dumps({
                "revision": "cuda-revision",
                "files": {"bin/sd-cli": {"bytes": 11, "sha256": hashlib.sha256(b"cuda binary").hexdigest()}},
            }))
        return ""

    monkeypatch.setattr(runtime_build, "source_identity", fake_identity)
    monkeypatch.setattr(runtime_build, "image_id", fake_image_id)
    monkeypatch.setattr(runtime_build, "run", fake_run)
    source = tmp_path / "source"
    (source / "stable-diffusion.cpp" / "ggml" / "src").mkdir(parents=True)

    manifest = build_runtime(source, tmp_path / "data", "docker", backend="cuda")

    assert manifest["backend"] == "cuda"
    assert manifest["build_environment"]["mirage_backend"] == "cuda"
    assert any("docker/Dockerfile.builder.cuda" in value for command in commands for value in command)
    assert any("docker/Dockerfile.runtime.cuda" in value for command in commands for value in command)
    assert any("--gpus" in command and "all" in command for command in commands)


@pytest.mark.parametrize("devices_found", [True, False])
def test_cuda_device_verification_uses_nvidia_device_request(paths, devices_found):
    current = paths.data / "runtimes" / "releases" / "cuda"
    current.mkdir(parents=True)
    (current / "manifest.json").write_text(json.dumps({
        "revision": "cuda", "build_id": "cuda", "backend": "cuda", "files": {},
        "runtime_image": {"reference": "runtime", "id": "runtime-id"},
    }))
    paths.runtime_current.symlink_to("releases/cuda")
    captured = {}

    class NvidiaEngine:
        engine = "podman"

        def require_runtime_image(self, image):
            pass

        def create(self, *args, **kwargs):
            captured.update(kwargs)
            captured["devices"] = args[4]
            return "container-id"

        def start(self, container):
            pass

        def logs(self, container):
            output = b"CUDA0\tNVIDIA GeForce\nCPU\tHost\n" if devices_found else b"CPU\tHost\n"
            return BytesIO(output), StringIO()

        def wait(self, container):
            return 0

        def remove(self, container, force=False):
            pass

    manager = RuntimeManager(paths, {"runtime": {"image": "runtime"}}, NvidiaEngine())
    if devices_found:
        assert manager.verify(devices=True)["verified"]
    else:
        with pytest.raises(WorkbenchError, match="did not enumerate a CUDA device") as error:
            manager.verify(devices=True)
        assert error.value.code == "gpu_unavailable"

    assert captured["devices"] == []
    assert captured["device_requests"] == [{"Driver": "cdi", "DeviceIDs": ["nvidia.com/gpu=all"]}]
