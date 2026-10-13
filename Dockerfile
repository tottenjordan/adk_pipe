# syntax=docker/dockerfile:1
# BuildKit syntax (cache mounts, COPY --link). Built by `cloudbuild.yaml` (buildx +
# registry layer cache); `docker build .` / `gcloud run deploy --source .` still work.

# ---- app source ----
# App source (agent packages: trend_scout, creative_agent, interactive_creative,
# creative_eval, agent_common), prepared in its own stage so the final image can take it
# with `COPY --link`: a source-only change then rebuilds just this small layer, without
# pulling and unpacking the ~1 GB dependency layer underneath it.
FROM python:3.13-slim AS src
WORKDIR /app
COPY . .
# `gcloud run deploy --source` uploads a zip that does NOT preserve symlinks, so the
# `agents/<name>` relative symlinks arrive as EMPTY dirs. ADK <2.10 only checked
# is_dir() and still listed them; ADK 2.10 also requires an `__init__.py`, so
# `/list-apps` came back empty. Re-create each entry as a symlink to the flat package.
RUN for d in agents/*/; do \
      n="$(basename "$d")"; \
      rm -rf "agents/$n" && ln -s "../$n" "agents/$n"; \
    done

# ---- runtime ----
FROM python:3.13-slim

# uv from the official distroless image (pinned, no curl|sh).
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

WORKDIR /app

# Dependency layer first: it only changes when pyproject.toml / uv.lock change, so a
# source-only change reuses it from the registry cache (and never re-pushes it). The uv
# cache lives in a BuildKit cache mount, not in the image (copy mode, since the mount is
# a different filesystem than the venv). `[tool.uv] package = false`: the project itself
# is never installed, so this is the whole sync (a second post-COPY `uv sync` would be a
# no-op that forces the dependency layer to be unpacked on every source change).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_LINK_MODE=copy uv sync --frozen --no-dev --no-install-project

COPY --link --from=src /app /app

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
# SESSION_SERVICE_URI (opt-in persistent Agent Engine sessions) is read from the
# environment inside async_app.py, not passed as a flag. See deployment/backend_entrypoint.sh.
CMD ["sh", "deployment/backend_entrypoint.sh"]
