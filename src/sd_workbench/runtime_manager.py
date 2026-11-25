import json
import os
from pathlib import Path
import shutil

from .engine import EngineClient
from .errors import WorkbenchError
from .util import file_sha256, read_json


class RuntimeManager:
    def __init__(self, paths, settings, engine=None):
        self.paths = paths
        self.settings = settings
        self.engine = engine or EngineClient()

    def manifest(self, revision=None):
        root = self.paths.runtime_current if revision is None else self.paths.data / "runtimes" / "releases" / revision
        path = root / "manifest.json"
        if not path.is_file():
            raise WorkbenchError("runtime_missing", "No activated sd.cpp runtime. Run: sdwb runtime build")
        return read_json(path)

    def status(self):
        current = self.paths.runtime_current
        result = {"active": False, "revision": None, "manifest": None, "releases": [], "engine": self.engine.engine}
        releases = self.paths.data / "runtimes" / "releases"
        if releases.is_dir():
            result["releases"] = sorted(path.name for path in releases.iterdir() if (path / "manifest.json").is_file())
        try:
            manifest = self.manifest()
            result["manifest"] = manifest
            result["revision"] = manifest["revision"]
            result["active"] = (current / "bin" / "sd-cli").is_file() and (current / "bin" / "sd-server").is_file()
        except (WorkbenchError, OSError, ValueError):
            pass
        result["host_data_dir"] = os.environ.get("SDWB_HOST_DATA_DIR", str(self.paths.data))
        return result

    def verify(self, devices=False):
        manifest = self.manifest()
        root = self.paths.runtime_current
        for relative, expected in manifest["files"].items():
            path = root / relative
            if not path.is_file() or path.stat().st_size != expected["bytes"]:
                raise WorkbenchError("runtime_corrupt", f"Runtime artifact missing or has wrong size: {relative}")
            if file_sha256(path) != expected["sha256"]:
                raise WorkbenchError("runtime_corrupt", f"Runtime artifact checksum mismatch: {relative}")
        image = self.settings["runtime"]["image"]
        dockerfile = Path(__file__).resolve().parents[2] / "docker" / "Dockerfile.runtime"
        if Path("/app/docker/Dockerfile.runtime").is_file():
            dockerfile = Path("/app/docker/Dockerfile.runtime")
        self.engine.ensure_runtime_image(image, dockerfile)
        args = ["--list-devices"] if devices else ["--help"]
        output = self.run_short(args, devices=devices)
        return {"verified": True, "revision": manifest["revision"], "output": output[-8000:]}

    def run_short(self, args, devices=False):
        host_data = Path(os.environ.get("SDWB_HOST_DATA_DIR", str(self.paths.data)))
        image = self.settings["runtime"]["image"]
        name = f"sdwb-verify-{os.getpid()}"
        configured = os.environ.get("SDWB_DRI_DEVICES", "") if devices else ""
        container = self.engine.create(
            name, image, args,
            [f"{host_data / 'runtimes/current/bin'}:/opt/sd/bin:ro"],
            [Path(value) for value in configured.split(",") if value],
            environment={"LD_LIBRARY_PATH": "/opt/sd/bin"},
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
        current = self.paths.runtime_current
        previous = os.readlink(current) if current.is_symlink() else None
        temporary = self.paths.data / "runtimes" / "current.new"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(Path("releases") / revision)
        os.replace(temporary, current)
        if previous:
            (self.paths.data / "runtimes" / "previous").write_text(previous + "\n")
        return self.status()

    def rollback(self):
        previous_path = self.paths.data / "runtimes" / "previous"
        if not previous_path.is_file():
            raise WorkbenchError("runtime_rollback_unavailable", "No previous runtime is recorded", 404)
        previous = previous_path.read_text().strip()
        if previous.startswith("releases/"):
            previous = previous.removeprefix("releases/")
        return self.activate(previous)
