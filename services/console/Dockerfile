# Build context is the repository root (see docker-compose.yml) because the
# console imports the P2W pricing engine from the ingestion source tree.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY services/console/pyproject.toml ./
COPY services/console/src ./src
RUN uv sync --no-dev

# thaqip_ingestion.p2w is pure-stdlib, so the source tree alone is enough.
# _bootstrap_p2w() in app.py adds /app/ingestion/src to sys.path.
COPY services/ingestion/src ./ingestion/src

ENV PATH="/app/.venv/bin:$PATH"

CMD ["uvicorn", "thaqip_console.app:app", "--host", "0.0.0.0", "--port", "8080"]
