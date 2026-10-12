# syntax=docker/dockerfile:1
# The one bandit image. Its default entrypoint serves the CPR bandit endpoint
# (bandit_serving.predictor.BanditPredictor behind the Vertex AI CPR model server;
# Vertex Model.upload uses the image's default entrypoint). The synthetic-traffic
# Cloud Run Job runs the same image with a command override
# (deployment/bandit/deploy_traffic_job.sh: --command python
# --args=-m,bandit_traffic.main). Locally:
#   docker run --rm --entrypoint python <image> -m bandit_traffic.main --help
# Replaces the Dockerfile that LocalModel.build_cpr_model generated: same
# handler/predictor env, entrypoint, port and /usr/app workdir, but dependencies
# are installed once, in a builder stage that sees only requirements.txt, so a
# code change reuses the cached dependency layer.
#
# Build context = the repo root (Cloud Build: deployment/bandit/cloudbuild.bandit.yaml)
# or the tree deployment/bandit/build_image.py stages (same layout). The context is
# filtered by bandit_serving/Dockerfile.dockerignore.
ARG BASE_IMAGE=python:3.13-slim
# Pinned uv (same digest as _UV_IMAGE in deployment/cloudbuild.agents.yaml).
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.19-python3.13-trixie-slim@sha256:aba5f865793af9275ceaa06d30935dfc6db8b81969c9fb099b0ebca6396b189e

FROM ${UV_IMAGE} AS uv

# The builder uses the final base image so the venv's interpreter symlink
# (/usr/local/bin/python3.13) exists unchanged in the final stage.
FROM ${BASE_IMAGE} AS builder
COPY --from=uv /usr/local/bin/uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY bandit_serving/requirements.txt /build/requirements.txt
RUN uv venv /opt/venv --python /usr/local/bin/python3.13 \
 && uv pip install --python /opt/venv/bin/python --no-cache --compile-bytecode \
      -r /build/requirements.txt

FROM ${BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    HOME=/home/app \
    HANDLER_MODULE=google.cloud.aiplatform.prediction.handler \
    HANDLER_CLASS=PredictionHandler \
    PREDICTOR_MODULE=bandit_serving.predictor \
    PREDICTOR_CLASS=BanditPredictor
# /tmp_cpr_local_model is where deploy_to_local_endpoint (build_image.py
# --local-test) bind-mounts the artifacts; pre-creating it world-writable lets the
# non-root server write its local checkpoints there. On Vertex the artifacts are gs://.
RUN useradd --create-home --home-dir /home/app --uid 10001 app \
 && mkdir -p /usr/app \
 && mkdir -m 1777 /tmp_cpr_local_model
COPY --from=builder /opt/venv /opt/venv
WORKDIR /usr/app
COPY bandit/ /usr/app/bandit/
COPY bandit_serving/*.py /usr/app/bandit_serving/
COPY bandit_traffic/*.py /usr/app/bandit_traffic/
USER app
EXPOSE 8080
ENTRYPOINT ["python", "-m", "google.cloud.aiplatform.prediction.model_server"]
