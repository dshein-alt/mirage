#!/bin/sh

sdwb_die() {
    echo "sdwb: $*" >&2
    exit 1
}

sdwb_engine() {
    if [ -n "${SDWB_CONTAINER_ENGINE:-}" ]; then
        command -v "$SDWB_CONTAINER_ENGINE" >/dev/null 2>&1 || sdwb_die "container engine not found: $SDWB_CONTAINER_ENGINE"
        printf '%s\n' "$SDWB_CONTAINER_ENGINE"
    elif command -v podman >/dev/null 2>&1; then
        printf '%s\n' podman
    elif command -v docker >/dev/null 2>&1; then
        printf '%s\n' docker
    else
        sdwb_die "Podman or Docker is required. Install one and retry."
    fi
}

sdwb_socket() {
    engine=$1
    if [ "$engine" = podman ]; then
        socket="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
        if [ ! -S "$socket" ] && command -v systemctl >/dev/null 2>&1; then
            systemctl --user enable --now podman.socket >/dev/null 2>&1 || true
        fi
    else
        socket=/var/run/docker.sock
    fi
    [ -S "$socket" ] || sdwb_die "container API socket not found: $socket. Enable the rootless Podman user socket or Docker service."
    [ -w "$socket" ] || sdwb_die "container API socket is not writable: $socket. Check rootless Podman or Docker group permissions."
    printf '%s\n' "$socket"
}

sdwb_free_bytes() {
    df -Pk "$1" | awk 'NR == 2 {print $4 * 1024}'
}
