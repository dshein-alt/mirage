#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=1.0.0
mkdir -p "$ROOT/dist"
archive=$ROOT/dist/mirage-$VERSION.tar.gz
[ -d "$ROOT/stable-diffusion.cpp/ggml/src" ] || { echo "Initialize submodules before packaging" >&2; exit 1; }
tar -czf "$archive" \
    --exclude='./.git' --exclude='./data' --exclude='./dist' \
    --exclude='./.venv' --exclude='./build' \
    --exclude='./stable-diffusion.cpp/.git' \
    --exclude='./stable-diffusion.cpp/**/.git' \
    --exclude='*/__pycache__' --exclude='*/.pytest_cache' \
    -C "$ROOT" .
printf 'Created %s\n' "$archive"
