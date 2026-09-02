FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml ./
COPY src ./src
RUN uv sync --extra db --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# default: one-shot poll; compose overrides with --loop
CMD ["python", "-m", "thaqip_ingestion.main", "--pages", "3"]
