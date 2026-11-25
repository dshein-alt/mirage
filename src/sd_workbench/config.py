from copy import deepcopy
import math
import threading

from .errors import WorkbenchError
from .manifest import ModelRegistry
from .paths import Paths
from .util import atomic_write_json, document_hash, read_json

SAMPLERS = {
    "euler", "euler_a", "heun", "dpm2", "dpm++2s_a", "dpm++2m", "dpm++2mv2",
    "ipndm", "ipndm_v", "lcm", "ddim_trailing", "tcd", "res_multistep", "res_2s",
    "er_sde", "euler_cfg_pp", "euler_a_cfg_pp", "euler_ge", "dpm++2m_sde",
    "dpm++2m_sde_bt", "lms",
}
SCHEDULERS = {
    "discrete", "karras", "exponential", "ays", "gits", "sgm_uniform", "simple",
    "smoothstep", "kl_optimal", "lcm", "bong_tangent", "ltx2", "logit_normal",
    "flux2", "flux", "beta", "llada_image",
}


class ConfigService:
    def __init__(self, paths: Paths, registry: ModelRegistry):
        self.paths = paths
        self.registry = registry
        self._lock = threading.RLock()
        self._snapshot = None
        self._revision = None
        self.ensure()

    def ensure(self):
        if not self.paths.generation_config.exists():
            atomic_write_json(
                self.paths.generation_config,
                read_json(self.paths.config_defaults / "generation-presets.default.json"),
            )
        if not self.paths.settings.exists():
            atomic_write_json(
                self.paths.settings,
                read_json(self.paths.config_defaults / "settings.default.json"),
            )
        self.reload()

    @staticmethod
    def validate(document, registry):
        try:
            if document["schema_version"] != 1:
                raise ValueError("schema_version must be 1")
            defaults = document["defaults"]
            resolutions = document["resolutions"]
            models = document["models"]
            if set(models) != set(registry.models):
                raise ValueError(f"models must contain exactly: {', '.join(registry.models)}")
            if defaults["model"] not in models:
                raise ValueError("defaults.model is unknown")
            if defaults["resolution"] not in resolutions:
                raise ValueError("defaults.resolution is unknown")
            if defaults["quality"] not in models[defaults["model"]]["quality"]:
                raise ValueError("defaults.quality is unknown for the default model")
            if not resolutions:
                raise ValueError("at least one resolution is required")
            for resolution_id, resolution in resolutions.items():
                if not resolution_id or not isinstance(resolution, dict):
                    raise ValueError("resolution IDs must be non-empty")
                for dimension in ("width", "height"):
                    value = resolution[dimension]
                    if isinstance(value, bool) or not isinstance(value, int) or not 64 <= value <= 4096:
                        raise ValueError(f"resolutions.{resolution_id}.{dimension} must be an integer from 64 to 4096")
                    if value % 32:
                        raise ValueError(f"resolutions.{resolution_id}.{dimension} must be divisible by 32")
            for model_id, model in models.items():
                quality = model["quality"]
                if not quality or model["default_quality"] not in quality:
                    raise ValueError(f"models.{model_id}.default_quality is invalid")
                cfg = model["cfg_scale"]
                if isinstance(cfg, bool) or not isinstance(cfg, (int, float)) or not math.isfinite(cfg) or not 0 <= cfg <= 30:
                    raise ValueError(f"models.{model_id}.cfg_scale must be finite and between 0 and 30")
                if model["sampler"] not in SAMPLERS:
                    raise ValueError(f"models.{model_id}.sampler is unsupported")
                if model["scheduler"] not in SCHEDULERS:
                    raise ValueError(f"models.{model_id}.scheduler is unsupported")
                for quality_id, preset in quality.items():
                    steps = preset["steps"]
                    if not quality_id or isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 250:
                        raise ValueError(f"models.{model_id}.quality.{quality_id}.steps must be an integer from 1 to 250")
        except (KeyError, TypeError) as error:
            raise ValueError(f"missing or invalid configuration field: {error}") from error
        return deepcopy(document)

    def reload(self):
        with self._lock:
            try:
                candidate = read_json(self.paths.generation_config)
                candidate = self.validate(candidate, self.registry)
            except (OSError, ValueError) as error:
                raise WorkbenchError("config_invalid", f"Invalid generation configuration: {error}") from error
            self._snapshot = candidate
            self._revision = document_hash(candidate)
            return self.snapshot()

    def snapshot(self):
        with self._lock:
            return {"document": deepcopy(self._snapshot), "revision": self._revision}

    def update(self, document):
        candidate = self.validate(document, self.registry)
        with self._lock:
            atomic_write_json(self.paths.generation_config, candidate)
            self._snapshot = deepcopy(candidate)
            self._revision = document_hash(candidate)
            return self.snapshot()

    def restore_defaults(self):
        return self.update(read_json(self.paths.config_defaults / "generation-presets.default.json"))

    def resolve(self, model_id, resolution_id=None, quality_id=None, overrides=None):
        snapshot = self.snapshot()
        document = snapshot["document"]
        if model_id not in document["models"]:
            raise WorkbenchError("model_unknown", f"Unknown model: {model_id}", 404)
        model = document["models"][model_id]
        resolution_id = resolution_id or document["defaults"]["resolution"]
        quality_id = quality_id or model["default_quality"]
        if resolution_id not in document["resolutions"]:
            raise WorkbenchError("resolution_unknown", f"Unknown resolution: {resolution_id}")
        if quality_id not in model["quality"]:
            raise WorkbenchError("quality_unknown", f"Quality {quality_id} is not defined for {model_id}")
        resolution = document["resolutions"][resolution_id]
        overrides = overrides or {}
        result = {
            "model": model_id,
            "resolution": resolution_id,
            "quality": quality_id,
            "width": overrides.get("width", resolution["width"]),
            "height": overrides.get("height", resolution["height"]),
            "steps": overrides.get("steps", model["quality"][quality_id]["steps"]),
            "cfg_scale": overrides.get("cfg_scale", model["cfg_scale"]),
            "sampler": overrides.get("sampler", model["sampler"]),
            "scheduler": overrides.get("scheduler", model["scheduler"]),
            "seed": overrides.get("seed"),
            "config_revision": snapshot["revision"],
        }
        for dimension in ("width", "height"):
            value = result[dimension]
            if isinstance(value, bool) or not isinstance(value, int) or not 64 <= value <= 4096 or value % 32:
                raise WorkbenchError("dimension_invalid", f"{dimension} must be divisible by 32 and between 64 and 4096")
        if isinstance(result["steps"], bool) or not isinstance(result["steps"], int) or not 1 <= result["steps"] <= 250:
            raise WorkbenchError("steps_invalid", "steps must be an integer from 1 to 250")
        cfg = result["cfg_scale"]
        if isinstance(cfg, bool) or not isinstance(cfg, (int, float)) or not math.isfinite(cfg) or not 0 <= cfg <= 30:
            raise WorkbenchError("cfg_invalid", "cfg_scale must be between 0 and 30")
        if result["sampler"] not in SAMPLERS or result["scheduler"] not in SCHEDULERS:
            raise WorkbenchError("sampler_invalid", "sampler or scheduler is unsupported")
        if result["seed"] is not None and (isinstance(result["seed"], bool) or not isinstance(result["seed"], int) or not -1 <= result["seed"] <= 2147483647):
            raise WorkbenchError("seed_invalid", "seed must be between -1 and 2147483647")
        return result

    def settings(self):
        try:
            return read_json(self.paths.settings)
        except (OSError, ValueError) as error:
            raise WorkbenchError("settings_invalid", f"Invalid settings: {error}", 500) from error

    def update_settings(self, document):
        allowed_args = {"--offload-to-cpu", "--diffusion-fa"}
        try:
            if document["schema_version"] != 1 or document["queue"]["concurrency"] != 1:
                raise ValueError("schema_version must be 1 and queue.concurrency must remain 1")
            if not isinstance(document["server"]["host"], str) or not isinstance(document["server"]["port"], int):
                raise ValueError("server host and port are invalid")
            if set(document["models"]) != set(self.registry.models):
                raise ValueError("settings models must match the model registry")
            profiles = document["backend_profiles"]
            for model_id, model in document["models"].items():
                if model["backend_profile"] not in profiles:
                    raise ValueError(f"unknown backend profile for {model_id}")
                if any(arg not in allowed_args for arg in model.get("extra_args", [])):
                    raise ValueError(f"unsupported extra argument for {model_id}")
            for profile_id, profile in profiles.items():
                if not profile_id or not isinstance(profile["backend"], str) or not profile["backend"]:
                    raise ValueError("backend profiles require a non-empty backend")
                if any(arg not in allowed_args for arg in profile.get("extra_args", [])):
                    raise ValueError(f"unsupported extra argument in profile {profile_id}")
            if not 1024 <= document["security"]["max_upload_bytes"] <= 104857600:
                raise ValueError("max_upload_bytes must be between 1 KiB and 100 MiB")
            if not 1 <= document["security"]["max_prompt_chars"] <= 100000:
                raise ValueError("max_prompt_chars must be between 1 and 100000")
            if not isinstance(document.get("environment", {}), dict):
                raise ValueError("environment must be an object")
        except (KeyError, TypeError) as error:
            raise ValueError(f"missing or invalid settings field: {error}") from error
        candidate = deepcopy(document)
        atomic_write_json(self.paths.settings, candidate)
        return candidate
