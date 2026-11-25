FROM ghcr.io/astral-sh/uv:0.9.13 AS uv

FROM python:3.12-slim AS production
COPY --from=uv /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    SDWB_DATA_DIR=/data SDWB_CONFIG_DIR=/app/config SDWB_WEB_DIR=/app/web
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable --compile-bytecode

COPY config/ ./config/
COPY web/ ./web/
COPY docker/ ./docker/

RUN mkdir -p /data && chown -R 1000:1000 /data /app
USER 1000:1000
EXPOSE 8765
CMD ["python", "-m", "sd_workbench.api"]

FROM production AS development
USER root
RUN uv sync --locked --dev --no-editable
COPY tests/ ./tests/
COPY install.sh ./install.sh
COPY bin/ ./bin/
COPY scripts/ ./scripts/
USER 1000:1000
CMD ["uv", "run", "--locked", "--no-sync", "pytest"]
