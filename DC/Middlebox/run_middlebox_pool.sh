#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
STACK_NAME="${STACK_NAME:-tlmsp}"
STACK_FILE="$SCRIPT_DIR/docker-stack.yml"
OVERLAY_NETWORK_NAME="${OVERLAY_NETWORK_NAME:-tlmsp_pool_overlay}"
CLIENT_CA_HOST_PATH="${CLIENT_CA_HOST_PATH:-$PROJECT_ROOT/certs_external/ca.crt}"

GATEWAY_REPLICAS="${GATEWAY_REPLICAS:-1}"
WARM_REPLICAS="${WARM_REPLICAS:-10}"
AUTH_REPLICAS="${AUTH_REPLICAS:-0}"
COLD_REPLICAS="${COLD_REPLICAS:-0}"
BUILD_IMAGES="${BUILD_IMAGES:-0}"

MIN_READY_OPERATORS="${MIN_READY_OPERATORS:-3}"
SCALE_UP_BY="${SCALE_UP_BY:-10}"
MAX_OPERATORS="${MAX_OPERATORS:-0}"
AUTOSCALE_PERIOD_SECONDS="${AUTOSCALE_PERIOD_SECONDS:-2}"
NO_READY_OPERATOR_POLICY="${NO_READY_OPERATOR_POLICY:-drop}"
NO_READY_OPERATOR_WAIT_SECONDS="${NO_READY_OPERATOR_WAIT_SECONDS:-0}"
OPERATOR_EXIT_AFTER_REQUEST="${OPERATOR_EXIT_AFTER_REQUEST:-true}"
OPERATOR_CONSUME_AFTER_REQUEST="${OPERATOR_CONSUME_AFTER_REQUEST:-false}"
POOL_STREAM_LOGS="${POOL_STREAM_LOGS:-1}"
POOL_CLEANUP_ON_EXIT="${POOL_CLEANUP_ON_EXIT:-1}"
TMP_STACK=""

log() {
    printf '[POOL] %s\n' "$*"
}

ensure_overlay_network() {
    local stack_label=""

    if docker network inspect "$OVERLAY_NETWORK_NAME" >/dev/null 2>&1; then
        stack_label="$(docker network inspect "$OVERLAY_NETWORK_NAME" --format '{{ index .Labels "com.docker.stack.namespace" }}' 2>/dev/null || true)"

        if [[ -n "$stack_label" ]]; then
            log "recreating stack-owned network $OVERLAY_NETWORK_NAME (label=$stack_label) as external overlay"
            docker network rm "$OVERLAY_NETWORK_NAME" >/dev/null 2>&1 || true
        else
            return 0
        fi
    fi

    log "creating overlay network $OVERLAY_NETWORK_NAME"
    docker network create --driver overlay --attachable "$OVERLAY_NETWORK_NAME" >/dev/null

    return 0
}

cleanup() {
    set +e
    log "scaling middlebox services to 0"
    docker service scale \
        "${STACK_NAME}_mb_gateway=0" \
        "${STACK_NAME}_mb_operator_warm=0" \
        "${STACK_NAME}_mb_operator_auth=0" \
        "${STACK_NAME}_mb_operator_cold=0" >/dev/null 2>&1 || true

    if [[ -n "${LOG_GATEWAY_PID:-}" ]]; then kill "$LOG_GATEWAY_PID" >/dev/null 2>&1 || true; fi
    if [[ -n "${LOG_WARM_PID:-}" ]]; then kill "$LOG_WARM_PID" >/dev/null 2>&1 || true; fi
    if [[ -n "${LOG_AUTH_PID:-}" ]]; then kill "$LOG_AUTH_PID" >/dev/null 2>&1 || true; fi
    if [[ -n "${TMP_STACK}" && -f "${TMP_STACK}" ]]; then rm -f "${TMP_STACK}" || true; fi
}

if [[ "$POOL_CLEANUP_ON_EXIT" == "1" ]]; then
    trap cleanup EXIT INT TERM
fi

if [[ "$BUILD_IMAGES" == "1" ]]; then
    log "building mb_gateway and mb_operator images"
    docker build -f "$SCRIPT_DIR/Dockerfile.gateway" -t mb_gateway:latest "$SCRIPT_DIR"
    docker build -f "$SCRIPT_DIR/Dockerfile.operator" -t mb_operator:latest "$SCRIPT_DIR"
fi

