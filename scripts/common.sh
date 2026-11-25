#!/bin/sh

mirage_die() {
    echo "mirage: $*" >&2
    exit 1
}

mirage_engine() {
    if [ -n "${MIRAGE_CONTAINER_ENGINE:-}" ]; then
        command -v "$MIRAGE_CONTAINER_ENGINE" >/dev/null 2>&1 || mirage_die "container engine not found: $MIRAGE_CONTAINER_ENGINE"
        printf '%s\n' "$MIRAGE_CONTAINER_ENGINE"
    elif command -v podman >/dev/null 2>&1; then
        printf '%s\n' podman
    elif command -v docker >/dev/null 2>&1; then
        printf '%s\n' docker
    else
        mirage_die "Podman or Docker is required. Install one and retry."
    fi
}

mirage_socket() {
    engine=$1
    if [ "$engine" = podman ]; then
        socket="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
        if [ ! -S "$socket" ] && command -v systemctl >/dev/null 2>&1; then
            systemctl --user enable --now podman.socket >/dev/null 2>&1 || true
        fi
    else
        socket=/var/run/docker.sock
    fi
    [ -S "$socket" ] || mirage_die "container API socket not found: $socket. Enable the rootless Podman user socket or Docker service."
    [ -w "$socket" ] || mirage_die "container API socket is not writable: $socket. Check rootless Podman or Docker group permissions."
    printf '%s\n' "$socket"
}

mirage_free_bytes() {
    df -Pk "$1" | awk 'NR == 2 {print $4 * 1024}'
}
