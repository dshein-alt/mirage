import os
from pathlib import Path
import secrets
import shutil
import uuid

from .config import ConfigService
from .database import Database
from .downloader import DownloadManager
from .errors import WorkbenchError
from .images import MediaService
from .manifest import ModelRegistry
from .output import reserve_output
from .paths import Paths
from .queue import JobQueue
from .runner import ProcessRunner
from .runtime_manager import RuntimeManager
from .security import validate_prompt, validate_references


class Application:
    def __init__(self, paths=None, start_queue=False):
        self.paths = paths or Paths.from_environment()
        self.registry = ModelRegistry(self.paths.model_manifest, self.paths.data / "models")
        self.config = ConfigService(self.paths, self.registry)
        self.settings = self.config.settings()
        self.settings["server"] = self.config.resolve_server(self.settings["server"])
        self.database = Database(self.paths.database)
        if os.environ.get("MIRAGE_MAINTENANCE") == "1":
            self.database.ensure_maintenance(os.environ.get("MIRAGE_DEPLOYMENT_ID", "installer"))
        self.media = MediaService(self.paths, self.database, self.settings["security"]["max_upload_bytes"])
        self.media.backfill_dimensions()
        self.runtime = RuntimeManager(self.paths, self.settings)
        self.runner = ProcessRunner(self.paths, self.registry, self.settings, self.database, self.media)
        self.downloader = DownloadManager(self.registry, self.paths.downloads)
        self.queue = JobQueue(self.database, self.runner, self.downloader)
        if start_queue:
            self.queue.start()

    def close(self):
        self.queue.stop()
        self.database.close()

    def submit_generation(self, request):
        if not isinstance(request, dict):
            raise WorkbenchError("request_invalid", "JSON object required")
        prompt = validate_prompt(request.get("prompt"), self.settings["security"]["max_prompt_chars"])
        model = request.get("model", self.config.snapshot()["document"]["defaults"]["model"])
        references = validate_references(model, request.get("reference_ids", []), self.registry)
        for input_id in references:
            self.media.input_path(input_id)
        editing = bool(references)
        self.registry.verify(model, editing=editing)
        overrides = request.get("overrides", {})
        if "width" in request or "height" in request or (isinstance(overrides, dict) and set(overrides) & {"width", "height"}):
            raise WorkbenchError("dimension_override", "Width and height overrides are not supported; choose sd, hd, or fhd")
        if not isinstance(overrides, dict) or set(overrides) - {"steps", "cfg_scale", "sampler", "scheduler", "seed"}:
            raise WorkbenchError("overrides_invalid", "Unsupported generation override")
        resolved = self.config.resolve(model, request.get("resolution"), request.get("quality"), overrides)
        if resolved["seed"] is None:
            resolved["seed"] = secrets.randbelow(2147483648)
        if os.environ.get("MIRAGE_FAKE_SD") == "1":
            revision = self.settings["runtime"]["minimum_sd_revision"]
            manifest = {
                "sd_cpp_commit": revision,
                "build_id": f"fake-{revision[:12]}",
                "runtime_image": {"reference": self.settings["runtime"]["image"], "id": self.settings["runtime"]["image"]},
            }
        else:
            manifest = self.runtime.manifest()
            revision = manifest["sd_cpp_commit"]
        resolved.update({
            "runtime_revision": revision,
            "sd_cpp_commit": manifest["sd_cpp_commit"],
            "runtime_build": manifest["build_id"],
            "runtime_image_id": manifest["runtime_image"]["id"],
            "runtime_image_reference": manifest["runtime_image"]["reference"],
            "runtime_backend": manifest.get("backend", "vulkan"),
        })
        model_settings = self.settings["models"][model]
        profile_name = model_settings["backend_profile"]
        profile = self.settings["backend_profiles"][profile_name]
        resolved["backend_profile"] = {
            "name": profile_name,
            "backend": profile["backend"],
            "extra_args": list(profile.get("extra_args", [])),
        }
        resolved["model_extra_args"] = list(model_settings.get("extra_args", []))
        final = reserve_output(self.paths.outputs, self.registry.get(model)["label"], resolved["resolution"], resolved["quality"], self.database.output_name_exists)
        resolved["output_name"] = final.name
        normalized = {
            "prompt": prompt,
            "model": model,
            "resolution": resolved["resolution"],
            "quality": resolved["quality"],
            "reference_ids": references,
        }
        log_path = self.paths.logs / "jobs" / f"{uuid.uuid4()}.log"
        try:
            job = self.database.create_job("generation", normalized, resolved, model, runtime_revision=revision, log_path=str(log_path))
        except BaseException:
            final.unlink(missing_ok=True)
            raise
        self.queue.notify()
        return job

    def submit_install(self, model_id, editing=False, verify=False):
        self.registry.get(model_id)
        for job in self.database.active_model_jobs(model_id):
            if job["kind"] == "model_install":
                return job
        resolved = {"editing": editing, "verify": verify}
        log_path = self.paths.logs / "models" / f"{model_id}-{uuid.uuid4()}.log"
        job = self.database.create_job("model_install", {"model": model_id, "with_editing": editing}, resolved, model_id, log_path=str(log_path))
        self.queue.notify()
        return job

    def remove_model(self, model_id):
        active = self.database.active_model_jobs(model_id)
        self.downloader.remove(model_id, active)
        return self.registry.status(model_id)

    def update_settings(self, document):
        if isinstance(document, dict) and document.get("server") != self.settings["server"]:
            raise WorkbenchError("server_managed_by_installer", "Use the installer to change the access mode or port")
        updated = self.config.update_settings(document)
        self.settings = updated
        self.runner.settings = updated
        self.runtime.settings = updated
        self.media.max_upload_bytes = updated["security"]["max_upload_bytes"]
        return updated

    def system(self):
        usage = shutil.disk_usage(self.paths.data)
        return {
            "version": "1.0.0",
            "data_dir": str(self.paths.data),
            "disk": {"total": usage.total, "used": usage.used, "free": usage.free},
            "runtime": self.runtime.status(),
            "queue": {"active_gpu_jobs": 1 if (self.paths.data / ".active-job").exists() else 0, "concurrency": self.settings["queue"]["concurrency"]},
            "maintenance": self.database.maintenance_status(),
            "deployment_id": os.environ.get("MIRAGE_DEPLOYMENT_ID"),
            "container_image": os.environ.get("MIRAGE_CONTAINER_IMAGE_ID"),
            "fake_runner": os.environ.get("MIRAGE_FAKE_SD") == "1",
        }
