#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$ROOT/scripts/common.sh"
ENGINE=$(sdwb_engine)
"$ENGINE" build --target production -f "$ROOT/docker/Dockerfile.app" -t localhost/sd-workbench-app:1 "$ROOT"
"$ENGINE" build -f "$ROOT/docker/Dockerfile.runtime" -t localhost/sd-workbench-runtime:1 "$ROOT"
