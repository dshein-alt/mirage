import os
from pathlib import Path
import secrets
import threading
import time

from .engine import EngineClient
from .errors import WorkbenchError
from .progress import parse_progress, parse_stage


class ProcessRunner:
    def __init__(self, paths, registry, settings, database, media, engine=None):
        self.paths = paths
        self.registry = registry
        self.settings = settings
        self.database = database
        self.media = media
        self.engine = engine or EngineClient()
        self.fake = os.environ.get("SDWB_FAKE_SD") == "1"

    def run(self, job, cancel_event):
        started = time.monotonic()
        resolved = job["resolved"]
        final = self.paths.outputs / resolved["output_name"]
        temporary = self.paths.outputs / f".{final.stem}.{os.getpid()}.{secrets.token_hex(6)}.partial.png"
        log_path = Path(job["log_path"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        active = self.paths.data / ".active-job"
        active.write_text(job["id"] + "\n")
        try:
            with log_path.open("w", encoding="utf-8") as log:
                command = self.command(job, temporary)
                log.write("sd.cpp revision: " + str(resolved.get("runtime_revision")) + "\n")
                log.write("Arguments: " + " ".join(self.redact_argument(arg) for arg in command) + "\n")
                log.flush()
                if self.fake:
                    self.run_fake(job, temporary, log, cancel_event)
                else:
                    self.run_container(job, command, log, cancel_event)
            if cancel_event.is_set():
                raise WorkbenchError("job_cancelled", "Job cancelled", 409)
            duration = time.monotonic() - started
            job["duration_seconds"] = duration
            record = self.media.publish(job, temporary, final)
            return record, duration
        finally:
            temporary.unlink(missing_ok=True)
            active.unlink(missing_ok=True)

    def redact_argument(self, value):
        return value.replace(os.environ.get("HF_TOKEN", ""), "<redacted>") if os.environ.get("HF_TOKEN") else value

    def command(self, job, temporary):
        resolved = job["resolved"]
        model = self.registry.get(resolved["model"])
        command = []
        for component in model["components"].values():
            if component["role"] == "llm_vision":
                continue
            command.extend([f"--{component['role']}" if component["role"] != "diffusion" else "--diffusion-model", f"/models/{component['local_path']}"])
        if "backend_profile" in resolved:
            profile = resolved["backend_profile"]
            extra_args = resolved.get("model_extra_args", [])
        else:
            model_settings = self.settings["models"][resolved["model"]]
            profile_name = model_settings["backend_profile"]
            profile = self.settings["backend_profiles"][profile_name]
            extra_args = model_settings.get("extra_args", [])
        command.extend(["--backend", profile["backend"], *profile.get("extra_args", []), *extra_args])
        command.extend([
            "--cfg-scale", str(resolved["cfg_scale"]),
            "--sampling-method", resolved["sampler"],
            "--scheduler", resolved["scheduler"],
            "--steps", str(resolved["steps"]),
            "-W", str(resolved["width"]), "-H", str(resolved["height"]),
            "--seed", str(resolved["seed"]), "-p", job["request"]["prompt"],
        ])
        reference_ids = job["request"].get("reference_ids", [])
        if reference_ids:
            vision = next(component for component in model["components"].values() if component["role"] == "llm_vision")
            command.extend(["--llm_vision", f"/models/{vision['local_path']}"])
            for input_id in reference_ids:
                record = self.database.get_input(input_id)
                command.extend(["-r", f"/inputs/{record['filename']}"])
        command.extend(["-o", f"/output/{temporary.name}", "-v"])
        return command

    def run_fake(self, job, temporary, log, cancel_event):
        from PIL import Image

        for step in range(job["resolved"]["steps"]):
            if cancel_event.is_set():
                raise WorkbenchError("job_cancelled", "Job cancelled", 409)
            log.write(f"sampling step {step + 1}/{job['resolved']['steps']}\n")
            log.flush()
            self.database.update_job(job["id"], progress=(step + 1) / job["resolved"]["steps"] * 0.95, stage="sampling")
            time.sleep(0.005)
        image = Image.new("RGB", (job["resolved"]["width"], job["resolved"]["height"]), (35, 90, 120))
        image.save(temporary, "PNG")
        log.write("VAE decode complete\nSaved fake PNG (SDWB_FAKE_SD=1)\n")

    def run_container(self, job, command, log, cancel_event):
        host_data = Path(os.environ.get("SDWB_HOST_DATA_DIR", str(self.paths.data)))
        image = self.settings["runtime"]["image"]
        dockerfile = Path("/app/docker/Dockerfile.runtime")
        if not dockerfile.is_file():
            dockerfile = Path(__file__).resolve().parents[2] / "docker" / "Dockerfile.runtime"
        self.engine.ensure_runtime_image(image, dockerfile)
        configured_devices = os.environ.get("SDWB_DRI_DEVICES", "")
        devices = [Path(value) for value in configured_devices.split(",") if value]
        if not devices and os.environ.get("SDWB_ALLOW_NO_GPU") != "1":
            raise WorkbenchError("gpu_unavailable", "No /dev/dri GPU devices are visible. Check device permissions and Vulkan setup", 503)
        revision = job["resolved"]["runtime_revision"]
        if Path(revision).name != revision:
            raise WorkbenchError("runtime_invalid", "Invalid runtime revision in job snapshot", 500)
        release_bin = self.paths.data / "runtimes" / "releases" / revision / "bin"
        if not (release_bin / "sd-cli").is_file():
            raise WorkbenchError("runtime_missing", f"Runtime release required by this job is unavailable: {revision}", 500)
        name = f"sdwb-job-{job['id'][:12]}"
        binds = [
            f"{host_data / 'runtimes' / 'releases' / revision / 'bin'}:/opt/sd/bin:ro",
            f"{host_data / 'models'}:/models:ro",
            f"{host_data / 'inputs'}:/inputs:ro",
            f"{host_data / 'outputs'}:/output:rw",
            f"{host_data / 'logs'}:/logs:rw",
        ]
        container = self.engine.create(name, image, command, binds, devices, self.settings.get("environment", {}))
        self.database.set_container(job["id"], container)
        try:
            self.engine.start(container)
            response, connection = self.engine.logs(container)
            try:
                buffered = b""
                while True:
                    if cancel_event.is_set():
                        self.engine.remove(container, force=True)
                    block = response.read1(65536) if hasattr(response, "read1") else response.read(65536)
                    if not block:
                        break
                    buffered += block
                    while b"\n" in buffered:
                        raw, buffered = buffered.split(b"\n", 1)
                        line = raw.decode("utf-8", "replace").rstrip("\r")
                        log.write(line + "\n")
                        log.flush()
                        self.record_log_progress(job["id"], line)
                if buffered:
                    log.write(buffered.decode("utf-8", "replace") + "\n")
            finally:
                connection.close()
            try:
                status = self.engine.wait(container)
            except WorkbenchError:
                if not cancel_event.is_set():
                    raise
                status = None
            if cancel_event.is_set():
                raise WorkbenchError("job_cancelled", "Job cancelled", 409)
            if status:
                raise WorkbenchError("inference_failed", f"sd-cli exited with status {status}; see job logs", 500)
        finally:
            self.database.update_job(job["id"], container_id=None)
            self.engine.remove(container, force=True)

    def record_log_progress(self, job_id, line):
        progress = parse_progress(line)
        stage = parse_stage(line)
        fields = {}
        if progress is not None:
            fields["progress"] = 0.05 + progress * 0.9
        if stage:
            fields["stage"] = stage
        if fields:
            self.database.update_job(job_id, **fields)

    def cancel(self, job_id):
        job = self.database.get_job_with_container(job_id)
        if job.get("container_id"):
            self.engine.remove(job["container_id"], force=True)
