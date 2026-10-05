"""Bandit experiments REST API (docs/bandit/contracts.md §5).

A user picks 2-4 creatives from a finished creative run; the api snapshots them as
bandit arms, writes ``experiment.json`` (a §1 ``experiment_config_to_dict``) under
``BANDIT_ARTIFACTS_PREFIX/{id}/``, records a ``bandit_experiments`` row and deploys a
Vertex endpoint in a detached task. Synthetic traffic runs as a Cloud Run Job;
``/metrics`` aggregates the job's ``bandit_episode_metrics`` rows; ``/creatives``
builds the §8 per-creative time series from ``bandit_events``.

Status machine (``next_status``)::

    deploying -> ready -> running_traffic -> ready -> stopping -> stopped
    any active -> failed (deploy error, endpoint gone) | expired (TTL reaper)

Like ``runserver/async_runs.py``: module-level ``configure(...)`` binds the shared
session service + backends, detached tasks are held in ``_BACKGROUND_TASKS``, and the
path-scoped routes are gated by ``UserAuthzMiddleware`` (the bare ``POST
/experiments`` checks its body ``userId`` with ``authorize_body_user``). A foreign
experiment is a 404. No JAX here: the api image doesn't ship it.

Single-process, best-effort guards (one active experiment per user, one teardown
task per experiment), the same trade-off as the ``/runs`` duplicate-run guard: the
api runs one uvicorn process on one instance.

Deploys are the exception, because a duplicate one leaks a Vertex model/endpoint:
besides the in-process task guard, a deploy only runs while its process holds the
row's **deploy lease** (``deploy_lease_until`` / ``deploy_lease_owner``, taken with a
conditional UPDATE, renewed every ``deploy_heartbeat_seconds``, released when the
deploy ends). So another instance, or an old revision kept alive by a traffic tag
whose reaper sees a ``deploying`` row without a local task, backs off instead of
uploading a second model (incident 2026-10-05). A crashed holder's lease expires
after ``deploy_lease_seconds`` and the next reconcile/reaper pass resumes the deploy.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import math
import os
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agent_common.idempotency import stable_row_id
from runserver.authz import (
    AuthzMode,
    UserAuthzError,
    authorize_body_user,
    trusted_user,
)
from runserver.experiments_deploy import ID_FIELDS, FakeDeployer, VertexDeployer
from runserver.experiments_jobs import CloudRunJobsRunner, FakeJobsRunner
from runserver.experiments_metrics import aggregate_episode_metrics
from runserver.experiments_series import build_creative_series
from runserver.experiments_store import (
    ACTIVE_STATUSES,
    BigQueryExperimentStore,
    InMemoryExperimentStore,
)

log = logging.getLogger(__name__)

SCENARIOS = ("clear_winner", "segment_winners", "drift")
CTR_MODES = ("demo", "realistic")
REWARD_MODES = ("click", "engaged")
MIN_ARMS, MAX_ARMS = 2, 4
EPISODES_RANGE = (1, 100)
HORIZON_RANGE = (1000, 400000)
TERMINAL_STATUSES = ("stopped", "failed", "expired")
DEFAULT_TRAFFIC_JOB = "trend-trawler-bandit-traffic"
REAPER_INTERVAL_SECONDS = 300.0

# (status, event) -> next status. Anything else is an InvalidTransition.
_TRANSITIONS: dict[str, dict[str, str]] = {
    "deploying": {
        "deployed": "ready",
        "fail": "failed",
        "stop": "stopping",
        "expire": "expired",
    },
    "ready": {
        "traffic_started": "running_traffic",
        "fail": "failed",
        "stop": "stopping",
        "expire": "expired",
    },
    "running_traffic": {
        "traffic_done": "ready",
        "fail": "failed",
        "stop": "stopping",
        "expire": "expired",
    },
    # A stop in progress only ends one way; a late deploy failure doesn't override it.
    "stopping": {"torn_down": "stopped", "expire": "expired"},
}


class InvalidTransition(ValueError):
    pass


def next_status(current: str, event: str) -> str:
    """The status after ``event`` (raises ``InvalidTransition`` when not allowed)."""
    try:
        return _TRANSITIONS[current][event]
    except KeyError:
        raise InvalidTransition(f"{event!r} not allowed from {current!r}") from None


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _as_datetime(value: Any) -> dt.datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, dt.datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value if isinstance(value, dt.datetime) else None


def is_expired(row: Mapping[str, Any], now: dt.datetime) -> bool:
    """True when a not-yet-terminal experiment is past ``ttl_expires_at``."""
    if row.get("status") in TERMINAL_STATUSES:
        return False
    ttl = _as_datetime(row.get("ttl_expires_at"))
    return ttl is not None and now >= ttl


def _iso(value: Any) -> str | None:
    when = _as_datetime(value)
    if when is None:
        return None
    return when.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def to_summary(row: Mapping[str, Any]) -> dict:
    """A store row -> the §5 camelCase ``ExperimentSummary``."""
    progress = row.get("progress")
    if isinstance(progress, str):
        progress = json.loads(progress) if progress else None
    return {
        "experimentId": row["experiment_id"],
        "userId": row["user_id"],
        "sessionId": row["session_id"],
        "appName": row["app_name"],
        "createdAt": _iso(row.get("created_at")),
        "updatedAt": _iso(row.get("updated_at")),
        "status": row["status"],
        "scenario": row.get("scenario") or "",
        "ctrMode": row.get("ctr_mode") or "",
        "rewardMode": row.get("reward_mode") or "",
        "ttlExpiresAt": _iso(row.get("ttl_expires_at")),
        "arms": list(row.get("arms") or []),
        "endpointId": row.get("endpoint_id") or None,
        "trafficExecution": row.get("traffic_execution") or None,
        "progress": (
            {
                "episodesDone": int(progress.get("episodes_done") or 0),
                "episodesTotal": int(progress.get("episodes_total") or 0),
            }
            if isinstance(progress, Mapping)
            else None
        ),
        "error": row.get("error") or None,
        "scenarioOverrides": overrides_to_camel(row.get("scenario_overrides")),
    }


# --- Arm snapshot ---------------------------------------------------------------


class SelectionError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _as_obj(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _items(container: Any, key: str) -> list[dict]:
    obj = _as_obj(container)
    if isinstance(obj, Mapping):
        obj = obj.get(key)
    return [x for x in obj if isinstance(x, Mapping)] if isinstance(obj, list) else []


def _match_ad_copy_eval(evals: list[dict], concept: Mapping, index: int) -> dict | None:
    """Headline, then ``ad_copy_id`` -> ``original_id``, then position.

    Headline goes first (unlike the results page's id-first lookup) because the
    judge often echoes the same ``original_id`` for every ad copy (seen in the
    screenshot fixture), which would give every arm the first copy's scores."""
    headline = concept.get("headline")
    for ev in evals:
        if headline and ev.get("headline") == headline:
            return ev
    for ev in evals:
        if ev.get("original_id") is not None and ev.get("original_id") == concept.get(
            "ad_copy_id"
        ):
            return ev
    return evals[index] if index < len(evals) else None


def _eval_scores(
    ev: Mapping | None, prefix: str
) -> tuple[dict[str, float], float | None]:
    if not ev:
        return {}, None
    score = ev.get("score") or {}
    out: dict[str, float] = {}
    for verdict in score.get("verdicts") or []:
        dim, raw = verdict.get("dimension"), verdict.get("score")
        if dim and isinstance(raw, (int, float)):
            out[dim] = max(0.0, min(1.0, float(raw) / 10.0))
    overall = score.get("overall_score")
    overall = float(overall) if isinstance(overall, (int, float)) else None
    if overall is not None:
        out[f"{prefix}_overall"] = overall
    return out, overall


def _image_uri(
    state: Mapping, concept_name: str, default_bucket: str | None
) -> str | None:
    from creative_agent.gcs_tools import artifact_key_for  # lazy: builds the agent

    bucket = state.get("gcs_bucket_name") or (
        str(state.get("gcs_bucket") or "").removeprefix("gs://") or default_bucket
    )
    folder, subdir = state.get("gcs_folder"), state.get("agent_output_dir")
    if not (bucket and folder and subdir):
        return None
    key = artifact_key_for(concept_name)
    generated = state.get("_generated_artifact_keys")
    if isinstance(generated, list) and generated and key not in generated:
        return None  # never rendered (e.g. image generation failed)
    return f"gs://{bucket}/{folder}/{subdir}/{key}"


def validate_selection(indices: Sequence[int], available: int) -> list[int]:
    picked = list(indices)
    if len(picked) < MIN_ARMS:
        raise SelectionError(
            "too_few_arms", f"select at least {MIN_ARMS} creatives (got {len(picked)})"
        )
    if len(picked) > MAX_ARMS:
        raise SelectionError(
            "too_many_arms", f"select at most {MAX_ARMS} creatives (got {len(picked)})"
        )
    if len(set(picked)) != len(picked):
        raise SelectionError("duplicate_index", "creative indices must be unique")
    bad = [i for i in picked if not 0 <= i < available]
    if bad:
        raise SelectionError(
            "index_out_of_range",
            f"creative indices {bad} out of range (run has {available})",
        )
    return picked


def snapshot_arms(
    session_state: Mapping[str, Any],
    session_id: str,
    indices: Sequence[int],
    *,
    default_bucket: str | None = None,
) -> list[dict]:
    """Freeze the selected visual concepts as §5 ``Arm`` dicts.

    ``creativeId = stable_row_id(session_id, concept_name)``; ``scores`` holds every
    judge dimension normalised to 0-1 plus ``ad_copy_overall`` / ``visual_overall``;
    ``overallScore`` is the mean of the available overall scores (the results
    page's proof score). Raises ``SelectionError`` for an invalid selection."""
    concepts = _items(session_state.get("final_visual_concepts"), "visual_concepts")
    if not concepts:
        raise SelectionError("no_creatives", "this run has no visual concepts")
    picked = validate_selection(indices, len(concepts))
    report = _as_obj(session_state.get("creative_evaluation_report")) or {}
    visual_evals = _items(report, "visual_concept_evaluations")
    ad_evals = _items(report, "ad_copy_evaluations")
    arms = []
    for idx in picked:
        concept = concepts[idx]
        name = str(concept.get("concept_name") or f"concept {idx}")
        vis = next((v for v in visual_evals if v.get("concept_name") == name), None)
        ad = _match_ad_copy_eval(ad_evals, concept, idx)
        ad_scores, ad_overall = _eval_scores(ad, "ad_copy")
        vis_scores, vis_overall = _eval_scores(vis, "visual")
        overalls = [s for s in (ad_overall, vis_overall) if s is not None]
        arms.append(
            {
                "creativeId": stable_row_id(session_id, name),
                "index": idx,
                "label": str(concept.get("headline") or name),
                "conceptName": name,
                "imageUri": _image_uri(session_state, name, default_bucket),
                "scores": {**ad_scores, **vis_scores},
                "overallScore": sum(overalls) / len(overalls) if overalls else None,
            }
        )
    return arms


def visual_styles(session_state: Mapping[str, Any], arms: Sequence[Mapping]) -> dict:
    """``{creativeId: visual_style}`` for the arms (feeds ``ArmSpec.visual_style``)."""
    concepts = _items(session_state.get("final_visual_concepts"), "visual_concepts")
    return {
        a["creativeId"]: str(concepts[a["index"]].get("visual_style") or "")
        for a in arms
        if 0 <= a["index"] < len(concepts)
    }


DEFAULT_POLICY = {
    "prior_var": 1.0,
    "exploration_scale": 1.0,
    "propensity_samples": 1000,
    "min_propensity": 0.02,
    "discount": 1.0,
}

#: Scenario target mean CTRs, copied from ``bandit/scenarios/*.yaml``
#: (``target_ctr``). runserver must not import ``bandit`` (contracts §1), so the
#: tiny noise-variance rule is duplicated here; ``tests/test_experiments_api.py``
#: asserts parity with ``bandit.config.scenario_noise_var`` (the source of truth).
SCENARIO_TARGET_CTR = {
    "clear_winner": {"demo": 0.0467, "realistic": 0.00933},
    "segment_winners": {"demo": 0.04, "realistic": 0.008},
    "drift": {"demo": 0.0467, "realistic": 0.00933},
}


def default_noise_var(scenario: str, ctr_mode: str, reward_mode: str) -> float:
    """Calibrated LinTS σ² (contracts §7): p(1-p) for clicks, 2p-p² for engaged
    (rewards scaled by the base dwell), at the scenario's target CTR p. Mirrors
    ``bandit.config.default_noise_var``; unknown combos fall back to 0.04/demo."""
    p = SCENARIO_TARGET_CTR.get(scenario, {}).get(ctr_mode, 0.04)
    if reward_mode == "engaged":
        return round(2 * p - p * p, 6)
    return round(p * (1 - p), 6)


#: Segments per scenario preset (``len(load_scenario(name).segments)``) and the
#: inclusive override bounds, duplicated from ``bandit`` (contracts §9; runserver
#: never imports it). ``tests/test_experiments_api.py`` asserts parity.
SCENARIO_SEGMENTS = {"clear_winner": 3, "segment_winners": 4, "drift": 3}
OVERRIDE_BOUNDS: dict[str, tuple[float, float]] = {
    "segment_mix": (0.05, 1.0),
    "gap_scale": (0.25, 2.0),
    "judge_wrong": (0.0, 1.0),
    "noise_scale": (0.0, 2.0),
    "drift_at_frac": (0.2, 0.8),
}
#: REST camelCase -> experiment.json snake_case (contracts §9).
OVERRIDE_FIELDS = {
    "segmentMix": "segment_mix",
    "gapScale": "gap_scale",
    "judgeWrong": "judge_wrong",
    "noiseScale": "noise_scale",
    "driftAtFrac": "drift_at_frac",
}
_OVERRIDE_CAMEL = {v: k for k, v in OVERRIDE_FIELDS.items()}


class ScenarioOverridesError(ValueError):
    """An invalid ``scenarioOverrides`` body; ``field`` is the camelCase name."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def _override_number(field: str, value: Any) -> float:
    # bool is an int subclass; JSON true/false is never a valid override.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ScenarioOverridesError(field, f"{field} must be a number")
    lo, hi = OVERRIDE_BOUNDS[OVERRIDE_FIELDS[field]]
    if not (math.isfinite(value) and lo <= value <= hi):
        raise ScenarioOverridesError(field, f"{field} must be in [{lo}, {hi}]")
    return float(value)


def validate_scenario_overrides(
    scenario: str, ov: Mapping[str, Any] | None
) -> dict | None:
    """Check a camelCase ``scenarioOverrides`` body against contracts §9 and return
    the snake_case ``experiment.json`` object, or ``None`` when nothing is set
    (``null`` fields count as unset). ``segment_mix`` is stored as sent; ``bandit``
    renormalises it. Raises ``ScenarioOverridesError`` naming the field."""
    if ov is None:
        return None
    if not isinstance(ov, Mapping):
        raise ScenarioOverridesError(
            "scenarioOverrides", "scenarioOverrides must be an object"
        )
    for key in ov:
        if key not in OVERRIDE_FIELDS:
            raise ScenarioOverridesError(
                str(key), f"unknown scenarioOverrides field {key!r}"
            )
    out: dict[str, Any] = {}
    mix = ov.get("segmentMix")
    if mix is not None:
        n = SCENARIO_SEGMENTS.get(scenario)
        if not isinstance(mix, list):
            raise ScenarioOverridesError("segmentMix", "segmentMix must be a list")
        if n is None or len(mix) != n:
            raise ScenarioOverridesError(
                "segmentMix", f"segmentMix needs {n} weights for {scenario}"
            )
        out["segment_mix"] = [_override_number("segmentMix", w) for w in mix]
    for field in ("gapScale", "judgeWrong", "noiseScale", "driftAtFrac"):
        value = ov.get(field)
        if value is None:
            continue
        if field == "driftAtFrac" and scenario != "drift":
            raise ScenarioOverridesError(
                field, "driftAtFrac is only valid for the drift scenario"
            )
        out[OVERRIDE_FIELDS[field]] = _override_number(field, value)
    return out or None


def overrides_to_camel(value: Any) -> dict | None:
    """A stored snake_case ``scenario_overrides`` (dict or JSON string) -> the
    camelCase ``ExperimentSummary.scenarioOverrides`` (``None`` when unset)."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value else None
        except ValueError:
            return None
    if not isinstance(value, Mapping) or not value:
        return None
    return {_OVERRIDE_CAMEL.get(k, k): v for k, v in value.items()}


def build_experiment_config(
    experiment_id: str,
    arms: Sequence[Mapping],
    scenario: str,
    ctr_mode: str = "demo",
    reward_mode: str = "click",
    *,
    styles: Mapping[str, str] | None = None,
    horizon: int = 20000,
    batch_size: int = 100,
    episodes: int = 20,
    seed: int | None = None,
    policy: Mapping[str, float] | None = None,
    scenario_overrides: Mapping[str, Any] | None = None,
) -> dict:
    """The §1 ``experiment_config_to_dict`` JSON (pure; doesn't import ``bandit``).

    ``seed`` defaults to a stable 31-bit hash of the experiment id.
    ``scenario_overrides`` (the snake_case §9 object from
    ``validate_scenario_overrides``) is written only when non-empty, so a default
    config stays loadable by images built before §9."""
    if seed is None:
        seed = int(stable_row_id(experiment_id), 16) % (2**31)
    cfg = {
        "experiment_id": experiment_id,
        "arms": [
            {
                "creative_id": a["creativeId"],
                "label": a["label"],
                "scores": {k: float(v) for k, v in (a.get("scores") or {}).items()},
                "visual_style": (styles or {}).get(a["creativeId"], ""),
            }
            for a in arms
        ],
        "scenario": scenario,
        "ctr_mode": ctr_mode,
        "reward_mode": reward_mode,
        "horizon": horizon,
        "batch_size": batch_size,
        "episodes": episodes,
        "seed": seed,
        "policy": {
            **DEFAULT_POLICY,
            "noise_var": default_noise_var(scenario, ctr_mode, reward_mode),
            **(policy or {}),
        },
    }
    if scenario_overrides:
        cfg["scenario_overrides"] = dict(scenario_overrides)
    return cfg


# --- Settings + wiring ----------------------------------------------------------

ConfigWriter = Callable[[str, dict], Awaitable[None]]


class MemoryConfigWriter:
    """Keeps written configs in memory (tests + fake mode)."""

    def __init__(self) -> None:
        self.files: dict[str, dict] = {}

    async def __call__(self, uri: str, payload: dict) -> None:
        self.files[uri] = json.loads(json.dumps(payload))


def gcs_config_writer(client_factory: Callable[[], Any] | None = None) -> ConfigWriter:
    """Write ``payload`` as JSON to a ``gs://bucket/path`` URI (blocking SDK in a thread)."""

    async def write(uri: str, payload: dict) -> None:
        from agent_common.clients import get_gcs_client

        bucket, _, blob = uri.removeprefix("gs://").partition("/")
        factory = client_factory or get_gcs_client

        def _upload() -> None:
            factory().bucket(bucket).blob(blob).upload_from_string(
                json.dumps(payload, indent=2), content_type="application/json"
            )

        await asyncio.to_thread(_upload)

    return write


@dataclass
class ExperimentSettings:
    artifacts_prefix: str = "memory://bandit"
    ttl_minutes: int = 120
    min_ttl_minutes: int = 10
    max_ttl_minutes: int = 480
    gcs_bucket: str | None = None
    config_writer: ConfigWriter | None = None
    # How long stop / an expiring GET waits for the teardown before answering.
    teardown_wait_seconds: float = 2.0
    default_horizon: int = 20000
    default_episodes: int = 20
    batch_size: int = 100
    # Deploy lease: expiry, and how often the running deploy renews it.
    deploy_lease_seconds: int = 180
    deploy_heartbeat_seconds: float = 60.0

    def clamp_ttl(self, minutes: int | None) -> int:
        value = self.ttl_minutes if minutes is None else minutes
        return max(self.min_ttl_minutes, min(self.max_ttl_minutes, int(value)))


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name) or default)
    except ValueError:
        log.warning("%s=%r is not an int; using %d", name, env.get(name), default)
        return default


