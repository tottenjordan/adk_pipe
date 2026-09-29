FROM python:3.13-slim

# uv from the official distroless image (pinned, no curl|sh).
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

WORKDIR /app

# Dependency layer first for caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# App source (agent packages: trend_scout, creative_agent, interactive_creative,
# creative_eval, agent_common).
COPY . .
# `gcloud run deploy --source` uploads a zip that does NOT preserve symlinks, so the
# `agents/<name>` relative symlinks arrive as EMPTY dirs. ADK <2.10 only checked
# is_dir() and still listed them; ADK 2.10 also requires an `__init__.py`, so
# `/list-apps` came back empty. Re-create each entry as a symlink to the flat package.
RUN for d in agents/*/; do \
      n="$(basename "$d")"; \
      rm -rf "agents/$n" && ln -s "../$n" "agents/$n"; \
    done

ENV PORT=8080
# The entrypoint's `uv run` would otherwise sync the dev group (pytest/ruff/ty) at
# container start; the image is built with --no-dev, so keep runtime in sync with it.
ENV UV_NO_DEV=1
# Serve from `agents/` (relative symlinks to the three runnable agents) rather than `.`
# so `GET /list-apps` returns only those three instead of every top-level dir. The
# loader only puts `agents/` on sys.path, so PYTHONPATH=/app keeps each agent's
# cross-package imports (creative_eval, agent_common) resolvable. See agents/README.md.
ENV PYTHONPATH=/app
# Cloud Run sets $PORT; bind all interfaces. Same-origin frontend proxy means no CORS needed.
# The entrypoint appends --session_service_uri only when SESSION_SERVICE_URI is set
# (opt-in persistent Agent Engine sessions). See deployment/backend_entrypoint.sh.
CMD ["sh", "deployment/backend_entrypoint.sh"]
