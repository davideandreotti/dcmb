#!/usr/bin/env bash
set -euo pipefail

PORT="${1:-8443}"
N="${2:-50}"

echo "iteration,ms"

for i in $(seq 1 "$N"); do
  name="mb-precreated-$i"

  start_ns=$(date +%s%N)

  docker start "$name" >/dev/null

  ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$name")

  until nc -z "$ip" "$PORT" >/dev/null 2>&1; do
    sleep 0.001
  done

  end_ns=$(date +%s%N)

  echo "$i,$(( (end_ns - start_ns) / 1000000 ))"

  docker rm -f "$name" >/dev/null 2>&1 || true
done