def build_backend_from_env(env: Mapping[str, str] = os.environ) -> dict:
    """``{mode, store, deployer, jobs, settings}`` for ``BANDIT_DEPLOY_MODE``.

    ``fake`` (or ``vertex`` without ``BQ_PROJECT_ID``/``BQ_DATASET_ID``/a bucket,
    i.e. local dev, which falls back with a warning): in-memory store, a
    ``FakeDeployer`` that is ready after ``BANDIT_FAKE_DEPLOY_SECONDS`` (default 2)
    and a fake job runner. ``vertex``: BigQuery + Vertex + Cloud Run Jobs."""
    mode = (env.get("BANDIT_DEPLOY_MODE") or "vertex").strip().lower()
    if mode not in ("vertex", "fake"):
        raise RuntimeError(f"BANDIT_DEPLOY_MODE must be vertex|fake, got {mode!r}")
    bucket = env.get("GOOGLE_CLOUD_STORAGE_BUCKET") or None
    prefix = (env.get("BANDIT_ARTIFACTS_PREFIX") or "").rstrip("/") or (
        f"gs://{bucket}/bandit" if bucket else ""
    )
    if mode == "vertex":
        missing = [n for n in ("BQ_PROJECT_ID", "BQ_DATASET_ID") if not env.get(n)] + (
            [] if prefix.startswith("gs://") else ["BANDIT_ARTIFACTS_PREFIX/bucket"]
        )
        if missing:
            log.warning(
                "bandit experiments: %s unset; falling back to BANDIT_DEPLOY_MODE=fake",
                ", ".join(missing),
            )
            mode = "fake"
    settings = ExperimentSettings(
        artifacts_prefix=prefix or "memory://bandit",
        ttl_minutes=_int_env(env, "BANDIT_TTL_MINUTES", 120),
        gcs_bucket=bucket,
    )
    if mode == "fake":
        settings.config_writer = MemoryConfigWriter()
        delay = float(env.get("BANDIT_FAKE_DEPLOY_SECONDS") or 2.0)
        return {
            "mode": mode,
            "store": InMemoryExperimentStore(),
            "deployer": FakeDeployer(delay=delay),
            "jobs": FakeJobsRunner(auto_finish=True),
            "settings": settings,
        }
    settings.config_writer = gcs_config_writer()
    image = env.get("BANDIT_SERVING_IMAGE", "")
    if not image:
        log.warning("BANDIT_SERVING_IMAGE unset: experiment deploys will fail")
    return {
        "mode": mode,
        "store": BigQueryExperimentStore(),
        "deployer": VertexDeployer(
            image, service_account=env.get("BANDIT_ENDPOINT_SA") or None
        ),
        "jobs": CloudRunJobsRunner(
            env.get("BANDIT_TRAFFIC_JOB") or DEFAULT_TRAFFIC_JOB
        ),
        "settings": settings,
    }


