import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    data: Path
    config_defaults: Path
    web: Path

    @classmethod
    def from_environment(cls):
        source = Path(__file__).resolve().parents[2]
        return cls(
            data=Path(os.environ.get("SDWB_DATA_DIR", source / "data")).resolve(),
            config_defaults=Path(os.environ.get("SDWB_CONFIG_DIR", source / "config")).resolve(),
            web=Path(os.environ.get("SDWB_WEB_DIR", source / "web")).resolve(),
        )

    def __post_init__(self):
        for name in ("config", "models", "downloads", "inputs", "outputs", "thumbnails", "logs", "runtimes"):
            (self.data / name).mkdir(parents=True, exist_ok=True)

    @property
    def downloads(self):
        return self.data / "downloads"

    @property
    def inputs(self):
        return self.data / "inputs"

    @property
    def outputs(self):
        return self.data / "outputs"

    @property
    def thumbnails(self):
        return self.data / "thumbnails"

    @property
    def logs(self):
        return self.data / "logs"

    @property
    def database(self):
        return self.data / "workbench.sqlite3"

    @property
    def generation_config(self):
        return self.data / "config" / "generation-presets.json"

    @property
    def settings(self):
        return self.data / "config" / "settings.json"

    @property
    def model_manifest(self):
        return self.config_defaults / "models.json"

    @property
    def runtime_current(self):
        return self.data / "runtimes" / "current"
