#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DATA=${SDWB_DATA_DIR:-$ROOT/data}
PREFIX=${SDWB_PREFIX:-$HOME/.local/bin}
MODELS=${SDWB_MODELS:-}
IMPORT_SOURCE=
SKIP_BUILD=0
SKIP_MODELS=0
START=1
NONINTERACTIVE=0
ALLOW_NO_GPU=0
EDITING=0

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]
  --data-dir PATH          Persistent data directory (default: ./data)
  --prefix PATH            Command installation directory (default: ~/.local/bin)
  --models LIST            Comma-separated: zimage,moss,qwen,qwen-edit,all,none
  --import-models PATH     Import existing files from a models directory (no download)
  --runtime ENGINE         podman or docker
  --skip-build             Reuse existing images and host runtime artifacts
  --skip-models            Do not install models in this run
  --no-start               Stop the service after installation
  --non-interactive        Never prompt; --models or SDWB_MODELS sets the selection
  --allow-no-gpu           Install without /dev/dri (CPU/custom backend required later)

Re-run the installer to add previously unselected models. Existing models,
configuration, outputs, and jobs are never removed by installation.
EOF
}

while [ $# -gt 0 ]; do
    case $1 in
        --data-dir) DATA=$2; shift 2 ;;
        --prefix) PREFIX=$2; shift 2 ;;
        --models) MODELS=$2; NONINTERACTIVE=1; shift 2 ;;
        --import-models) IMPORT_SOURCE=$2; shift 2 ;;
        --runtime) SDWB_CONTAINER_ENGINE=$2; export SDWB_CONTAINER_ENGINE; shift 2 ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        --skip-models) SKIP_MODELS=1; MODELS=none; shift ;;
        --no-start) START=0; shift ;;
        --non-interactive) NONINTERACTIVE=1; shift ;;
        --allow-no-gpu) ALLOW_NO_GPU=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

. "$ROOT/scripts/common.sh"
ENGINE=$(sdwb_engine)
SOCKET=$(sdwb_socket "$ENGINE")
export SDWB_CONTAINER_ENGINE="$ENGINE"
export SDWB_DATA_DIR="$DATA"

for tool in git awk df stat cp ln readlink sha256sum; do
    command -v "$tool" >/dev/null 2>&1 || sdwb_die "required host tool is missing: $tool"
done
mkdir -p "$DATA"
DATA=$(CDPATH= cd -- "$DATA" && pwd)
[ -w "$DATA" ] || sdwb_die "data directory is not writable: $DATA"
if [ -n "$IMPORT_SOURCE" ]; then
    [ -d "$IMPORT_SOURCE" ] || sdwb_die "model import directory not found: $IMPORT_SOURCE"
    IMPORT_SOURCE=$(CDPATH= cd -- "$IMPORT_SOURCE" && pwd)
fi
if [ "$ALLOW_NO_GPU" -eq 0 ]; then
    set -- /dev/dri/renderD*
    [ -e "$1" ] || sdwb_die "no DRI render device found. Check /dev/dri permissions and Vulkan drivers, or use --allow-no-gpu"
fi
free=$(sdwb_free_bytes "$DATA")
[ "$free" -ge 8000000000 ] || sdwb_die "at least 8 GB free space is required before models; free: $free bytes"

installed_model() {
    case $1 in
        moss) [ -f "$DATA/models/moss/MOSS-Image-SFT-Q4_K.gguf" ] ;;
        qwen) [ -f "$DATA/models/qwen/qwen_image_2.1-Q4_K.gguf" ] ;;
        qwen-edit) [ -f "$DATA/models/qwen/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf" ] ;;
        zimage) [ -f "$DATA/models/zimage/z_image_turbo-Q4_K.gguf" ] ;;
    esac
}

select_models() {
    [ -n "$MODELS" ] || MODELS=zimage
    if [ "$NONINTERACTIVE" -eq 1 ] || [ ! -t 0 ]; then
        return
    fi
    dialog_command=
    if command -v dialog >/dev/null 2>&1; then
        dialog_command=dialog
    elif command -v whiptail >/dev/null 2>&1; then
        dialog_command=whiptail
    fi
    if [ -n "$dialog_command" ]; then
        tmp=$(mktemp)
        trap 'rm -f "$tmp"' EXIT HUP INT TERM
        moss_state=OFF; qwen_state=OFF; edit_state=OFF; zimage_state=ON
        installed_model moss && moss_state=ON
        installed_model qwen && qwen_state=ON
        installed_model qwen-edit && edit_state=ON
        installed_model zimage && zimage_state=ON
        if "$dialog_command" --title "SD Workbench models" --checklist \
            "Select models to install. Already installed items are preserved.\nRerun this installer later to add models." 18 76 4 \
            zimage "Z-Image Turbo — 6.7 GB" "$zimage_state" \
            qwen "Qwen-Image 2.1 — 9.9 GB" "$qwen_state" \
            qwen-edit "Qwen editing projector — 0.75 GB (also selects Qwen)" "$edit_state" \
            moss "MOSS-Image-SFT — 9.3 GB" "$moss_state" 2>"$tmp"; then
            MODELS=$(tr '\n' ' ' < "$tmp" | tr '"' ' ' | tr -s ' ' ',' | sed 's/^,//;s/,$//')
            [ -n "$MODELS" ] || MODELS=none
        else
            rm -f "$tmp"
            trap - EXIT HUP INT TERM
            sdwb_die "installation cancelled"
        fi
        rm -f "$tmp"
        trap - EXIT HUP INT TERM
    else
        echo "Interactive model selection (installed items are preserved):"
        echo "  1) Z-Image Turbo (6.7 GB)  2) Qwen-Image 2.1 (9.9 GB)"
        echo "  3) Qwen editing (+0.75 GB) 4) MOSS-Image-SFT (9.3 GB)"
        printf "Numbers to add, 'all', or 'none' [1]: "
        read -r answer
        MODELS=${answer:-1}
        old_ifs=$IFS; IFS=,
        set -- $MODELS
        IFS=$old_ifs
        MODELS=
        for item in "$@"; do
            case $item in
                1) item=zimage ;; 2) item=qwen ;; 3) item=qwen-edit ;; 4) item=moss ;;
            esac
            MODELS=${MODELS:+$MODELS,}$item
        done
    fi
}

