import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from .provenance import git, tree_hash, update_file_hash, write_json
from .runtime_build import image_id, run

APP_INPUTS = ("pyproject.toml", "uv.lock", "LICENSE", "src", "config", "web", "docker/Dockerfile.app")
BASE_IMAGES = ("python:3.12-slim", "ghcr.io/astral-sh/uv:0.9.13")


def app_identity(root, base_images, *, git_command=None):
    root = Path(root).resolve()
    digest = hashlib.sha256()
    for name in APP_INPUTS:
        path = root / name
        if path.is_dir():
            digest.update(name.encode())
            digest.update(tree_hash(path).encode())
        else:
            update_file_hash(digest, path, name)
    environment = json.dumps(base_images, sort_keys=True, separators=(",", ":"))
    digest.update(environment.encode())
    build_id = f"app-{digest.hexdigest()[:32]}"
    git_command = git_command or git
    try:
        dirty = bool(git_command(root, "status", "--porcelain=v1", "--untracked-files=all", "--", *APP_INPUTS))
    except (OSError, RuntimeError, subprocess.CalledProcessError):
        dirty = False
    return {"app_build_id": build_id, "deployment_id": build_id, "app_source_sha256": digest.hexdigest(), "base_images": base_images, "dirty": dirty}


def build_app(root, data, engine, *, git_command=None):
    root = Path(root).resolve()
    data = Path(data).resolve()
    bases = {}
    for reference in BASE_IMAGES:
        run([engine, "pull", reference])
        bases[reference] = image_id(engine, reference)
    git_command = git_command or git
    identity = app_identity(root, bases, git_command=git_command)
    build_id = identity["app_build_id"]
    reference = f"localhost/mirage-app:{build_id}"
    destination = data / "releases" / "app" / build_id
    destination.mkdir(parents=True, exist_ok=True)
    manifest_path = destination / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        try:
            image_id(engine, manifest["app_image"]["id"])
        except RuntimeError:
            pass
        else:
            return manifest
    run([engine, "build", "--target", "production", "-f", root / "docker" / "Dockerfile.app", "-t", reference, root])
    immutable = image_id(engine, reference)
    manifest = {
        "schema_version": 2,
        **identity,
        "source_reference": git_command(root, "rev-parse", "HEAD") if (root / ".git").exists() else None,
        "app_image": {"reference": reference, "id": immutable},
    }
    write_json(manifest_path, manifest)
    print(json.dumps(manifest, indent=2), flush=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Build an immutable Mirage application image")
    parser.add_argument("root", type=Path)
    parser.add_argument("data", type=Path)
    parser.add_argument("engine", choices=("podman", "docker"))
    args = parser.parse_args()
    try:
        build_app(args.root, args.data, args.engine)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"mirage: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
