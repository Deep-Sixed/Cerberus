#!/bin/sh
set -eu

load_secret() {
  variable_name="$1"
  secret_path="$2"
  eval "current_value=\${$variable_name-}"
  if [ -z "$current_value" ] && [ -r "$secret_path" ]; then
    value="$(cat "$secret_path")"
    export "$variable_name=$value"
  fi
}

load_secret CERBERUS_API_TOKEN /run/secrets/cerberus_api_token
load_secret CERBERUS_ADMIN_TOKEN /run/secrets/cerberus_admin_token
load_secret CB_KEY_DEV /run/secrets/cb_key_dev
load_secret CB_KEY_WORKER /run/secrets/cb_key_worker
load_secret CERBERUS_FUSION_WORKER_TOKEN /run/secrets/cerberus_fusion_worker_token
load_secret OPENROUTER_API_KEY /run/secrets/openrouter_api_key

exec "$@"
