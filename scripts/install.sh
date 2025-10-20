#!/usr/bin/env bash
set -euo pipefail

if ! command -v curl >/dev/null 2>&1; then
  echo "[logflow] curl is required for health checks." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "[logflow] docker is required but not installed." >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD=(docker-compose)
else
  echo "[logflow] docker compose plugin or binary is required." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${REPO_DIR}/docker/docker-compose.yml"

echo "[logflow] pulling images..."
"${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" pull --ignore-pull-failures

echo "[logflow] starting services (with --build)..."
"${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" up -d --build

HEALTH_URL="http://localhost:9000/ba.js"
echo "[logflow] waiting for ${HEALTH_URL} ..."
for attempt in $(seq 1 30); do
  if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
    echo "[logflow] stack is ready."
    break
  fi
  sleep 2
  if [[ "${attempt}" -eq 30 ]]; then
    echo "[logflow] health check failed after $((${attempt} * 2)) seconds." >&2
    exit 1
  fi
done

cat <<'EOF'

Logflow lightweight analytics stack is running.

Snippet (update the endpoint hostname if accessed remotely):

<script src="http://localhost:9000/ba.js"
        data-site="logflow"
        data-endpoint="http://localhost:9000/ba"
        data-click="true" data-scroll="true" data-spa="true" data-hb="15"
        data-sample="1.0"
        defer></script>

Dashboard: http://localhost:9000/
EOF
