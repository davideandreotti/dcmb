#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CUSTOM_GOROOT="$PROJECT_ROOT/DC/go"
CUSTOM_GO="$CUSTOM_GOROOT/bin/go"
SGX_GOROOT="${SGX_GOROOT:-/home/bonsai/go-sgx-mod}"
SGX_GO="$SGX_GOROOT/bin/go"
ENCLAVE_KEY="${GRAMINE_ENCLAVE_KEY:-$HOME/.config/gramine/enclave-key.pem}"
BASE_PATH="$PATH"

if [[ ! -x "$CUSTOM_GO" ]]; then
    echo "Custom Go not found or not executable: $CUSTOM_GO" >&2
    echo "Build the DC/go submodule first, then rerun this script." >&2
    exit 1
fi

if [[ ! -x "$SGX_GO" ]]; then
    echo "SGX Go not found or not executable: $SGX_GO" >&2
    echo "Set SGX_GOROOT to the go-sgx-mod copy, or build /home/bonsai/go-sgx-mod first." >&2
    exit 1
fi

use_go_toolchain() {
    local goroot="$1"

    export GOROOT="$goroot"
    export PATH="$goroot/bin:$BASE_PATH"
    export GOTOOLCHAIN=local
}

use_go_toolchain "$CUSTOM_GOROOT"

require_command() {
    local name="$1"
    if ! command -v "$name" >/dev/null 2>&1; then
        echo "Required command not found in PATH: $name" >&2
        exit 1
    fi
}

common_tags=(trace)
case "${1:-all}" in
    all|middleboxHandler|handler|full|emptyHandler|empty|emptyhandler)
        ;;
    *)
        echo "Usage: $0 [all|middleboxHandler|emptyHandler]"
        exit 1
    ;;
esac

join_tags() {
    local IFS=,
    echo "$*"
}

common_build_args=(-tags "$(join_tags "${common_tags[@]}")")
certserver_build_args=(-tags "$(join_tags trace dcapverify)")
middlebox_build_args=(-tags "$(join_tags trace)")
middlebox_empty_build_args=(-tags "$(join_tags trace emptyhandler)")

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
    local name="$1"
    local template="$SCRIPT_DIR/${name}.manifest.template"
    local manifest="$SCRIPT_DIR/${name}.manifest"
    local output="$SCRIPT_DIR/${name}.manifest.sgx"

    require_command gramine-manifest
    require_command gramine-sgx-sign

    if [[ ! -f "$template" ]]; then
        echo "Missing Gramine manifest template: $template" >&2
        exit 1
    fi

    generate_or_find_enclave_key

    echo "[COMPILE] generating ${name}.manifest"
    gramine-manifest "$template" "$manifest"

    echo "[COMPILE] signing ${name}.manifest.sgx with $ENCLAVE_KEY"
    gramine-sgx-sign \
        --manifest "$manifest" \
        --key "$ENCLAVE_KEY" \
        --output "$output"

    echo "[COMPILE] OK: $output"
}

build_docker_images() {
    require_command docker

    echo "[COMPILE] building dcmiddlebox-worker:baseline"
    docker build \
        -f "$SCRIPT_DIR/dcmiddlebox.Dockerfile" \
        --build-arg MIDDLEBOX_BINARY=middlebox \
        -t dcmiddlebox-worker:baseline \
        "$PROJECT_ROOT"

    echo "[COMPILE] building dcmiddlebox-worker:emptyhandler"
    docker build \
        -f "$SCRIPT_DIR/dcmiddlebox.Dockerfile" \
        --build-arg MIDDLEBOX_BINARY=middlebox_emptyhandler \
        -t dcmiddlebox-worker:emptyhandler \
        "$PROJECT_ROOT"

    echo "[COMPILE] building dcmb_gateway:docker"
    docker build \
        -f "$SCRIPT_DIR/Dockerfile.gateway" \
        -t dcmb_gateway:docker \
        "$PROJECT_ROOT"

    echo "[COMPILE] OK: docker images dcmiddlebox-worker:baseline dcmiddlebox-worker:emptyhandler dcmb_gateway:docker"
}

(
    cd "$SCRIPT_DIR"
    use_go_toolchain "$CUSTOM_GOROOT"
    build "client" "cmd/client" "$SCRIPT_DIR/client" "${common_build_args[@]}"
    build "certserver" "cmd/certserver" "$SCRIPT_DIR/certserver" "${certserver_build_args[@]}"
    build "middlebox" "cmd/middlebox" "$SCRIPT_DIR/middlebox" "${middlebox_build_args[@]}"
    build "middlebox_emptyhandler" "cmd/middlebox" "$SCRIPT_DIR/middlebox_emptyhandler" "${middlebox_empty_build_args[@]}"
    build "middlebox_gateway" "cmd/gateway" "$SCRIPT_DIR/middlebox_gateway" "${common_build_args[@]}"

    use_go_toolchain "$SGX_GOROOT"
    build "middlebox_sgxgo" "cmd/middlebox" "$SCRIPT_DIR/middlebox_sgxgo" "${middlebox_build_args[@]}"

    use_go_toolchain "$CUSTOM_GOROOT"
    build_gramine_manifest "middlebox"
    build_gramine_manifest "middlebox_sgxgo"
    build_gramine_manifest "middlebox_emptyhandler"
    build_docker_images
)