# --- Module state (configure) ---------------------------------------------------

_SESSION_SERVICE: Any = None
_STORE: Any = None
_DEPLOYER: Any = None
_JOBS: Any = None
_AUTHZ_MODE = AuthzMode.TRUST_CLIENT
_SETTINGS = ExperimentSettings()
# Strong refs so asyncio's GC can't drop detached tasks (see async_runs).
_BACKGROUND_TASKS: set[asyncio.Task] = set()
_DEPLOY_TASKS: dict[str, asyncio.Task] = {}
_TEARDOWN_TASKS: dict[str, asyncio.Task] = {}
_TRAFFIC_STARTING: set[str] = set()
_LOCKS: dict[str, asyncio.Lock] = {}
# /creatives cache: experiment_id -> (expires_at monotonic | None = forever, body).
# Live traffic refreshes every 30 s; a stopped/expired experiment's data is final.
SERIES_LIVE_TTL_SECONDS = 30.0
SERIES_FINAL_STATUSES = ("stopped", "expired")
SERIES_CACHE_MAX = 64
_SERIES_CACHE: OrderedDict[str, tuple[float | None, dict]] = OrderedDict()
# This process's deploy-lease identity: revision (Cloud Run) + a per-process nonce.
LEASE_OWNER = f"{os.environ.get('K_REVISION') or 'local'}/{uuid.uuid4().hex[:12]}"


