#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DATA=${MIRAGE_DATA_DIR:-$ROOT/data}
. "$ROOT/scripts/common.sh"
ENGINE=$(mirage_engine)
PYTHONPATH=$ROOT/src${PYTHONPATH:+:$PYTHONPATH}
export PYTHONPATH
exec python3 -m mirage.runtime_build "$ROOT" "$DATA" "$ENGINE" "$@"