ensure_gateway_image_has_docker_cli() {
    if ! docker image inspect mb_gateway:latest >/dev/null 2>&1; then
        log "mb_gateway:latest not found, building gateway image"
        docker build -f "$SCRIPT_DIR/Dockerfile.gateway" -t mb_gateway:latest "$SCRIPT_DIR"
    fi

    if docker run --rm --entrypoint /bin/sh mb_gateway:latest -lc 'command -v docker >/dev/null'; then
        return
    fi

    log "mb_gateway:latest does not contain docker CLI, rebuilding gateway image"
    docker build --no-cache -f "$SCRIPT_DIR/Dockerfile.gateway" -t mb_gateway:latest "$SCRIPT_DIR"

    if docker run --rm --entrypoint /bin/sh mb_gateway:latest -lc 'command -v docker >/dev/null'; then
        return
    fi

    log "ERROR: rebuilt mb_gateway:latest still missing docker CLI"
    log "Run: BUILD_IMAGES=1 ./run_middlebox_pool.sh and share build logs"
    exit 1
}

ensure_gateway_image_has_docker_cli
ensure_overlay_network

log "deploying stack definition with MIN_READY=$MIN_READY_OPERATORS SCALE_UP_BY=$SCALE_UP_BY MAX=$MAX_OPERATORS"
export GATEWAY_REPLICAS
export WARM_REPLICAS
export STACK_NAME
export OVERLAY_NETWORK_NAME
export MIN_READY_OPERATORS
export SCALE_UP_BY
export MAX_OPERATORS
export AUTOSCALE_PERIOD_SECONDS
export NO_READY_OPERATOR_POLICY
export NO_READY_OPERATOR_WAIT_SECONDS
export OPERATOR_EXIT_AFTER_REQUEST
export OPERATOR_CONSUME_AFTER_REQUEST
export CLIENT_CA_HOST_PATH

TMP_STACK=$(mktemp /tmp/tlmsp-stack-XXXXXX.yml)
envsubst < "$STACK_FILE" > "$TMP_STACK"
docker stack deploy -c "$TMP_STACK" "$STACK_NAME" >/dev/null

log "scaling services gateway=$GATEWAY_REPLICAS warm=$WARM_REPLICAS auth=$AUTH_REPLICAS cold=$COLD_REPLICAS"
docker service scale \
    --detach=true \
    "${STACK_NAME}_mb_gateway=${GATEWAY_REPLICAS}" \
    "${STACK_NAME}_mb_operator_warm=${WARM_REPLICAS}" \
    "${STACK_NAME}_mb_operator_auth=${AUTH_REPLICAS}" \
    "${STACK_NAME}_mb_operator_cold=${COLD_REPLICAS}" >/dev/null

log "request-driven refill min_ready=$MIN_READY_OPERATORS scale_by=$SCALE_UP_BY max=$MAX_OPERATORS"
log "no_ready_operator_policy=$NO_READY_OPERATOR_POLICY wait=${NO_READY_OPERATOR_WAIT_SECONDS}s"
log "operator_exit_after_request=$OPERATOR_EXIT_AFTER_REQUEST"
log "operator_consume_after_request=$OPERATOR_CONSUME_AFTER_REQUEST"

log "overlay network available as $OVERLAY_NETWORK_NAME"
log "start the server separately, for example:"
log "docker run --rm -it --name server --network $OVERLAY_NETWORK_NAME -e CERTS_DIR=/certs -v $PROJECT_ROOT/certs_external/server:/certs server python3 certs_server.py"
log "start the client separately, for example:"
log "docker run --rm -it --name client --network $OVERLAY_NETWORK_NAME -v $CLIENT_CA_HOST_PATH:/certs/ca.crt:ro client ./client -id client -ca /certs/ca.crt -H \"Authorization : Bearer token\" https://mb_gateway:8443/function/init"

if [[ "$POOL_STREAM_LOGS" != "1" ]]; then
    log "pool deployed without log streaming"
    exit 0
fi

log "streaming gateway/operator logs; press Ctrl+C here to stop all middleboxes"

docker service logs -f --raw "${STACK_NAME}_mb_gateway" &
LOG_GATEWAY_PID=$!
docker service logs -f --raw "${STACK_NAME}_mb_operator_warm" &
LOG_WARM_PID=$!
docker service logs -f --raw "${STACK_NAME}_mb_operator_auth" &
LOG_AUTH_PID=$!

wait "$LOG_GATEWAY_PID" "$LOG_WARM_PID" "$LOG_AUTH_PID"
