# Build context: repository root. One image, two roles: api (runs migrations first) and worker.
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --package notifier
WORKDIR /app/services/notifier
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn notifier.api:create_app --factory --host 0.0.0.0 --port 8000"]
