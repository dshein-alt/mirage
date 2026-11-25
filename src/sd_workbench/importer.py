import argparse
import os
from pathlib import Path
import shutil

from .errors import WorkbenchError
from .paths import Paths
from .manifest import ModelRegistry
from .util import file_sha256


def import_models(registry, source, model_ids, editing=False, checksum=False):
    source = source.resolve()
    imported = []
    for model_id in model_ids:
        model = registry.get(model_id)
        for component in model["components"].values():
            if not component["required"] and not (editing and "edit" in component.get("required_for", [])):
                continue
            source_path = source.joinpath(*Path(component["local_path"]).parts)
            destination = registry.path(component)
            if destination.is_file() and destination.stat().st_size == component["bytes"]:
                continue
            if not source_path.is_file():
                raise WorkbenchError("import_missing", f"Local component is missing: {source_path}")
            if source_path.stat().st_size != component["bytes"]:
                raise WorkbenchError("import_invalid", f"Wrong size for {source_path}")
            if checksum and file_sha256(source_path) != component["sha256"]:
                raise WorkbenchError("import_invalid", f"Wrong SHA-256 for {source_path}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.import-{os.getpid()}")
            try:
                try:
                    os.link(source_path, temporary)
                except OSError:
                    shutil.copy2(source_path, temporary)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            imported.append(component["local_path"])
    return imported


def main():
    parser = argparse.ArgumentParser(description="Import already-downloaded model files without network access")
    parser.add_argument("source", type=Path)
    parser.add_argument("models", nargs="+", choices=("moss", "qwen", "zimage"))
    parser.add_argument("--with-editing", action="store_true")
    parser.add_argument("--checksum", action="store_true")
    args = parser.parse_args()
    paths = Paths.from_environment()
    registry = ModelRegistry(paths.model_manifest, paths.data / "models")
    imported = import_models(registry, args.source, args.models, args.with_editing, args.checksum)
    print(f"Imported {len(imported)} components; no downloads were performed")


if __name__ == "__main__":
    main()
