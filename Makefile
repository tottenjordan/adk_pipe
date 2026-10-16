# Developer shortcuts for the commands in CLAUDE.md. `make` (or `make help`) lists them.
# Every target is a command, not a file: keep each one in .PHONY and give its rule
# line a `## description` (tests/test_makefile.py checks both, and `make -n` of each).

.DEFAULT_GOAL := help
SHELL := bash

GCP_REGION ?= us-central1
# Empty = gcloud's configured project.
GCP_PROJECT ?=
GCLOUD_PROJECT_FLAG = $(if $(GCP_PROJECT),--project=$(GCP_PROJECT))

# Tests need no credentials, only a (dummy) project ID; the unset GOOGLE_GENAI_USE_*
# vars keep a developer's shell from switching the genai client backend under the tests.
TEST_ENV = env -u GOOGLE_GENAI_USE_ENTERPRISE -u GOOGLE_GENAI_USE_VERTEXAI GOOGLE_CLOUD_PROJECT=test-project
# Stale exported engine IDs make deploy_agent.py refuse protected engines (deployment/README.md).
ENGINE_ENV = env -u CREATIVE_AGENT_ENGINE_ID -u INTERACTIVE_AGENT_ENGINE_ID -u TREND_SCOUT_ENGINE_ID

# Empty = ADK's default local SQLite session store; set e.g. memory:// to override.
SESSION_SERVICE_URI ?=
# Clean venv for the CRF tests (outside the repo so ruff/ty/pytest never see it).
CRF_VENV ?= $(or $(TMPDIR),/tmp)/tt-crf-venv
CRF_DIR = cloud_functions/creative_fanout

# deploy-engines: per-agent version namespaces (required) and the git ref Cloud Build clones.
CREATIVE_VERSION ?=
INTERACTIVE_VERSION ?=
SCOUT_VERSION ?=
REF ?= main
DEPLOY_VARS = CREATIVE_VERSION INTERACTIVE_VERSION SCOUT_VERSION
MISSING_DEPLOY_VARS = $(strip $(foreach v,$(DEPLOY_VARS),$(if $($(v)),,$(v))))

.PHONY: help sync fmt lint test test-fast test-crf frontend-test frontend-dev \
	dev-backend eval-scout eval-creative deploy-engines list-engines bandit-image

help: ## List these shortcuts
	@grep -E '^[A-Za-z0-9_-]+:.*## ' $(firstword $(MAKEFILE_LIST)) \
		| awk 'BEGIN {FS = ":.*## "} {printf "  %-16s %s\n", $$1, $$2}'

sync: ## Install dependencies (uv sync)
	uv sync

fmt: ## Format and auto-fix with ruff
	uv run ruff format .
	uv run ruff check --fix .

lint: ## ruff format --check, ruff check, ty check (the CI lint gate)
	uv run ruff format --check .
	uv run ruff check .
	uv run ty check

test: ## Python tests, 16 xdist workers
	$(TEST_ENV) uv run pytest tests/ -q -n 16

test-fast: ## Python tests without the slow ones (-n 6 -m "not slow")
	$(TEST_ENV) uv run pytest tests/ -q -n 6 -m "not slow"

test-crf: ## CRF tests in a clean venv from the function's own lock (mirrors crf-deps.yml)
	rm -rf $(CRF_VENV)
	uv venv --python 3.13 $(CRF_VENV)
	VIRTUAL_ENV=$(CRF_VENV) uv pip install --require-hashes -r $(CRF_DIR)/requirements.lock.txt
	VIRTUAL_ENV=$(CRF_VENV) uv pip install pytest
	GOOGLE_CLOUD_PROJECT=test-project $(CRF_VENV)/bin/python -m pytest tests/test_crf_*.py -q

frontend-test: ## Frontend tests (Vitest, single run)
	cd frontend && npm test

frontend-dev: ## Frontend dev server on http://localhost:3000
	cd frontend && npm install && npm run dev

# Uses ADK's default local SQLite session store unless SESSION_SERVICE_URI is set.
dev-backend: ## Local backend: the async_app launcher on :8000 (pairs with frontend-dev)
	TRUST_CLIENT_USER_ID=1 ALLOW_ORIGINS=http://localhost:3000 \
		$(if $(SESSION_SERVICE_URI),SESSION_SERVICE_URI=$(SESSION_SERVICE_URI)) \
		uv run uvicorn deployment.async_app:app --port 8000

eval-scout: ## ADK eval of trend_scout (real API calls, LLM judge)
	uv run adk eval agents/trend_scout tests/eval/evalsets/trend_scout_evalset.json \
		--config_file_path=tests/eval/eval_config.json --print_detailed_results

eval-creative: ## ADK eval of creative_agent (real API calls, creative rubric)
	uv run adk eval agents/creative_agent tests/eval/evalsets/creative_agent_evalset.json \
		--config_file_path=tests/eval/creative_eval_config.json --print_detailed_results

deploy-engines: ## Deploy all 3 engines via Cloud Build (needs CREATIVE_VERSION INTERACTIVE_VERSION SCOUT_VERSION; REF=main)
	$(if $(MISSING_DEPLOY_VARS),$(error Missing $(MISSING_DEPLOY_VARS). Usage: make deploy-engines CREATIVE_VERSION=vN INTERACTIVE_VERSION=vN SCOUT_VERSION=vN [REF=main]))
	gcloud builds submit --no-source --region=$(GCP_REGION) $(GCLOUD_PROJECT_FLAG) \
		--config=deployment/cloudbuild.agents.yaml \
		--substitutions=_REF=$(REF),_CREATIVE_VERSION=$(CREATIVE_VERSION),_INTERACTIVE_VERSION=$(INTERACTIVE_VERSION),_SCOUT_VERSION=$(SCOUT_VERSION)

list-engines: ## List Agent Engine instances
	$(ENGINE_ENV) uv run python deployment/deploy_agent.py --list

bandit-image: ## Build the bandit image via Cloud Build (prints the digest; deploys nothing)
	gcloud builds submit --region $(GCP_REGION) $(GCLOUD_PROJECT_FLAG) \
		--config deployment/bandit/cloudbuild.bandit.yaml \
		--ignore-file deployment/bandit/bandit.gcloudignore \
		--substitutions=SHORT_SHA=$$(git rev-parse --short HEAD) .
