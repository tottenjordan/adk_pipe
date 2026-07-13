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
# Serve from `agents/` (relative symlinks to the three runnable agents) rather than `.`
# so `GET /list-apps` returns only those three instead of every top-level dir. The
# loader only puts `agents/` on sys.path, so PYTHONPATH=/app keeps each agent's
# cross-package imports (creative_eval, agent_common) resolvable. See agents/README.md.
ENV PYTHONPATH=/app
# Cloud Run sets $PORT; bind all interfaces. Same-origin frontend proxy means no CORS needed.
CMD ["sh", "-c", "uv run adk api_server agents --host 0.0.0.0 --port ${PORT}"]
