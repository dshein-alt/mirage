from copy import deepcopy
from datetime import datetime, timezone
import math
from pathlib import Path
import shutil
import sys
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
RESOLUTIONS = {
    "sd": {"width": 512, "height": 512},
    "hd": {"width": 1024, "height": 1024},
    "fhd": {"width": 1920, "height": 1080},
}


class ConfigService:
    def __init__(self, paths: Paths, registry: ModelRegistry):
        self.paths = paths
        self.registry = registry
        self._lock = threading.RLock()
        self._snapshot = None
        self._revision = None
        self._warnings = []
        self.ensure()

    def ensure(self):
        if not self.paths.generation_config.exists():
            atomic_write_json(
                self.paths.generation_config,
                read_json(self.paths.config_defaults / "generation-presets.default.json"),
            )
        else:
            try:
                existing = read_json(self.paths.generation_config)
            except (OSError, ValueError):
                existing = None
            if existing and existing.get("schema_version") == 1:
                self.migrate_generation(existing)
        if not self.paths.settings.exists():
            atomic_write_json(
                self.paths.settings,
                read_json(self.paths.config_defaults / "settings.default.json"),
            )
        self.reload()

    def migration_warning(self, message):
        self._warnings.append(message)
        print(f"Mirage configuration migration: {message}", file=sys.stderr, flush=True)

    def migrate_generation(self, legacy):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = self.paths.generation_config.with_name(f"generation-presets.pre-mirage-{timestamp}.json")
        shutil.copy2(self.paths.generation_config, backup)
        with backup.open("rb") as stream:
            import os
            os.fsync(stream.fileno())
        defaults = deepcopy(legacy["defaults"])
        original = defaults.get("resolution")
        stock_mapping = {"small": "sd", "medium": "sd", "large": "hd", "hd": "hd"}
        if original in stock_mapping:
            defaults["resolution"] = stock_mapping[original]
        else:
            size = legacy.get("resolutions", {}).get(original, {})
            pair = (size.get("width"), size.get("height"))
            defaults["resolution"] = next(
                (name for name, dimensions in RESOLUTIONS.items() if pair == (dimensions["width"], dimensions["height"])),
                "hd",
            )
        document = {
            "schema_version": 2,
            "defaults": defaults,
            "resolutions": deepcopy(RESOLUTIONS),
            "models": deepcopy(legacy["models"]),
        }
        candidate = self.validate(document, self.registry)
        atomic_write_json(self.paths.generation_config, candidate)
        self.migration_warning(f"generation presets were backed up to {backup.name}")
        if original not in stock_mapping and defaults["resolution"] == "hd":
            original_size = legacy.get("resolutions", {}).get(original, {})
            if (original_size.get("width"), original_size.get("height")) != (1024, 1024):
                self.migration_warning("the previous default size is unsupported; HD 1024×1024 is now the default")

    @staticmethod
    def validate(document, registry):
        try:
            if document["schema_version"] != 2:
                raise ValueError("schema_version must be 2")
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
            if set(resolutions) != set(RESOLUTIONS):
                raise ValueError("resolutions must contain exactly: sd, hd, fhd")
            for resolution_id, expected in RESOLUTIONS.items():
                if resolutions[resolution_id] != expected:
                    raise ValueError(
                        f"resolutions.{resolution_id} must be exactly {expected['width']}×{expected['height']}"
                    )
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
            return {"document": deepcopy(self._snapshot), "revision": self._revision, "warnings": list(self._warnings)}

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
        if set(overrides) & {"width", "height"}:
            raise WorkbenchError("dimension_override", "Width and height overrides are not supported; choose sd, hd, or fhd")
        result = {
            "model": model_id,
            "resolution": resolution_id,
            "quality": quality_id,
            "width": resolution["width"],
            "height": resolution["height"],
            "steps": overrides.get("steps", model["quality"][quality_id]["steps"]),
            "cfg_scale": overrides.get("cfg_scale", model["cfg_scale"]),
            "sampler": overrides.get("sampler", model["sampler"]),
            "scheduler": overrides.get("scheduler", model["scheduler"]),
            "seed": overrides.get("seed"),
            "config_revision": snapshot["revision"],
        }
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
