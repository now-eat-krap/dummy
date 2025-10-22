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

###############################################################################
# (NEW) Download Qwen2.5-0.5B-Instruct GGUF model if missing
###############################################################################
MODEL_DIR="${REPO_DIR}/models"
MODEL_NAME="Qwen2.5-0.5B-Instruct-Q3_K_M.gguf"
# Hugging Face 직링크 (공개 리포). 302 리다이렉트가 있으므로 -L 사용.
MODEL_URL="https://huggingface.co/bartowski/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/${MODEL_NAME}?download=true"

echo "[logflow] ensuring GGUF model exists: ${MODEL_DIR}/${MODEL_NAME}"
mkdir -p "${MODEL_DIR}"
if [[ ! -s "${MODEL_DIR}/${MODEL_NAME}" ]]; then
  echo "[logflow] downloading ${MODEL_NAME} ..."
  # 부분 다운로드 파일로 받아 완료 시 원본 이름으로 교체 (중간 실패 대비)
  TMP_FILE="${MODEL_DIR}/${MODEL_NAME}.part"
  if ! curl -fL --progress-bar -o "${TMP_FILE}" "${MODEL_URL}"; then
    echo "[logflow] model download failed from Hugging Face." >&2
    rm -f "${TMP_FILE}" || true
    exit 1
  fi
  mv "${TMP_FILE}" "${MODEL_DIR}/${MODEL_NAME}"
  chmod 644 "${MODEL_DIR}/${MODEL_NAME}" || true
  echo "[logflow] model downloaded: ${MODEL_DIR}/${MODEL_NAME}"
else
  echo "[logflow] model already present, skipping download."
fi
###############################################################################

echo "[logflow] starting services (with --build)..."
"${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" up -d --build

HEALTH_URL="http://localhost:8080/ba.js"
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

<script src="http://localhost:8080/ba.js"
        data-site="logflow"
        data-endpoint="http://localhost:8080/ba"
        data-click="true" data-scroll="true" data-spa="true" data-hb="15"
        data-sample="1.0"
        defer></script>

Dashboard: http://localhost:8080/
EOF
