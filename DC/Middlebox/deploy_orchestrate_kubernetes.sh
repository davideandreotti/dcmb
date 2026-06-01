#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./deploy_orchestrate_kubernetes.sh build
#   ./deploy_orchestrate_kubernetes.sh kind-load
#   ./deploy_orchestrate_kubernetes.sh apply
#   ./deploy_orchestrate_kubernetes.sh status
#   ./deploy_orchestrate_kubernetes.sh check-sgx
#   ./deploy_orchestrate_kubernetes.sh logs-middlebox
#   ./deploy_orchestrate_kubernetes.sh delete

NS="${NS:-default}"
MANIFEST="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/orchestrate_kubernetes.yaml"
KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-kind}"

CLIENT_IMAGE="${CLIENT_IMAGE:-client:latest}"
SERVER_IMAGE="${SERVER_IMAGE:-server:latest}"
MIDDLEBOX_IMAGE="${MIDDLEBOX_IMAGE:-middleboxsgxshield:latest}"

MB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$MB_DIR/../.." && pwd)"
SERVER_DIR="$ROOT_DIR/PerformanceMeasuring"

cmd="${1:-apply}"

case "$cmd" in
  build)
    cd "$MB_DIR"
    go mod tidy
    GOOS=linux GOARCH=amd64 go build -o middleboxsgx middleboxsgx.go middleboxHandler.go messageTypes.go
    docker build -f middleboxsgxshield.Dockerfile -t "$MIDDLEBOX_IMAGE" .
    docker build -f Dockerfile.client -t "$CLIENT_IMAGE" .

    cd "$SERVER_DIR"
    docker build -f Dockerfile.server -t "$SERVER_IMAGE" .
    ;;

  kind-load)
    kind load docker-image "$CLIENT_IMAGE" --name "$KIND_CLUSTER_NAME"
    kind load docker-image "$SERVER_IMAGE" --name "$KIND_CLUSTER_NAME"
    kind load docker-image "$MIDDLEBOX_IMAGE" --name "$KIND_CLUSTER_NAME"
    ;;

  apply)
    TMP_MANIFEST="$(mktemp)"
    sed -e "s|image: client:latest|image: ${CLIENT_IMAGE}|" \
        -e "s|image: server:latest|image: ${SERVER_IMAGE}|" \
        -e "s|image: middleboxsgxshield:latest|image: ${MIDDLEBOX_IMAGE}|" \
        "$MANIFEST" > "$TMP_MANIFEST"

    kubectl -n "$NS" apply -f "$TMP_MANIFEST"
    rm -f "$TMP_MANIFEST"

    kubectl -n "$NS" rollout status deployment/server-kubernetes --timeout=240s
    kubectl -n "$NS" rollout status deployment/middlebox-kubernetes --timeout=240s
    kubectl -n "$NS" rollout status deployment/client-kubernetes --timeout=240s
    ;;

  status)
    kubectl -n "$NS" get deploy,pod,svc
    ;;

  check-sgx)
    kubectl -n "$NS" exec deploy/middlebox-kubernetes -- ls -l /dev/sgx_enclave /dev/sgx_provision
    ;;

  logs-middlebox)
    kubectl -n "$NS" logs -f deploy/middlebox-kubernetes
    ;;

  delete)
    kubectl -n "$NS" delete -f "$MANIFEST" --ignore-not-found=true
    ;;

  *)
    echo "Unknown command: $cmd"
    echo "Valid commands: build | kind-load | apply | status | check-sgx | logs-middlebox | delete"
    exit 1
    ;;
esac
