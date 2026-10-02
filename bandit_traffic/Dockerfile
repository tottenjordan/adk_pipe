# Bandit synthetic-traffic Cloud Run Job. Build from the repo root:
#   docker build -f bandit_traffic/Dockerfile -t trend-trawler-bandit-traffic .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY bandit_traffic/requirements.txt /app/bandit_traffic/requirements.txt
RUN pip install -r /app/bandit_traffic/requirements.txt

COPY bandit/ /app/bandit/
COPY bandit_traffic/ /app/bandit_traffic/

RUN useradd --create-home --uid 10001 app
USER app

ENTRYPOINT ["python", "-m", "bandit_traffic.main"]
