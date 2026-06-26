#!/usr/bin/env bash
set -euo pipefail

IMAGE="${1:-dcmiddlebox-worker:baseline}"
PORT="${2:-8443}"
N="${3:-100}"

echo "iteration,ms"

for i in $(seq 1 "$N"); do
  start_ns=$(date +%s%N)

  cid=$(docker run -d \
    -v /home/bonsai/dcmb/certs_external:/home/bonsai/dcmb/certs_external:ro \
    -p 127.0.0.1::"$PORT" \
    "$IMAGE")

  host_port=$(docker inspect -f "{{(index (index .NetworkSettings.Ports \"${PORT}/tcp\") 0).HostPort}}" "$cid")

  until nc -z 127.0.0.1 "$host_port" >/dev/null 2>&1; do
    sleep 0.001
  done

  end_ns=$(date +%s%N)

  echo "$i,$(( (end_ns - start_ns) / 1000000 ))"

  docker rm -f "$cid" >/dev/null 2>&1 || true
done