def configure(
    *,
    session_service,
    store,
    deployer,
    jobs,
    authz_mode: AuthzMode = AuthzMode.TRUST_CLIENT,
    settings: ExperimentSettings | None = None,
) -> None:
    """Bind the shared session service and the experiment backends (resets the
    per-process task registries)."""
    global _SESSION_SERVICE, _STORE, _DEPLOYER, _JOBS, _AUTHZ_MODE, _SETTINGS
    _SESSION_SERVICE, _STORE, _DEPLOYER, _JOBS = session_service, store, deployer, jobs
    _AUTHZ_MODE = authz_mode
    _SETTINGS = settings or ExperimentSettings()
    if _SETTINGS.config_writer is None:
        _SETTINGS.config_writer = MemoryConfigWriter()
    _DEPLOY_TASKS.clear()
    _TEARDOWN_TASKS.clear()
    _TRAFFIC_STARTING.clear()
    _LOCKS.clear()
    _SERIES_CACHE.clear()


def _lock(key: str) -> asyncio.Lock:
    return _LOCKS.setdefault(key, asyncio.Lock())


def _spawn(coro, registry: dict[str, asyncio.Task] | None = None, key: str = ""):
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    if registry is not None:
        registry[key] = task
        task.add_done_callback(
            lambda t: registry.pop(key, None) if registry.get(key) is t else None
        )
    return task


