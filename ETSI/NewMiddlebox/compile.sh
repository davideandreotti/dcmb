#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
go="$REPO_ROOT/DC/go/bin/go"

if [[ ! -x "$go" ]]; then
    echo "Repository Go toolchain not found or not executable: $go" >&2
    exit 1
fi

"$go" build -o client client.go
"$go" build -o listener_empty listener.go emptyHandler.go
"$go" build -o listener listener.go middleboxHandler.go messageTypes.go