select_models
case ",$MODELS," in *qwen-edit*) EDITING=1 ;; esac
case ",$MODELS," in *all*) MODELS=zimage,qwen,moss; EDITING=1 ;; esac
old_ifs=$IFS; IFS=,
for model in $MODELS; do
    case $model in zimage|qwen|moss|qwen-edit|none) ;; *) sdwb_die "unknown model selection: $model" ;; esac
done
IFS=$old_ifs

if [ ! -d "$ROOT/stable-diffusion.cpp/ggml/src" ]; then
    git -C "$ROOT" submodule update --init --recursive
fi
revision=$(git -C "$ROOT/stable-diffusion.cpp" rev-parse HEAD)
[ "$revision" = 532d2c15c430a785a712b2d94281f04e733008a4 ] || sdwb_die "unsupported sd.cpp revision: $revision; expected tested commit 532d2c1"

mkdir -p "$DATA/config" "$DATA/models" "$DATA/downloads" "$DATA/inputs" "$DATA/outputs" "$DATA/thumbnails" "$DATA/logs" "$DATA/runtimes"
[ -f "$DATA/config/generation-presets.json" ] || cp "$ROOT/config/generation-presets.default.json" "$DATA/config/generation-presets.json"
[ -f "$DATA/config/settings.json" ] || cp "$ROOT/config/settings.default.json" "$DATA/config/settings.json"

if [ "$SKIP_BUILD" -eq 0 ]; then
    "$ROOT/scripts/app-build.sh"
    SDWB_DATA_DIR="$DATA" "$ROOT/scripts/runtime-build.sh" "$revision"
fi
mkdir -p "$PREFIX"
ln -sfn "$ROOT/bin/sdwb" "$PREFIX/sdwb"

selected() {
    case ",$MODELS," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}

if [ -n "$IMPORT_SOURCE" ] && [ "$MODELS" != none ]; then
    import_models=
    for model in zimage qwen moss; do
        if selected "$model" || { [ "$EDITING" -eq 1 ] && [ "$model" = qwen ]; }; then
            import_models="$import_models $model"
        fi
    done
    # Run the importer with a temporary service container-free app invocation.
    # The source mount is read-only; valid files are hard-linked when possible.
    userns=
    [ "$ENGINE" != podman ] || userns=--userns=keep-id
    # shellcheck disable=SC2086
    "$ENGINE" run --rm --user "$(id -u):$(id -g)" $userns \
        -v "$DATA:/data:rw" -v "$IMPORT_SOURCE:/import:ro" \
        localhost/sd-workbench-app:1 python -m sd_workbench.importer /import $import_models \
        $([ "$EDITING" -eq 1 ] && printf %s --with-editing)
fi

started_for_models=0
if [ "$MODELS" != none ] && [ "$SKIP_MODELS" -eq 0 ]; then
    "$PREFIX/sdwb" start >/dev/null
    started_for_models=1
    for model in zimage qwen moss; do
        if selected "$model" || { [ "$EDITING" -eq 1 ] && [ "$model" = qwen ]; }; then
            if [ "$model" = qwen ] && [ "$EDITING" -eq 1 ]; then
                "$PREFIX/sdwb" models install qwen --with-editing --wait
            else
                "$PREFIX/sdwb" models install "$model" --wait
            fi
        fi
    done
fi

if [ "$ALLOW_NO_GPU" -eq 0 ]; then
    if [ "$started_for_models" -eq 0 ]; then
        "$PREFIX/sdwb" start >/dev/null
        started_for_models=1
    fi
    "$PREFIX/sdwb" runtime verify --devices
fi

if [ "$START" -eq 1 ]; then
    "$PREFIX/sdwb" start >/dev/null
    echo "SD Workbench is ready: http://127.0.0.1:8765"
elif [ "$started_for_models" -eq 1 ]; then
    "$PREFIX/sdwb" stop >/dev/null
fi
printf 'Command installed: %s\nPersistent data: %s\n' "$PREFIX/sdwb" "$DATA"
case ":$PATH:" in *":$PREFIX:"*) ;; *) echo "Add $PREFIX to PATH to use the sdwb command." ;; esac