async def drain() -> None:
    """Await every detached task (tests; graceful shutdown)."""
    while pending := [t for t in _BACKGROUND_TASKS if not t.done()]:
        await asyncio.gather(*pending, return_exceptions=True)


async def _update(experiment_id: str, **changes: Any) -> dict | None:
    async with _lock(experiment_id):
        row = await _STORE.get(experiment_id)
        if row is None:
            return None
        changes["updated_at"] = _utcnow()
        row.update(changes)
        await _STORE.upsert(row, fields=list(changes))
        return row


async def _transition(
    experiment_id: str, event: str, **changes: Any
) -> tuple[dict | None, bool]:
    """Apply ``event`` (plus ``changes``) if allowed; returns ``(row, applied)``."""
    async with _lock(experiment_id):
        row = await _STORE.get(experiment_id)
        if row is None:
            return None, False
        try:
            status = next_status(row["status"], event)
        except InvalidTransition:
            return row, False
        changes.update(status=status, updated_at=_utcnow())
        row.update(changes)
        await _STORE.upsert(row, fields=list(changes))
        return row, True


def _artifact_uri(row: Mapping[str, Any]) -> str:
    return str(row.get("config_uri") or "").removesuffix("/experiment.json")


# --- Detached tasks -------------------------------------------------------------


async def _lease_heartbeat(experiment_id: str) -> None:
    """Renew this process's deploy lease until cancelled (or until it is lost)."""
    while True:
        await asyncio.sleep(_SETTINGS.deploy_heartbeat_seconds)
        try:
            renewed = await _STORE.renew_deploy_lease(
                experiment_id, LEASE_OWNER, _SETTINGS.deploy_lease_seconds
            )
        except Exception:
            log.exception("bandit deploy %s: lease heartbeat failed", experiment_id)
            continue
        if not renewed:
            log.warning("bandit deploy %s: deploy lease lost", experiment_id)
            return


