#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v gramine-manifest >/dev/null 2>&1; then
    echo "Error: gramine-manifest not found in PATH"
    exit 1
fi

if ! command -v gramine-sgx-sign >/dev/null 2>&1; then
    echo "Error: gramine-sgx-sign not found in PATH"
    exit 1
fi

if [ ! -f "middlebox.manifest.template" ]; then
    echo "Error: missing middlebox.manifest.template"
    exit 1
fi

if [ ! -f "enclave-key.pem" ]; then
    echo "Error: missing enclave-key.pem"
    exit 1
fi

gramine-manifest -Dlog_level=error middlebox.manifest.template middlebox.manifest

gramine-sgx-sign --with file -k enclave-key.pem -m middlebox.manifest -o middlebox.manifest.sgx -s middlebox.sig

gramine-sgx middlebox -reuse_dc=true

echo "OK: generated middlebox.manifest, middlebox.manifest.sgx, and middlebox.sig"
