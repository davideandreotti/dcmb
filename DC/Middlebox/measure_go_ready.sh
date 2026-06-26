#!/usr/bin/env bash
set -euo pipefail

BIN="${1:-./middlebox}"
PORT="${2:-8443}"
N="${3:-100}"

echo "iteration,ms"

for i in $(seq 1 "$N"); do
  start_ns=$(date +%s%N)

  "$BIN" >/tmp/middlebox-$i.log 2>&1 &
  pid=$!

  cleanup() {
    kill "$pid" >/dev/null 2>&1 || true
    wait "$pid" >/dev/null 2>&1 || true
  }

  until nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      echo "process exited before port became ready; log:"
      cat /tmp/middlebox-$i.log
      exit 1
    fi
    sleep 0.001
  done

  end_ns=$(date +%s%N)

  echo "$i,$(( (end_ns - start_ns) / 1000000 ))"

  cleanup
  sleep 0.05
done
