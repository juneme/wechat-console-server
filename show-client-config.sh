#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

show_secrets=false
console_url=""

usage() {
  cat <<'EOF'
Usage: bash show-client-config.sh [--url http://server:8787] [--show-secrets]

Without --show-secrets, only configuration readiness is shown. Revealing the
client configuration requires an interactive confirmation and should be done
only in a private terminal.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)
      [[ $# -ge 2 ]] || { echo "ERROR: --url requires a value" >&2; exit 2; }
      console_url="$2"
      shift 2
      ;;
    --show-secrets)
      show_secrets=true
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

[[ -f .env ]] || { echo "ERROR: .env not found; run install.sh first" >&2; exit 1; }

read_env() {
  sed -n "s/^$1=//p" .env | tail -n 1 | tr -d '\r'
}

read_database_key() {
  local column="$1"
  docker compose exec -T uploader python - "$column" <<'PY'
import sys

from app.config import get_settings
from app.credentials import CredentialCipher
from app.database import AssetStore

settings = get_settings()
store = AssetStore(settings.database_path)
row = store.get_service_credentials()
if row:
    cipher = CredentialCipher.create(
        secret=settings.credentials_encryption_key,
        key_path=settings.database_path.parent / ".wechat-credentials.key",
    )
    print(cipher.decrypt(row[sys.argv[1]]))
PY
}

if [[ -z "$console_url" ]]; then
  console_url="$(read_env PUBLIC_BASE_URL)"
fi
[[ "$console_url" =~ ^https?://[^[:space:]]+$ ]] || {
  echo "ERROR: provide --url with the console root URL, for example http://SERVER_IP:8787" >&2
  exit 1
}
console_url="${console_url%/}"

image_key="$(read_env AI_API_KEY)"
publish_key="$(read_env PUBLISH_API_KEY)"
if [[ -z "$image_key" || -z "$publish_key" ]]; then
  command -v docker >/dev/null 2>&1 || {
    echo "ERROR: Docker is required to read initialized database keys" >&2
    exit 1
  }
  docker compose version >/dev/null 2>&1 || {
    echo "ERROR: Docker Compose plugin is unavailable" >&2
    exit 1
  }
  [[ -n "$image_key" ]] || image_key="$(read_database_key ai_api_key_ciphertext)"
  [[ -n "$publish_key" ]] || publish_key="$(read_database_key publish_api_key_ciphertext)"
fi
[[ -n "$image_key" ]] || { echo "ERROR: AI_API_KEY is not configured" >&2; exit 1; }
[[ -n "$publish_key" ]] || { echo "ERROR: PUBLISH_API_KEY is not configured" >&2; exit 1; }

echo "Console URL: $console_url"
echo "Image API key: configured"
echo "Publish API key: configured"

if [[ "$show_secrets" != true ]]; then
  echo
  echo "Run again with --show-secrets in a private terminal to reveal the Skill configuration."
  exit 0
fi

[[ -t 0 ]] || {
  echo "ERROR: secret output requires an interactive terminal" >&2
  exit 1
}

echo
echo "WARNING: the next output grants image-upload and draft-creation access."
read -r -p "Type SHOW to continue: " confirmation
[[ "$confirmation" == "SHOW" ]] || { echo "Cancelled."; exit 1; }

cat <<EOF

WECHAT_CONSOLE_URL=$console_url
WECHAT_IMAGE_API_KEY=$image_key
WECHAT_PUBLISH_API_KEY=$publish_key
EOF
