#!/usr/bin/env bash
set -euo pipefail
/opt/ace/ace-server --models /opt/models --host 127.0.0.1 --port 8085 \
    --device CUDA0 --keep-loaded &
ace_pid=$!
python3 -m uvicorn gateway:from_env --factory --host 0.0.0.0 --port 8080 --no-access-log &
gateway_pid=$!
trap 'kill "$ace_pid" "$gateway_pid" 2>/dev/null || true' EXIT
trap 'exit 0' TERM INT
wait -n "$ace_pid" "$gateway_pid"