async def _deploy_task(experiment_id: str) -> None:
    row = await _STORE.get(experiment_id)
    if row is None or row["status"] != "deploying":
        return
    try:
        acquired = await _STORE.acquire_deploy_lease(
            experiment_id, LEASE_OWNER, _SETTINGS.deploy_lease_seconds
        )
    except Exception:
        log.exception("bandit deploy %s: lease acquire failed", experiment_id)
        return  # the next reconcile/reaper pass retries
    if not acquired:
        log.info("bandit deploy %s: lease held elsewhere; not deploying", experiment_id)
        return
    heartbeat = asyncio.create_task(_lease_heartbeat(experiment_id))
    try:
        # Re-read under the lease: a previous holder may have recorded more ids.
        await _deploy_locked(experiment_id, await _STORE.get(experiment_id) or row)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        try:
            await _STORE.release_deploy_lease(experiment_id, LEASE_OWNER)
        except Exception:  # it expires on its own
            log.exception("bandit deploy %s: lease release failed", experiment_id)


async def _deploy_locked(experiment_id: str, row: dict) -> None:
    """The deploy proper; only called while holding the deploy lease."""

    async def on_step(ids: dict) -> None:
        await _update(experiment_id, **ids)

    try:
        ids = await _DEPLOYER.deploy(
            experiment_id,
            _artifact_uri(row),
            existing={k: row.get(k) for k in ID_FIELDS},
            on_step=on_step,
        )
    except Exception as exc:
        log.exception("bandit deploy %s failed", experiment_id)
        failed, _ = await _transition(
            experiment_id, "fail", error=f"deploy failed: {exc}"[:1000]
        )
        try:  # don't leak a half-created model/endpoint
            await _DEPLOYER.teardown(failed or row)
        except Exception:
            log.exception("bandit cleanup after failed deploy %s", experiment_id)
        return
    current, applied = await _transition(experiment_id, "deployed", **ids)
    if (
        not applied
        and current is not None
        and current["status"]
        in (
            "stopping",
            "stopped",
            "expired",
        )
    ):
        # Stopped/expired while deploying: the earlier teardown may have missed
        # resources created after it ran.
        await _DEPLOYER.teardown({**current, **ids})


def _start_deploy(experiment_id: str) -> asyncio.Task:
    live = _DEPLOY_TASKS.get(experiment_id)
    if live is not None and not live.done():
        return live
    return _spawn(_deploy_task(experiment_id), _DEPLOY_TASKS, experiment_id)


async def _teardown_task(experiment_id: str, final_event: str) -> None:
    row = await _STORE.get(experiment_id)
    if row is None:
        return
    try:
        await _DEPLOYER.teardown(row)
    except Exception as exc:
        log.exception("bandit teardown %s failed", experiment_id)
        await _update(experiment_id, error=f"teardown failed: {exc}"[:1000])
        return
    await _transition(experiment_id, final_event, stopped_at=_utcnow())


def _start_teardown(experiment_id: str, final_event: str) -> asyncio.Task:
    live = _TEARDOWN_TASKS.get(experiment_id)
    if live is not None and not live.done():
        return live
    return _spawn(
        _teardown_task(experiment_id, final_event), _TEARDOWN_TASKS, experiment_id
    )


async def _wait_briefly(task: asyncio.Task) -> None:
    await asyncio.wait({task}, timeout=_SETTINGS.teardown_wait_seconds)


def _live(registry: dict[str, asyncio.Task], key: str) -> bool:
    task = registry.get(key)
    return task is not None and not task.done()


def _should_resume_deploy(row: Mapping[str, Any], now: dt.datetime) -> bool:
    """A ``deploying`` row with no local task and no unexpired lease seen on it.
    (A cheap pre-check; ``_deploy_task``'s conditional acquire is authoritative.)"""
    if _live(_DEPLOY_TASKS, row["experiment_id"]):
        return False
    until = _as_datetime(row.get("deploy_lease_until"))
    return until is None or until <= now


async def reconcile(row: dict, now: dt.datetime | None = None) -> dict:
    """Bring a row's status in line with the TTL, the endpoint and the job."""
    now = now or _utcnow()
    eid, status = row["experiment_id"], row["status"]
    if status in TERMINAL_STATUSES:
        return row
    if is_expired(row, now) and status != "stopping":
        await _wait_briefly(_start_teardown(eid, "expire"))
        return await _STORE.get(eid) or row
    if status == "deploying" and _should_resume_deploy(row, now):
        _start_deploy(eid)  # resume after an api restart
    elif status == "stopping" and not _live(_TEARDOWN_TASKS, eid):
        _start_teardown(eid, "torn_down")
    elif status in ("ready", "running_traffic") and row.get("endpoint_id"):
        try:
            state = await _DEPLOYER.state(row)
        except Exception:
            log.exception("bandit endpoint state %s", eid)
            state = None
        if state is not None and not (state["exists"] and state["deployed"]):
            failed, _ = await _transition(
                eid, "fail", error="endpoint no longer exists or has no deployed model"
            )
            if failed is not None:
                _start_teardown_quietly(failed)
            return failed or row
        if status == "running_traffic":
            row = await _maybe_finish_traffic(row)
    return row


def _start_teardown_quietly(row: dict) -> None:
    """Best-effort cleanup of whatever is left of a failed experiment."""

    async def _cleanup() -> None:
        try:
            await _DEPLOYER.teardown(row)
        except Exception:
            log.exception("bandit cleanup %s", row["experiment_id"])

    _spawn(_cleanup())


async def _maybe_finish_traffic(row: dict) -> dict:
    raw = row.get("progress")
    progress: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    total = int(progress.get("episodes_total") or 0)
    done = total > 0 and int(progress.get("episodes_done") or 0) >= total
    job_state = "unknown"
    if row.get("traffic_execution"):
        job_state = await _JOBS.state(row["traffic_execution"])
    if not (done or job_state in ("succeeded", "failed")):
        return row
    changes: dict[str, Any] = {}
    if job_state == "failed":
        changes["error"] = "traffic job failed (see the Cloud Run execution logs)"
    updated, _ = await _transition(row["experiment_id"], "traffic_done", **changes)
    return updated or row


