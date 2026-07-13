FROM python:3.13-slim

# uv from the official distroless image (pinned, no curl|sh).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Dependency layer first for caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# App source (agent packages: trend_scout, creative_agent, interactive_creative,
# creative_eval, agent_common).
COPY . .

ENV PORT=8080
# Cloud Run sets $PORT; bind all interfaces. Same-origin frontend proxy means no CORS needed.
CMD ["sh", "-c", "uv run adk api_server . --host 0.0.0.0 --port ${PORT}"]
