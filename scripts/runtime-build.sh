#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DATA=${SDWB_DATA_DIR:-$ROOT/data}
REVISION=${1:-532d2c15c430a785a712b2d94281f04e733008a4}
. "$ROOT/scripts/common.sh"
ENGINE=$(sdwb_engine)

[ -d "$ROOT/stable-diffusion.cpp/ggml/src" ] || sdwb_die "source submodules are missing; run: git submodule update --init --recursive"
actual=$(git -C "$ROOT/stable-diffusion.cpp" rev-parse HEAD)
[ "$actual" = "$REVISION" ] || sdwb_die "source is at $actual, requested $REVISION; check out the requested tested revision first"
mkdir -p "$DATA/runtimes/releases" "$DATA/runtimes/staging" "$DATA/logs"

lock=$DATA/runtime-build.lock
if ! mkdir "$lock" 2>/dev/null; then
    sdwb_die "another runtime build is active ($lock)"
fi
staging=$DATA/runtimes/staging/$REVISION-$$
release=$DATA/runtimes/releases/$REVISION
cleanup() {
    rm -rf "$staging"
    rmdir "$lock" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM
[ ! -e "$DATA/.active-job" ] || sdwb_die "cannot replace a runtime while a generation job is active"

if [ -f "$release/manifest.json" ] && [ -f "$release/SHA256SUMS" ] && [ -x "$release/bin/sd-cli" ]; then
    (cd "$release" && sha256sum -c SHA256SUMS >/dev/null) || sdwb_die "existing runtime artifacts failed checksum verification; move the broken release aside and rebuild"
    echo "Verified host artifacts already exist for $REVISION"
else
    mkdir -p "$staging"
    image=localhost/sd-workbench-builder:${REVISION%${REVISION#????????}}
    "$ENGINE" build --pull -f "$ROOT/docker/Dockerfile.builder" \
        --build-arg "SD_REVISION=$REVISION" -t "$image" "$ROOT"
    "$ENGINE" run --rm -v "$staging:/export:rw" "$image" /export
    mkdir -p "$release"
    cp -a "$staging/." "$release/"
fi

"$ENGINE" build -f "$ROOT/docker/Dockerfile.runtime" -t localhost/sd-workbench-runtime:1 "$ROOT"
"$ENGINE" run --rm --entrypoint /bin/sh \
    -v "$release:/opt/sd:ro" localhost/sd-workbench-runtime:1 -c \
    'export LD_LIBRARY_PATH=/opt/sd/bin; ! ldd /opt/sd/bin/sd-cli 2>/dev/null | grep "not found"; /opt/sd/bin/sd-cli --help >/dev/null'

previous=
if [ -L "$DATA/runtimes/current" ]; then
    previous=$(readlink "$DATA/runtimes/current" || true)
fi
ln -sfn "releases/$REVISION" "$DATA/runtimes/current.new"
mv -T "$DATA/runtimes/current.new" "$DATA/runtimes/current"
[ -z "$previous" ] || printf '%s\n' "$previous" > "$DATA/runtimes/previous"
printf 'Activated sd.cpp %s\n' "$REVISION"
