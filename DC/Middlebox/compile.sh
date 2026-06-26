#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CUSTOM_GOROOT="$PROJECT_ROOT/DC/go"
CUSTOM_GO="$CUSTOM_GOROOT/bin/go"
ENCLAVE_KEY="${GRAMINE_ENCLAVE_KEY:-$HOME/.config/gramine/enclave-key.pem}"

if [[ ! -x "$CUSTOM_GO" ]]; then
    echo "Custom Go not found or not executable: $CUSTOM_GO" >&2
    echo "Build the DC/go submodule first, then rerun this script." >&2
    exit 1
fi

export GOROOT="$CUSTOM_GOROOT"
export PATH="$CUSTOM_GOROOT/bin:$PATH"
export GOTOOLCHAIN=local

require_command() {
    local name="$1"
    if ! command -v "$name" >/dev/null 2>&1; then
        echo "Required command not found in PATH: $name" >&2
        exit 1
    fi
}

common_tags=(trace)
middlebox_tags=(trace)
case "${1:-middleboxHandler}" in
    middleboxHandler|handler|full)
        ;;
    emptyHandler|empty|emptyhandler)
        middlebox_tags+=(emptyhandler)
        ;;
    *)
        echo "Usage: $0 [middleboxHandler|emptyHandler]"
        exit 1
    ;;
esac

join_tags() {
    local IFS=,
    echo "$*"
}

common_build_args=(-tags "$(join_tags "${common_tags[@]}")")
middlebox_build_args=(-tags "$(join_tags "${middlebox_tags[@]}")")

build() {
    local name="$1"
    local package_dir="$2"
    local output_path="$3"
    shift 3

    echo "[COMPILE] building $name with $(go version)"
    (
        cd "$SCRIPT_DIR/$package_dir"
        go build -mod=mod "$@" -o "$output_path" .
    )
    echo "[COMPILE] OK: $output_path"
}

generate_or_find_enclave_key() {
    if [[ -f "$ENCLAVE_KEY" ]]; then
        return
    fi

    require_command gramine-sgx-gen-private-key

    echo "[COMPILE] generating Gramine enclave key: $ENCLAVE_KEY"
    mkdir -p "$(dirname "$ENCLAVE_KEY")"
    chmod 700 "$(dirname "$ENCLAVE_KEY")"
    gramine-sgx-gen-private-key "$ENCLAVE_KEY" >/dev/null
    chmod 600 "$ENCLAVE_KEY"
}

build_gramine_manifest() {
    require_command gramine-manifest
    require_command gramine-sgx-sign

    if [[ ! -f "$SCRIPT_DIR/middlebox.manifest.template" ]]; then
        echo "Missing Gramine manifest template: $SCRIPT_DIR/middlebox.manifest.template" >&2
        exit 1
    fi

    generate_or_find_enclave_key

    echo "[COMPILE] generating middlebox.manifest"
    gramine-manifest middlebox.manifest.template middlebox.manifest

    echo "[COMPILE] signing middlebox.manifest.sgx with $ENCLAVE_KEY"
    gramine-sgx-sign \
        --manifest middlebox.manifest \
        --key "$ENCLAVE_KEY" \
        --output middlebox.manifest.sgx

    echo "[COMPILE] OK: $SCRIPT_DIR/middlebox.manifest.sgx"
}

build_docker_images() {
    require_command docker

    echo "[COMPILE] building dcmiddlebox-worker:baseline"
    docker build \
        -f "$SCRIPT_DIR/dcmiddlebox.Dockerfile" \
        -t dcmiddlebox-worker:baseline \
        "$PROJECT_ROOT"

    echo "[COMPILE] building dcmb_gateway:docker"
    docker build \
        -f "$SCRIPT_DIR/Dockerfile.gateway" \
        -t dcmb_gateway:docker \
        "$PROJECT_ROOT"

    echo "[COMPILE] OK: docker images dcmiddlebox-worker:baseline dcmb_gateway:docker"
}

(
    cd "$SCRIPT_DIR"
    build "client" "cmd/client" "$SCRIPT_DIR/client" "${common_build_args[@]}"
    build "certserver" "cmd/certserver" "$SCRIPT_DIR/certserver" "${common_build_args[@]}"
    build "middlebox" "cmd/middlebox" "$SCRIPT_DIR/middlebox" "${middlebox_build_args[@]}"
    build "middlebox_gateway" "cmd/gateway" "$SCRIPT_DIR/middlebox_gateway" "${common_build_args[@]}"
    build_gramine_manifest
    build_docker_images
)