async def reap_expired(now: dt.datetime | None = None) -> list[str]:
    """One reaper pass: tear down expired experiments (status ``expired``), and
    resume deploys / teardowns whose task died with a previous api revision.
    Returns the ids it expired."""
    now = now or _utcnow()
    expired: list[str] = []
    tasks: list[asyncio.Task] = []
    for row in await _STORE.list_active():
        eid, status = row["experiment_id"], row["status"]
        if is_expired(row, now) and status != "stopping":
            expired.append(eid)
            tasks.append(_start_teardown(eid, "expire"))
        elif status == "deploying" and _should_resume_deploy(row, now):
            _start_deploy(eid)
        elif status == "stopping" and not _live(_TEARDOWN_TASKS, eid):
            tasks.append(_start_teardown(eid, "torn_down"))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return expired


async def reaper_loop(interval: float = REAPER_INTERVAL_SECONDS) -> None:
    while True:
        try:
            if expired := await reap_expired():
                log.info("bandit reaper expired %s", expired)
        except Exception:
            log.exception("bandit reaper pass failed")
        await asyncio.sleep(interval)


def start_reaper(interval: float = REAPER_INTERVAL_SECONDS) -> asyncio.Task:
    return _spawn(reaper_loop(interval))


# --- Routes ---------------------------------------------------------------------

router = APIRouter()


