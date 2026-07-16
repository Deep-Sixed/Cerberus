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

load_secret METAROUTER_API_TOKEN /run/secrets/metarouter_api_token
load_secret OPENROUTER_API_KEY /run/secrets/openrouter_api_key

exec "$@"
