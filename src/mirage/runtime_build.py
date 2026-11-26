import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .provenance import container_git, source_identity, write_json
from .util import file_sha256, read_json


RUNTIME_BACKENDS = {
    "vulkan": {
        "builder": "docker/Dockerfile.builder",
        "runtime": "docker/Dockerfile.runtime",
        "base_images": ("ubuntu:24.04",),
    },
    "cuda": {
        "builder": "docker/Dockerfile.builder.cuda",
        "runtime": "docker/Dockerfile.runtime.cuda",
        "base_images": (
            "docker.io/nvidia/cuda:12.8.1-devel-ubuntu24.04",
            "docker.io/nvidia/cuda:12.8.1-runtime-ubuntu24.04",
        ),
    },
}


def run(arguments, capture=False):
    result = subprocess.run(
        [str(value) for value in arguments], text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "")[-4000:] if capture else ""
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(map(str, arguments))}\n{detail}")
    return (result.stdout or "").strip() if capture else ""


def image_id(engine, reference):
    return run([engine, "image", "inspect", "--format", "{{.Id}}", reference], capture=True)


def verify_files(root, manifest):
    for relative, expected in manifest.get("files", {}).items():
        path = root / relative
        if not path.is_file() or path.stat().st_size != expected["bytes"]:
            raise RuntimeError(f"runtime artifact is missing or has the wrong size: {relative}")
        if file_sha256(path) != expected["sha256"]:
            raise RuntimeError(f"runtime artifact checksum mismatch: {relative}")


def current_runtime_backend(data):
    manifest = data / "runtimes" / "current" / "manifest.json"
    return read_json(manifest).get("backend", "vulkan") if manifest.is_file() else None


def build_runtime(root, data, engine, revision=None, activate=False, *, backend="vulkan", git_command=None):
    root = Path(root).resolve()
    data = Path(data).resolve()
    if backend not in RUNTIME_BACKENDS:
        raise RuntimeError(f"unsupported runtime backend: {backend}")
    if activate and current_runtime_backend(data) not in (None, backend):
        raise RuntimeError("use the installer to switch GPU backends")
    selection = RUNTIME_BACKENDS[backend]
    if not (root / "stable-diffusion.cpp" / "ggml" / "src").is_dir():
        raise RuntimeError("source submodules are missing; run: git submodule update --init --recursive")
    base_images = {}
    for base in selection["base_images"]:
        run([engine, "pull", base])
        base_images[base] = image_id(engine, base)
    identity = source_identity(root, {**base_images, "mirage_backend": backend}, git_command=git_command)
    if revision and identity["sd_cpp_commit"] != revision:
        raise RuntimeError(f"source is at {identity['sd_cpp_commit']}, requested {revision}")
    build_id = identity["build_id"]
    releases = data / "runtimes" / "releases"
    staging_root = data / "runtimes" / "staging"
    releases.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    (data / "logs").mkdir(parents=True, exist_ok=True)
    lock = data / "runtime-build.lock"
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise RuntimeError(f"another runtime build is active ({lock})") from error
    staging = staging_root / f"{build_id}-{os.getpid()}"
    release = releases / build_id
    try:
        if (data / ".active-job").exists():
            raise RuntimeError("cannot build or activate a runtime while a generation job is active")
        if release.exists():
            manifest = read_json(release / "manifest.json")
            if manifest.get("build_id") != build_id:
                raise RuntimeError(f"release identity mismatch: {release}")
            verify_files(release, manifest)
            print(f"Verified host artifacts already exist for {build_id}", flush=True)
        else:
            staging.mkdir()
            builder = f"localhost/mirage-builder:{build_id}"
            run([
                engine, "build", "--pull", "-f", root / selection["builder"],
                "--build-arg", f"SD_REVISION={identity['sd_cpp_commit']}", "-t", builder, root,
            ])
            run([engine, "run", "--rm", "-v", f"{staging}:/export:rw", builder, "/export"])
            manifest = read_json(staging / "manifest.json")
            verify_files(staging, manifest)

        manifest = read_json((release if release.exists() else staging) / "manifest.json")
        reference = f"localhost/mirage-runtime:{build_id}"
        immutable = manifest.get("runtime_image", {}).get("id")
        if immutable:
            try:
                immutable = image_id(engine, immutable)
            except RuntimeError:
                immutable = None
        if not immutable:
            run([engine, "build", "-f", root / selection["runtime"], "-t", reference, root])
            immutable = image_id(engine, reference)
        artifact_root = release if release.exists() else staging
        gpu_arguments = []
        if backend == "cuda":
            gpu_arguments = (
                ["--device", "nvidia.com/gpu=all", "--security-opt", "label=disable"]
                if engine == "podman" else ["--gpus", "all"]
            )
        run([
            engine, "run", "--rm", *gpu_arguments, "--entrypoint", "/bin/sh",
            "-v", f"{artifact_root}:/opt/sd:ro", immutable, "-c",
            "export LD_LIBRARY_PATH=/opt/sd/bin; ! ldd /opt/sd/bin/sd-cli 2>/dev/null | grep 'not found' && /opt/sd/bin/sd-cli --help >/dev/null",
        ])
        manifest.update({
            "schema_version": 2,
            "backend": backend,
            "revision": identity["sd_cpp_commit"],
            **identity,
            "runtime_image": {"reference": reference, "id": immutable},
        })
        write_json(artifact_root / "manifest.json", manifest)
        verify_files(artifact_root, manifest)
        if not release.exists():
            os.replace(staging, release)
        if activate:
            if current_runtime_backend(data) not in (None, backend):
                raise RuntimeError("use the installer to switch GPU backends")
            current = data / "runtimes" / "current"
            previous = os.readlink(current) if current.is_symlink() else None
            temporary = data / "runtimes" / "current.new"
            temporary.unlink(missing_ok=True)
            temporary.symlink_to(Path("releases") / build_id)
            os.replace(temporary, current)
            if previous and previous != f"releases/{build_id}":
                (data / "runtimes" / "previous").write_text(previous + "\n", encoding="utf-8")
        print(json.dumps({"build_id": build_id, "sd_cpp_commit": identity["sd_cpp_commit"], "runtime_image": manifest["runtime_image"], "activated": activate}, indent=2), flush=True)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        lock.rmdir()


def main():
    parser = argparse.ArgumentParser(description="Build a content-addressed Mirage sd.cpp runtime")
    parser.add_argument("root", type=Path)
    parser.add_argument("data", type=Path)
    parser.add_argument("engine", choices=("podman", "docker"))
    parser.add_argument("revision", nargs="?")
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--backend", choices=tuple(RUNTIME_BACKENDS))
    parser.add_argument("--container-git", action="store_true")
    args = parser.parse_args()
    try:
        git_command = container_git(args.root, args.engine) if args.container_git else None
        backend = args.backend or current_runtime_backend(args.data) or "vulkan"
        build_runtime(args.root, args.data, args.engine, args.revision, args.activate, backend=backend, git_command=git_command)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"mirage: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
