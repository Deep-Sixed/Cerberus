FROM ghcr.io/astral-sh/uv:0.11.12 AS uv

FROM python:3.14.5-slim-trixie
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY config/example.yaml /etc/cerberus/config.yaml
COPY entrypoint.sh /usr/local/bin/cerberus-entrypoint
RUN chmod 755 /usr/local/bin/cerberus-entrypoint
ENV CERBERUS_CONFIG=/etc/cerberus/config.yaml
EXPOSE 4101
ENTRYPOINT ["/usr/local/bin/cerberus-entrypoint"]
CMD ["uv", "run", "--no-sync", "cerberus", "serve"]
