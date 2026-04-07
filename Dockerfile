FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY algos/pyproject.toml algos/uv.lock ./

ENV UV_SYSTEM_PYTHON=1

RUN uv sync --frozen --no-dev

COPY algos/ ./

EXPOSE 8080

CMD ["sh", "-c", "uv run uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]