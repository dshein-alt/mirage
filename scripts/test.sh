#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$ROOT/scripts/common.sh"
ENGINE=$(mirage_engine)
"$ENGINE" build --target development -f "$ROOT/docker/Dockerfile.app" -t localhost/mirage-test:1 "$ROOT"
"$ENGINE" run --rm localhost/mirage-test:1 uv run --locked --no-sync pytest -q
