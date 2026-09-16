FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv

FROM python:3.14.5-slim-trixie@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97
COPY --from=uv /uv /uvx /bin/
# The image already IS a pinned interpreter: python:3.14.5-slim-trixie, by digest.
# pyproject sets python-preference = "only-managed" so a developer checkout and CI
# resolve the same build, but that is a validation concern and must not reach the
# runtime image — left to apply here, uv downloads a second 34.4 MiB CPython and
# builds .venv against it, changing both the image footprint and the interpreter
# Cerberus actually runs on. Env overrides the pyproject setting, and persists into
# the final image so the runtime `uv run` honours it too.
ENV UV_PYTHON_PREFERENCE=only-system
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
RUN uv sync --frozen --no-dev --group build --no-install-project && uv pip install --no-deps --no-build-isolation .
COPY config/example.yaml /etc/cerberus/config.yaml
COPY entrypoint.sh /usr/local/bin/cerberus-entrypoint
RUN chmod 755 /usr/local/bin/cerberus-entrypoint
ENV CERBERUS_CONFIG=/etc/cerberus/config.yaml
EXPOSE 4101
ENTRYPOINT ["/usr/local/bin/cerberus-entrypoint"]
CMD ["uv", "run", "--no-sync", "cerberus", "serve"]
