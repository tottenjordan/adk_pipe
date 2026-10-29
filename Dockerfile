# syntax=docker/dockerfile:1
# BuildKit syntax (cache mounts, COPY --link). Built by `cloudbuild.yaml` (buildx +
# registry layer cache); `docker build .` / `gcloud run deploy --source .` still work.

# ---- app source ----
# App source (agents/: trend_scout, creative_agent, interactive_creative; libs/:
# creative_eval, agent_common; runserver/, deployment/), prepared in its own stage so the
# final image can take it with `COPY --link`: a source-only change then rebuilds just this
# small layer, without pulling and unpacking the ~1 GB dependency layer underneath it.
FROM python:3.13-slim AS src
WORKDIR /app
COPY . .

# ---- runtime ----
FROM python:3.13-slim

# uv from the official distroless image (pinned, no curl|sh).
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

WORKDIR /app

# Dependency layer first: it only changes when pyproject.toml / uv.lock change, so a
# source-only change reuses it from the registry cache (and never re-pushes it). The uv
# cache lives in a BuildKit cache mount, not in the image (copy mode, since the mount is
# a different filesystem than the venv). `--no-install-project`: the project itself is
# never installed (locally it is an editable install, `[tool.uv] package = true`; here
# PYTHONPATH below does that job), so this is the whole sync (a post-COPY `uv sync`
# would force the dependency layer to be unpacked on every source change).
# Note: `imageio-ffmpeg` (in main dependencies) ships a self-contained static
# `ffmpeg` binary inside `.venv` (`imageio_ffmpeg.get_ffmpeg_exe()`), so Phase 3
# video conform works out-of-the-box without `apt-get install ffmpeg`.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_LINK_MODE=copy uv sync --frozen --no-dev --no-install-project

COPY --link --from=src /app /app

ENV PORT=8080
# The entrypoint's `uv run` would otherwise sync the dev group (pytest/ruff/ty) at
# container start; the image is built with --no-dev, so keep runtime in sync with it.
ENV UV_NO_DEV=1
# Never sync at container start: with `package = true` the entrypoint's `uv run` would
# otherwise build and install the project (fetching hatchling from PyPI) on every boot.
# The venv is complete from the build-time sync above.
ENV UV_NO_SYNC=1
# async_app serves `agents/` (the three runnable agents, real dirs), so `GET /list-apps`
# returns exactly those. The project is not installed here (--no-install-project), so
# there is no editable-install .pth: PYTHONPATH lists the roots instead. /app for
# runserver + deployment, /app/agents for the agents (ADK's loader adds it too, but
# runserver imports agents directly), /app/libs for agent_common + creative_eval.
ENV PYTHONPATH=/app:/app/agents:/app/libs
# Cloud Run sets $PORT; bind all interfaces. Same-origin frontend proxy means no CORS needed.
# SESSION_SERVICE_URI (opt-in persistent Agent Engine sessions) is read from the
# environment inside async_app.py, not passed as a flag. See deployment/backend_entrypoint.sh.
CMD ["sh", "deployment/backend_entrypoint.sh"]
