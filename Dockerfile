FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first (the project itself is installed after, so edits don't refetch them).
COPY pyproject.toml uv.lock README.md ./
COPY src/graphwright/__init__.py src/graphwright/__init__.py
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["graphwright"]
