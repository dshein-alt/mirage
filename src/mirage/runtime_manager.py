import os
from pathlib import Path
import re

from .engine import EngineClient, nvidia_device_requests
from .errors import WorkbenchError
from .util import file_sha256, read_json


class RuntimeManager:
    def __init__(self, paths, settings, engine=None):
        self.paths = paths
        self.settings = settings
        self.engine = engine or EngineClient()

    def release_root(self, revision):
        return self.paths.runtime_current if revision is None else self.paths.data / "runtimes" / "releases" / revision

    def manifest(self, revision=None):
        root = self.release_root(revision)
        path = root / "manifest.json"
        if not path.is_file():
            raise WorkbenchError("runtime_missing", "No activated sd.cpp runtime. Run: mirage runtime build")
        return read_json(path)

    @staticmethod
    def build_id(manifest):
        return manifest.get("build_id", manifest["revision"])

    def image_identity(self, manifest):
        image = manifest.get("runtime_image", {})
        return {
            "reference": image.get("reference", self.settings["runtime"]["image"]),
            "id": image.get("id", self.settings["runtime"]["image"]),
        }

    def status(self):
        current = self.paths.runtime_current
        result = {
            "active": False,
            "revision": None,
            "sd_cpp_commit": None,
            "build_id": None,
            "backend": None,
            "runtime_image": None,
            "previous": None,
            "manifest": None,
            "releases": [],
            "engine": self.engine.engine,
        }
        releases = self.paths.data / "runtimes" / "releases"
        if releases.is_dir():
            result["releases"] = sorted(path.name for path in releases.iterdir() if (path / "manifest.json").is_file())
        try:
            manifest = self.manifest()
            build_id = self.build_id(manifest)
            result.update({
                "manifest": manifest,
                "revision": manifest["revision"],
                "sd_cpp_commit": manifest.get("sd_cpp_commit", manifest["revision"]),
                "build_id": build_id,
                "backend": manifest.get("backend", "vulkan"),
                "runtime_image": self.image_identity(manifest),
                "active": (current / "bin" / "sd-cli").is_file() and (current / "bin" / "sd-server").is_file(),
            })
        except (WorkbenchError, OSError, ValueError):
            pass
        previous_path = self.paths.data / "runtimes" / "previous"
        if previous_path.is_file():
            previous = previous_path.read_text(encoding="utf-8").strip().removeprefix("releases/")
            try:
                manifest = self.manifest(previous)
                result["previous"] = {
                    "build_id": self.build_id(manifest),
                    "sd_cpp_commit": manifest.get("sd_cpp_commit", manifest["revision"]),
                    "runtime_image": self.image_identity(manifest),
                }
            except (WorkbenchError, OSError, ValueError):
                result["previous"] = {"build_id": previous, "missing": True}
        result["host_data_dir"] = os.environ.get("MIRAGE_HOST_DATA_DIR", str(self.paths.data))
        return result

    def verify(self, devices=False, revision=None):
        manifest = self.manifest(revision)
        root = self.release_root(revision)
        for relative, expected in manifest["files"].items():
            path = root / relative
            if not path.is_file() or path.stat().st_size != expected["bytes"]:
                raise WorkbenchError("runtime_corrupt", f"Runtime artifact missing or has wrong size: {relative}")
            if file_sha256(path) != expected["sha256"]:
                raise WorkbenchError("runtime_corrupt", f"Runtime artifact checksum mismatch: {relative}")
        image = self.image_identity(manifest)["id"]
        self.engine.require_runtime_image(image)
        args = ["--list-devices"] if devices else ["--help"]
        output = self.run_short(args, devices=devices, revision=revision, image=image)
        if devices and manifest.get("backend") == "cuda" and not re.search(r"^CUDA\d+\t", output, re.MULTILINE):
            raise WorkbenchError("gpu_unavailable", "The runtime did not enumerate a CUDA device. Check the NVIDIA driver and container toolkit", 503)
        return {
            "verified": True,
            "revision": manifest["revision"],
            "sd_cpp_commit": manifest.get("sd_cpp_commit", manifest["revision"]),
            "build_id": self.build_id(manifest),
            "runtime_image": self.image_identity(manifest),
            "output": output[-8000:],
        }

    def run_short(self, args, devices=False, revision=None, image=None):
        host_data = Path(os.environ.get("MIRAGE_HOST_DATA_DIR", str(self.paths.data)))
        manifest = self.manifest(revision)
        build_id = self.build_id(manifest)
        image = image or self.image_identity(manifest)["id"]
        name = f"mirage-verify-{build_id[:12]}-{os.getpid()}"
        backend = manifest.get("backend", "vulkan")
        configured = os.environ.get("MIRAGE_DRI_DEVICES", "") if devices and backend == "vulkan" else ""
        release = host_data / "runtimes" / "releases" / build_id if revision is not None else host_data / "runtimes" / "current"
        container = self.engine.create(
            name, image, args,
            [f"{release / 'bin'}:/opt/sd/bin:ro"],
            [Path(value) for value in configured.split(",") if value],
            environment={"LD_LIBRARY_PATH": "/opt/sd/bin"},
            device_requests=nvidia_device_requests(self.engine.engine) if devices and backend == "cuda" else [],
        )
        try:
            self.engine.start(container)
            response, connection = self.engine.logs(container)
            chunks = []
            try:
                while True:
                    block = response.read1(65536) if hasattr(response, "read1") else response.read(65536)
                    if not block:
                        break
                    chunks.append(block)
            finally:
                connection.close()
            status = self.engine.wait(container)
            output = b"".join(chunks).decode("utf-8", "replace")
            if status:
                raise WorkbenchError("runtime_verify_failed", output[-4000:] or f"sd-cli exited {status}", 500)
            return output
        finally:
            self.engine.remove(container, force=True)

    def activate(self, revision):
        release = self.paths.data / "runtimes" / "releases" / revision
        if not (release / "manifest.json").is_file():
            raise WorkbenchError("runtime_not_found", f"Unknown runtime release: {revision}", 404)
        if (self.paths.data / ".active-job").exists():
            raise WorkbenchError("runtime_busy", "Cannot switch runtimes while a job is active", 409)
        target_backend = self.manifest(revision).get("backend", "vulkan")
        if (self.paths.runtime_current / "manifest.json").is_file():
            current_backend = self.manifest().get("backend", "vulkan")
            if target_backend != current_backend:
                raise WorkbenchError("runtime_backend_mismatch", "Use the installer to switch GPU backends", 409)
        self.verify(revision=revision)
        current = self.paths.runtime_current
        previous = os.readlink(current) if current.is_symlink() else None
        temporary = self.paths.data / "runtimes" / "current.new"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(Path("releases") / revision)
        os.replace(temporary, current)
        if previous and previous != f"releases/{revision}":
            (self.paths.data / "runtimes" / "previous").write_text(previous + "\n", encoding="utf-8")
        return self.status()

    def rollback(self):
        previous_path = self.paths.data / "runtimes" / "previous"
        if not previous_path.is_file():
            raise WorkbenchError("runtime_rollback_unavailable", "No previous runtime is recorded", 404)
        previous = previous_path.read_text(encoding="utf-8").strip()
        if previous.startswith("releases/"):
            previous = previous.removeprefix("releases/")
        return self.activate(previous)
