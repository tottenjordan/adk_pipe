# syntax=docker/dockerfile:1
# One image for both CRF Cloud Run functions: creative-trawler-crf (orchestrator,
# FUNCTION_TARGET=crf_entrypoint, a CloudEvent) and creative-worker-crf (worker:
# FUNCTION_TARGET=agent_worker_task + FUNCTION_SIGNATURE_TYPE=http behind Cloud
# Tasks, or agent_worker_entrypoint, a CloudEvent, on the Pub/Sub rollback path).
# Built from the repo root by the root cloudbuild.yaml (build-crf step), or locally:
#   docker build -f cloud_functions/creative_fanout/Dockerfile -t crf .
# Dependencies are installed once, in a builder stage that sees only
# requirements.txt, so a code change reuses the cached dependency layer.
# Unlike the old `--source --base-image python313` buildpack deploys, this image
# gets no automatic base-image (OS/runtime) updates: rebuild to pick them up.
ARG BASE_IMAGE=python:3.13-slim
# Pinned uv (same digest as _UV_IMAGE in deployment/cloudbuild.agents.yaml).
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.19-python3.13-trixie-slim@sha256:aba5f865793af9275ceaa06d30935dfc6db8b81969c9fb099b0ebca6396b189e

FROM ${UV_IMAGE} AS uv

# The builder uses the final base image so the venv's interpreter symlink
# (/usr/local/bin/python3.13) exists unchanged in the final stage.
FROM ${BASE_IMAGE} AS builder
COPY --from=uv /usr/local/bin/uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY cloud_functions/creative_fanout/requirements.txt /build/requirements.txt
RUN uv venv /opt/venv --python /usr/local/bin/python3.13 \
 && uv pip install --python /opt/venv/bin/python --no-cache --compile-bytecode \
      -r /build/requirements.txt

FROM ${BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH
RUN useradd --create-home --uid 10001 app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY cloud_functions/creative_fanout/ /app/
USER app

# Shell form so $FUNCTION_TARGET / $FUNCTION_SIGNATURE_TYPE / $PORT expand at
# start; no FUNCTION_TARGET default, so a service without it exits at once
# instead of running the wrong entry point. FUNCTION_SIGNATURE_TYPE defaults to
# cloudevent (the orchestrator and the Pub/Sub worker); the Cloud Tasks worker
# sets it to http. --source=main.py is the buildpack-style package load
# (functions-framework registers main.py with its directory as the package
# path), which main.py's relative imports (`from .config import config`) need;
# `python main.py` would fail on them.
CMD ["/bin/sh", "-c", "exec functions-framework --source=main.py --target=\"${FUNCTION_TARGET:?FUNCTION_TARGET is not set}\" --signature-type=\"${FUNCTION_SIGNATURE_TYPE:-cloudevent}\" --host=0.0.0.0 --port=\"${PORT:-8080}\""]
