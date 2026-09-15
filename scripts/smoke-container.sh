#!/bin/sh
set -eu

IMAGE="${1:-cerberus:ci}"
CONTAINER_NAME="cerberus-smoke-test-$$"

cleanup() {
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# Run container with synthetic validation placeholders; distinct credentials required
docker run -d --name "$CONTAINER_NAME" \
  -e CB_KEY_DEV=smoke-dev-credential \
  -e CERBERUS_ADMIN_TOKEN=smoke-admin-credential \
  -e CERBERUS_API_TOKEN=smoke-api-credential \
  -e OPENROUTER_API_KEY=smoke-openrouter-credential \
  "$IMAGE" >/dev/null

# Verify the container starts and serves the /health endpoint
attempts=0
max_attempts=30

while [ "$attempts" -lt "$max_attempts" ]; do
  if docker exec "$CONTAINER_NAME" /app/.venv/bin/python -c \
    "import urllib.request, json; res = urllib.request.urlopen('http://127.0.0.1:4101/health', timeout=2); data = json.loads(res.read()); assert data.get('status') == 'ok'" 2>/dev/null; then
    echo "Container runtime smoke test passed ($IMAGE is healthy)."
    exit 0
  fi

  status="$(docker inspect --format='{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null || echo "missing")"
  if [ "$status" = "exited" ] || [ "$status" = "dead" ]; then
    echo "Container exited unexpectedly:" >&2
    docker logs "$CONTAINER_NAME" >&2
    exit 1
  fi

  attempts=$((attempts + 1))
  sleep 1
done

echo "Timed out waiting for container $IMAGE to serve /health" >&2
docker logs "$CONTAINER_NAME" >&2
exit 1
