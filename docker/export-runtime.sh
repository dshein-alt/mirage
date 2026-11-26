#!/bin/sh
set -eu

export_dir=${1:-/export}
[ -n "$SD_REVISION" ] || { echo "SD_REVISION is empty" >&2; exit 1; }
[ -x /sd.cpp/build/bin/sd-cli ] || { echo "sd-cli was not built" >&2; exit 1; }
[ -x /sd.cpp/build/bin/sd-server ] || { echo "sd-server was not built" >&2; exit 1; }

mkdir -p "$export_dir/bin"
cp -a /sd.cpp/build/bin/. "$export_dir/bin/"

if [ "${MIRAGE_RUNTIME_BACKEND:-vulkan}" = cuda ] && [ -f /usr/local/cuda/lib64/stubs/libcuda.so ]; then
    mkdir -p /tmp/mirage-cuda-stubs
    ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /tmp/mirage-cuda-stubs/libcuda.so.1
    LD_LIBRARY_PATH=/tmp/mirage-cuda-stubs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
    export LD_LIBRARY_PATH
fi

for binary in "$export_dir"/bin/*; do
    [ -f "$binary" ] || continue
    if ldd "$binary" 2>/dev/null | grep 'not found' >&2; then
        echo "Unresolved shared library in $binary" >&2
        exit 1
    fi
done

python3 - "$export_dir" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

root = Path(sys.argv[1])
files = {}
for path in sorted((root / "bin").rglob("*")):
    if path.is_file() and not path.is_symlink():
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        files[str(path.relative_to(root))] = {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}
(root / "SHA256SUMS").write_text("".join(f"{item['sha256']}  {name}\n" for name, item in files.items()), encoding="utf-8")
version = subprocess.run(
    [str(root / "bin/sd-cli"), "--help"], text=True,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True,
    env={**os.environ, "LD_LIBRARY_PATH": str(root / "bin") + (":" + os.environ["LD_LIBRARY_PATH"] if os.environ.get("LD_LIBRARY_PATH") else "")}
).stdout.splitlines()[0]
manifest = {
    "schema_version": 1,
    "revision": os.environ["SD_REVISION"],
    "version": version,
    "arch": platform.machine(),
    "backend": os.environ.get("MIRAGE_RUNTIME_BACKEND", "vulkan"),
    "builder_base": os.environ.get("BUILDER_BASE", "ubuntu:24.04"),
    "runtime_base": os.environ.get("RUNTIME_BASE", "ubuntu:24.04"),
    "cmake_flags": os.environ["CMAKE_FLAGS"],
    "files": files,
}
(root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
PY
