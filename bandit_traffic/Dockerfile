# syntax=docker/dockerfile:1
# Bandit synthetic-traffic Cloud Run Job. Built from the repo root by
# deployment/bandit/cloudbuild.bandit.yaml, or locally:
#   docker build -f bandit_traffic/Dockerfile -t trend-trawler-bandit-traffic .
# Dependencies are installed once, in a builder stage that sees only
# requirements.txt, so a code change reuses the cached dependency layer.
ARG BASE_IMAGE=python:3.13-slim
# Pinned uv (same digest as _UV_IMAGE in deployment/cloudbuild.agents.yaml).
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.19-python3.13-trixie-slim@sha256:aba5f865793af9275ceaa06d30935dfc6db8b81969c9fb099b0ebca6396b189e

FROM ${UV_IMAGE} AS uv

# The builder uses the final base image so the venv's interpreter symlink
# (/usr/local/bin/python3.13) exists unchanged in the final stage.
FROM ${BASE_IMAGE} AS builder
COPY --from=uv /usr/local/bin/uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY bandit_traffic/requirements.txt /build/requirements.txt
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
COPY bandit/ /app/bandit/
COPY bandit_traffic/ /app/bandit_traffic/
USER app

ENTRYPOINT ["python", "-m", "bandit_traffic.main"]
