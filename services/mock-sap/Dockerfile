# Build context: repository root
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --package mock-sap
EXPOSE 8000
CMD ["uvicorn", "mock_sap.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
