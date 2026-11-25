from pathlib import Path
from urllib.parse import quote

from .errors import WorkbenchError
from .util import read_json


class ModelRegistry:
    def __init__(self, manifest_path: Path, models_dir: Path):
        self.manifest_path = manifest_path
        self.models_dir = models_dir
        self.document = read_json(manifest_path)
        if self.document.get("schema_version") != 1:
            raise WorkbenchError("manifest_invalid", "Unsupported model manifest schema", 500)
        self.models = self.document["models"]
        for model_id, model in self.models.items():
            if not model.get("components"):
                raise WorkbenchError("manifest_invalid", f"Model {model_id} has no components", 500)
            for component in model["components"].values():
                path = Path(component["local_path"])
                if path.is_absolute() or ".." in path.parts or not component["repository"] or not component["remote_path"]:
                    raise WorkbenchError("manifest_invalid", f"Unsafe or incomplete component in {model_id}", 500)

    def get(self, model_id):
        if model_id not in self.models:
            raise WorkbenchError("model_unknown", f"Unknown model: {model_id}", 404)
        return self.models[model_id]

    def path(self, component):
        return self.models_dir.joinpath(*Path(component["local_path"]).parts)

    def url(self, component):
        repository = "/".join(quote(part, safe="") for part in component["repository"].split("/"))
        remote = "/".join(quote(part, safe="") for part in component["remote_path"].split("/"))
        return f"https://huggingface.co/{repository}/resolve/main/{remote}?download=true"

    def component_status(self, model_id, component):
        path = self.path(component)
        result = {key: value for key, value in component.items()}
        result.update({"id": model_id, "installed": False, "valid": False, "actual_bytes": None, "url": self.url(component)})
        if path.is_file():
            result["installed"] = True
            result["actual_bytes"] = path.stat().st_size
            result["valid"] = path.stat().st_size == component["bytes"]
        return result

    def status(self, model_id):
        model = self.get(model_id)
        components = [self.component_status(model_id, component) for component in model["components"].values()]
        required = [component for component in components if component["required"]]
        editing = [component for component in components if "edit" in component.get("required_for", [])]
        return {
            "id": model_id,
            "name": model["name"],
            "tasks": model["tasks"],
            "license": model["license"],
            "installed": all(component["valid"] for component in required),
            "editing_installed": all(component["valid"] for component in editing),
            "bytes_total": sum(component["bytes"] for component in required),
            "bytes_installed": sum(component["actual_bytes"] or 0 for component in required if component["valid"]),
            "components": components,
        }

    def all_status(self):
        return [self.status(model_id) for model_id in self.models]

    def verify(self, model_id, editing=False, checksum=False):
        from .util import file_sha256

        model = self.get(model_id)
        errors = []
        for component in model["components"].values():
            if not component["required"] and not (editing and "edit" in component.get("required_for", [])):
                continue
            path = self.path(component)
            if not path.is_file():
                errors.append(f"missing {component['local_path']}")
            elif path.stat().st_size != component["bytes"]:
                errors.append(f"size mismatch for {component['local_path']}: expected {component['bytes']}, got {path.stat().st_size}")
            elif checksum and file_sha256(path) != component["sha256"]:
                errors.append(f"SHA-256 mismatch for {component['local_path']}")
        if errors:
            raise WorkbenchError("model_incomplete", "; ".join(errors))
        return True
