# One image for every Python process: Flask, the two web workers,
# the core pipeline supervisor, and the migration job. Compose picks
# the process with `command`.
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.17 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first so code edits do not reinstall torch and xgboost.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY webapp/ webapp/
COPY python-pipeline/ python-pipeline/
# Includes ml-model/models/ when it exists locally; the inference workers
# refuse to start without those trained artifacts.
COPY ml-model/ ml-model/

EXPOSE 5000
