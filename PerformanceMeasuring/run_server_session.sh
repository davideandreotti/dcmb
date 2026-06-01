#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
NETWORK_NAME="${NETWORK_NAME:-tlmsp_overlay}"
CONTAINER_NAME="${CONTAINER_NAME:-server}"
IMAGE_NAME="${IMAGE_NAME:-server}"
CERTS_DIR="${CERTS_DIR:-$PROJECT_ROOT/certs_external/server}"
BUILD_IMAGE="${BUILD_IMAGE:-0}"

log() {
    printf '[SERVER-RUN] %s\n' "$*"
}

cleanup() {
    set +e
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}

trap cleanup EXIT INT TERM

if [[ "$BUILD_IMAGE" == "1" ]]; then
    log "building server image"
    docker build -f "$SCRIPT_DIR/Dockerfile.server" -t "$IMAGE_NAME" "$SCRIPT_DIR"
fi

if [[ ! -f "$CERTS_DIR/cert.pem" || ! -f "$CERTS_DIR/key.pem" ]]; then
    log "missing cert.pem or key.pem in $CERTS_DIR"
    exit 1
fi

docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

log "starting server on network $NETWORK_NAME with certs from $CERTS_DIR"
exec docker run --rm -it \
    --name "$CONTAINER_NAME" \
    --network "$NETWORK_NAME" \
    -e CERTS_DIR=/certs \
    -v "$CERTS_DIR:/certs" \
    "$IMAGE_NAME" \
    python3 certs_server.py