def _error(code: int, reason: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(
        status_code=code, detail={"reason": reason, "message": message, **extra}
    )


class _CreateBody(BaseModel):
    userId: str  # noqa: N815 -- camelCase matches the frontend JSON payload
    appName: str  # noqa: N815
    sessionId: str  # noqa: N815
    creativeIndices: list[int]  # noqa: N815
    scenario: str
    ctrMode: str = "demo"  # noqa: N815
    rewardMode: str = "click"  # noqa: N815
    ttlMinutes: int | None = None  # noqa: N815
    # Contracts §9; keys are checked by validate_scenario_overrides (unknown -> 400).
    scenarioOverrides: dict[str, Any] | None = None  # noqa: N815


class _TrafficBody(BaseModel):
    episodes: int
    horizon: int | None = None


async def _get_session(app_name: str, user_id: str, session_id: str):
    from google.adk.errors.session_not_found_error import SessionNotFoundError

    try:
        return await _SESSION_SERVICE.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
    except SessionNotFoundError:
        return None


async def _owned(user_id: str, experiment_id: str) -> dict:
    row = await _STORE.get(experiment_id)
    if row is None or row.get("user_id") != user_id:
        # 404 for a foreign experiment too: don't leak existence.
        raise _error(404, "not_found", "experiment not found")
    return row


def _check_choice(value: str, allowed: Sequence[str], field: str) -> None:
    if value not in allowed:
        raise _error(400, f"invalid_{field}", f"{field} must be one of {list(allowed)}")


@router.post("/experiments", status_code=201)
async def http_create_experiment(body: _CreateBody, request: Request) -> dict:
    try:
        user_id = authorize_body_user(_AUTHZ_MODE, trusted_user(request), body.userId)
    except UserAuthzError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    _check_choice(body.scenario, SCENARIOS, "scenario")
    _check_choice(body.ctrMode, CTR_MODES, "ctr_mode")
    _check_choice(body.rewardMode, REWARD_MODES, "reward_mode")
    try:
        overrides = validate_scenario_overrides(body.scenario, body.scenarioOverrides)
    except ScenarioOverridesError as exc:
        raise _error(
            400, "invalid_scenario_overrides", str(exc), field=exc.field
        ) from exc
    session = await _get_session(body.appName, user_id, body.sessionId)
    if session is None:
        raise _error(404, "session_not_found", "session not found")
    state = dict(session.state or {})
    try:
        arms = snapshot_arms(
            state,
            body.sessionId,
            body.creativeIndices,
            default_bucket=_SETTINGS.gcs_bucket,
        )
    except SelectionError as exc:
        raise _error(400, exc.reason, str(exc)) from exc

    async with _lock(f"user:{user_id}"):
        active = [
            r
            for r in await _STORE.list_for_user(user_id)
            if r["status"] in ACTIVE_STATUSES
        ]
        if active:
            raise _error(
                409,
                "active_experiment",
                "stop the active experiment before deploying another",
                experimentId=active[0]["experiment_id"],
            )
        experiment_id = uuid.uuid4().hex[:16]
        config = build_experiment_config(
            experiment_id,
            arms,
            body.scenario,
            body.ctrMode,
            body.rewardMode,
            styles=visual_styles(state, arms),
            horizon=_SETTINGS.default_horizon,
            batch_size=_SETTINGS.batch_size,
            episodes=_SETTINGS.default_episodes,
            scenario_overrides=overrides,
        )
        config_uri = f"{_SETTINGS.artifacts_prefix}/{experiment_id}/experiment.json"
        writer = _SETTINGS.config_writer or MemoryConfigWriter()
        try:
            await writer(config_uri, config)
        except Exception as exc:
            log.exception("bandit config write %s failed", config_uri)
            raise _error(
                502, "config_write_failed", "could not write experiment.json"
            ) from exc
        now = _utcnow()
        row: dict[str, Any] = {
            "experiment_id": experiment_id,
            "user_id": user_id,
            "session_id": body.sessionId,
            "app_name": body.appName,
            "created_at": now,
            "updated_at": now,
            "status": "deploying",
            "scenario": body.scenario,
            "ctr_mode": body.ctrMode,
            "reward_mode": body.rewardMode,
            "arms": arms,
            "config_uri": config_uri,
            "model_resource": None,
            "endpoint_id": None,
            "deployed_model_id": None,
            "ttl_expires_at": now
            + dt.timedelta(minutes=_SETTINGS.clamp_ttl(body.ttlMinutes)),
            "stopped_at": None,
            "traffic_execution": None,
            "progress": None,
            "error": None,
        }
        # Only written when set: a default experiment then never touches the
        # column, so it keeps working against a table not yet migrated (§9).
        if overrides:
            row["scenario_overrides"] = overrides
        await _STORE.upsert(row)
    _start_deploy(experiment_id)
    return {"experimentId": experiment_id, "status": "deploying"}


@router.get("/experiments/{user_id}")
async def http_list_experiments(user_id: str) -> dict:
    rows = await _STORE.list_for_user(user_id)
    return {"experiments": [to_summary(r) for r in rows]}


@router.get("/experiments/{user_id}/{experiment_id}")
async def http_get_experiment(user_id: str, experiment_id: str) -> dict:
    row = await _owned(user_id, experiment_id)
    return to_summary(await reconcile(row))


@router.get("/experiments/{user_id}/{experiment_id}/metrics")
async def http_get_metrics(user_id: str, experiment_id: str) -> dict:
    row = await _owned(user_id, experiment_id)
    rows = await _STORE.metrics_rows(experiment_id)
    arm_order = [a.get("creativeId") for a in row.get("arms") or []]
    return aggregate_episode_metrics(rows, experiment_id, arm_order=arm_order)


def _series_cache_get(experiment_id: str) -> dict | None:
    hit = _SERIES_CACHE.get(experiment_id)
    if hit is None:
        return None
    expires_at, body = hit
    if expires_at is not None and time.monotonic() >= expires_at:
        _SERIES_CACHE.pop(experiment_id, None)
        return None
    _SERIES_CACHE.move_to_end(experiment_id)
    return body


def _series_cache_put(experiment_id: str, status: str, body: dict) -> None:
    if status in SERIES_FINAL_STATUSES:
        expires_at = None
    elif status == "running_traffic":
        expires_at = time.monotonic() + SERIES_LIVE_TTL_SECONDS
    else:
        return
    _SERIES_CACHE[experiment_id] = (expires_at, body)
    _SERIES_CACHE.move_to_end(experiment_id)
    while len(_SERIES_CACHE) > SERIES_CACHE_MAX:
        _SERIES_CACHE.popitem(last=False)


@router.get("/experiments/{user_id}/{experiment_id}/creatives")
async def http_get_creative_series(user_id: str, experiment_id: str) -> dict:
    """Per-creative windowed time series from ``bandit_events`` (contracts §8)."""
    row = await _owned(user_id, experiment_id)
    cached = _series_cache_get(experiment_id)
    if cached is not None:
        return cached
    raw = await _STORE.creative_series_rows(experiment_id)
    body = build_creative_series(
        raw.get("series", []),
        raw.get("segments", []),
        raw.get("true_ctr", []),
        row.get("arms") or [],
        experiment_id=experiment_id,
        creative_segment_rows=raw.get("creative_segments", []),
        reward_mode=str(row.get("reward_mode") or "click"),
    )
    _series_cache_put(experiment_id, str(row.get("status") or ""), body)
    return body


@router.post("/experiments/{user_id}/{experiment_id}/traffic")
async def http_start_traffic(
    user_id: str, experiment_id: str, body: _TrafficBody
) -> dict:
    lo, hi = EPISODES_RANGE
    if not lo <= body.episodes <= hi:
        raise _error(400, "invalid_episodes", f"episodes must be in [{lo}, {hi}]")
    if body.horizon is not None:
        lo, hi = HORIZON_RANGE
        if not lo <= body.horizon <= hi:
            raise _error(400, "invalid_horizon", f"horizon must be in [{lo}, {hi}]")
    row = await reconcile(await _owned(user_id, experiment_id))
    if (
        row["status"] != "ready"
        or is_expired(row, _utcnow())
        or experiment_id in _TRAFFIC_STARTING
    ):
        raise _error(
            409,
            "not_ready",
            "traffic can only start when the experiment is ready",
            status=row["status"],
        )
    _TRAFFIC_STARTING.add(experiment_id)
    try:
        try:
            execution = await _JOBS.run(
                experiment_id=experiment_id,
                config_uri=row["config_uri"],
                endpoint_id=row["endpoint_id"],
                episodes=body.episodes,
                horizon=body.horizon,
            )
        except Exception as exc:
            log.exception("bandit traffic start %s failed", experiment_id)
            raise _error(
                502, "traffic_start_failed", "could not start the traffic job"
            ) from exc
        updated, applied = await _transition(
            experiment_id,
            "traffic_started",
            traffic_execution=execution,
            progress={"episodes_done": 0, "episodes_total": body.episodes},
            error=None,
        )
    finally:
        _TRAFFIC_STARTING.discard(experiment_id)
    if not applied:
        raise _error(
            409,
            "not_ready",
            "the experiment changed state while starting traffic",
            status=(updated or row)["status"],
        )
    return {"status": "running_traffic", "execution": execution}


@router.post("/experiments/{user_id}/{experiment_id}/stop")
async def http_stop_experiment(user_id: str, experiment_id: str) -> dict:
    row = await _owned(user_id, experiment_id)
    if row["status"] in TERMINAL_STATUSES:
        return {"status": row["status"]}
    if row["status"] != "stopping":
        await _transition(experiment_id, "stop")
    await _wait_briefly(_start_teardown(experiment_id, "torn_down"))
    current = await _STORE.get(experiment_id)
    return {"status": (current or row)["status"]}
