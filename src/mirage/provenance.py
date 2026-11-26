import hashlib
import json
import os
from pathlib import Path
import subprocess

BUILD_INPUTS = (
    "docker/Dockerfile.builder",
    "docker/Dockerfile.builder.cuda",
    "docker/Dockerfile.runtime",
    "docker/Dockerfile.runtime.cuda",
    "docker/export-runtime.sh",
)
IGNORED_DIRECTORIES = {".git", "build", "__pycache__", ".pytest_cache"}
GIT_IMAGE = "docker.io/alpine/git:v2.54.1"


def git_output(arguments):
    return subprocess.run(
        arguments, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def git(root, *args):
    return git_output(["git", "-C", str(root), *args])


def container_git(checkout, engine, image=GIT_IMAGE):
    checkout = Path(checkout).resolve()
    arguments = [engine, "run", "--rm", "--network=none", "--user", f"{os.getuid()}:{os.getgid()}"]
    if engine == "podman":
        arguments += ["--userns=keep-id"]
    arguments += [
        "-e", "HOME=/tmp", "-e", "GIT_OPTIONAL_LOCKS=0",
        "-v", f"{checkout}:/checkout:ro", "-w", "/checkout", image,
    ]

    def invoke(root, *args):
        path = Path("/checkout") / Path(root).resolve().relative_to(checkout)
        return git_output([*arguments, "-C", str(path), *args])

    return invoke


def update_file_hash(digest, path, relative):
    digest.update(str(relative).encode())
    digest.update(b"\0")
    if path.is_symlink():
        digest.update(b"link\0")
        digest.update(os.readlink(path).encode())
        return
    if not path.is_file():
        return
    digest.update(oct(path.stat().st_mode & 0o777).encode())
    digest.update(b"\0")
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    digest.update(b"\0")


def tree_hash(root):
    digest = hashlib.sha256()
    for directory, directories, files in os.walk(root):
        directories[:] = sorted(name for name in directories if name not in IGNORED_DIRECTORIES)
        for name in list(directories):
            path = Path(directory) / name
            if path.is_symlink():
                update_file_hash(digest, path, path.relative_to(root))
                directories.remove(name)
        for name in sorted(files):
            path = Path(directory) / name
            if name != ".git":
                update_file_hash(digest, path, path.relative_to(root))
    return digest.hexdigest()


def inputs_hash(root):
    digest = hashlib.sha256()
    for name in BUILD_INPUTS:
        update_file_hash(digest, root / name, name)
    return digest.hexdigest()


def source_identity(root, build_environment=None, *, git_command=None):
    root = Path(root).resolve()
    source = root / "stable-diffusion.cpp"
    if not (source / "ggml" / "src").is_dir():
        raise RuntimeError(
            "stable-diffusion.cpp submodules are missing; run: git submodule update --init --recursive"
        )
    git_command = git_command or git
    commit = git_command(source, "rev-parse", "HEAD")
    status = git_command(
        root, "status", "--porcelain=v1", "--untracked-files=all", "--",
        "stable-diffusion.cpp", *BUILD_INPUTS,
    )
    source_hash = tree_hash(source)
    build_inputs = inputs_hash(root)
    environment = json.dumps(build_environment or {}, sort_keys=True, separators=(",", ":"))
    identity = hashlib.sha256(
        f"mirage-runtime\0{source_hash}\0{build_inputs}\0{environment}\0".encode()
    ).hexdigest()
    return {
        "sd_cpp_commit": commit,
        "build_id": f"{commit[:12]}-{identity[:32]}",
        "source_sha256": source_hash,
        "build_inputs_sha256": build_inputs,
        "build_environment": build_environment or {},
        "dirty": bool(status),
    }


def write_json(path, document):
    temporary = Path(path).with_suffix(".tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, path)
