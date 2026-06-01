#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./deploy_middlebox_kubernetes.sh build
#   ./deploy_middlebox_kubernetes.sh apply
#   ./deploy_middlebox_kubernetes.sh logs
#   ./deploy_middlebox_kubernetes.sh exec
#   ./deploy_middlebox_kubernetes.sh delete
#
# Optional env vars:
#   NS=default
#   POD_NAME=middlebox_kubernetes
#   IMAGE_NAME=middleboxsgxshield:latest
#   KIND_CLUSTER_NAME=kind

NS="${NS:-default}"
POD_NAME="${POD_NAME:-middleboxkubernetes}"
IMAGE_NAME="${IMAGE_NAME:-middleboxsgxshield:latest}"
MANIFEST="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/middlebox_kubernetes.yaml"

cmd="${1:-apply}"

case "$cmd" in
  build)
    cd "$(dirname "$MANIFEST")"
    /home/bonsai/Desktop/MasterThesis/DC/go/bin/go mod tidy
    GOOS=linux GOARCH=amd64 /home/bonsai/Desktop/MasterThesis/DC/go/bin/go build -o middleboxsgx middleboxsgx.go middleboxHandler.go messageTypes.go
    docker build -f middleboxsgxshield.Dockerfile -t "$IMAGE_NAME" .
    ;;

  kind-load)
    KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-kind}"
    kind load docker-image "$IMAGE_NAME" --name "$KIND_CLUSTER_NAME"
    ;;

  apply)
    TMP_MANIFEST="$(mktemp)"
    sed "s|image: middleboxsgxshield:latest|image: ${IMAGE_NAME}|" "$MANIFEST" > "$TMP_MANIFEST"
    kubectl -n "$NS" apply -f "$TMP_MANIFEST"
    rm -f "$TMP_MANIFEST"
    kubectl -n "$NS" get pod "$POD_NAME" -o wide
    ;;

  logs)
    kubectl -n "$NS" logs -f "$POD_NAME"
    ;;

  exec)
    kubectl -n "$NS" exec -it "$POD_NAME" -- /bin/sh
    ;;

  delete)
    kubectl -n "$NS" delete -f "$MANIFEST"
    ;;

  check-sgx)
    kubectl -n "$NS" exec -it "$POD_NAME" -- ls -l /dev/sgx_enclave /dev/sgx_provision
    ;;

  *)
    echo "Unknown command: $cmd"
    echo "Valid commands: build | kind-load | apply | logs | exec | delete | check-sgx"
    exit 1
    ;;
esac
