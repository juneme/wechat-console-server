#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

include_temp=false

usage() {
  cat <<'EOF'
Usage: bash rotate-api-keys.sh [--include-temp]

Rotate AI_API_KEY and PUBLISH_API_KEY. Add --include-temp to rotate
TEMP_API_KEY at the same time. Keys are never printed by this command.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --include-temp)
      include_temp=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ -f .env ]] || { echo "ERROR: .env not found" >&2; exit 1; }
[[ -t 0 ]] || {
  echo "ERROR: key rotation requires an interactive terminal" >&2
  exit 1
}
command -v docker >/dev/null 2>&1 || {
  echo "ERROR: Docker is not installed" >&2
  exit 1
}
docker compose version >/dev/null 2>&1 || {
  echo "ERROR: Docker Compose plugin is unavailable" >&2
  exit 1
}

echo "This will invalidate the current image-upload and draft API keys."
if [[ "$include_temp" == true ]]; then
  echo "The temporary-image API key will also be invalidated."
fi
read -r -p "Type ROTATE to continue: " confirmation
[[ "$confirmation" == "ROTATE" ]] || { echo "Cancelled."; exit 1; }

random_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    tr -d '-' </proc/sys/kernel/random/uuid
  fi
}

set_env_value() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s#^${key}=.*#${key}=${value}#" .env
  else
    printf '\n%s=%s\n' "$key" "$value" >>.env
  fi
}

backup_file="$(mktemp ./.env.key-rotation.XXXXXX)"
cp .env "$backup_file"
chmod 600 "$backup_file" .env
completed=false

rollback() {
  if [[ "$completed" != true ]]; then
    echo "Rotation failed; restoring the previous .env." >&2
    cp "$backup_file" .env
    chmod 600 .env
    docker compose up -d --force-recreate uploader >/dev/null 2>&1 || true
  fi
  rm -f "$backup_file"
}
trap rollback EXIT

set_env_value AI_API_KEY "$(random_hex)"
set_env_value PUBLISH_API_KEY "$(random_hex)"
if [[ "$include_temp" == true ]]; then
  set_env_value TEMP_API_KEY "$(random_hex)"
fi

docker compose up -d --force-recreate uploader

for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8791/healthz >/dev/null 2>&1; then
    completed=true
    echo "API keys rotated and the console is healthy."
    echo "Use show-client-config.sh separately when a client needs the new values."
    exit 0
  fi
  sleep 2
done

echo "ERROR: the console did not become healthy after key rotation" >&2
exit 1
