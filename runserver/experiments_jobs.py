"""Synthetic-traffic job launcher (Cloud Run Jobs) behind a ``JobsRunner`` protocol.

``CloudRunJobsRunner.run`` starts one execution of the traffic job with per-run
container env overrides (``EXPERIMENT_ID``, ``CONFIG_URI``, ``ENDPOINT_ID``,
``EPISODES``, ``HORIZON``, per contracts §10 ``TRAFFIC_RUN``, ``FORGET`` and
``SHIFTS_JSON``, and per §11 ``LEARNING_MODE``) and returns the execution resource name without waiting
for it (the job can run for up to an hour). ``state`` maps an execution to
``running`` / ``succeeded`` / ``failed`` / ``unknown`` so the api can move a
``running_traffic`` experiment back to ``ready``. The google-cloud-run import is lazy.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from collections.abc import Callable
from typing import Any, Protocol


class JobsRunner(Protocol):
    async def run(
        self,
        *,
        experiment_id: str,
        config_uri: str,
        endpoint_id: str,
        episodes: int,
        horizon: int | None = None,
        traffic_run: int | None = None,
        forget: bool | None = None,
        shifts: list[dict] | None = None,
        learning: str | None = None,
    ) -> str:
        """Start a traffic execution; returns its resource name."""
        ...

    async def state(self, execution: str) -> str:
        """``running`` | ``succeeded`` | ``failed`` | ``unknown``."""
        ...


def build_env_overrides(
    *,
    experiment_id: str,
    config_uri: str,
    endpoint_id: str,
    episodes: int,
    horizon: int | None,
    traffic_run: int | None = None,
    forget: bool | None = None,
    shifts: list[dict] | None = None,
    learning: str | None = None,
) -> list[dict[str, str]]:
    """The job's per-execution env. ``shifts`` is the snake_case §10 job form
    (``SHIFTS_JSON``, compact JSON, set only when non-empty); ``traffic_run`` is the
    1-based run number (``TRAFFIC_RUN``) and ``forget`` the run's forgetting
    switch (``FORGET=true|false``). ``LEARNING_MODE=continuous`` (contracts §11)
    is set only for a continuous run; the job defaults to ``per_episode``."""
    env = {
        "EXPERIMENT_ID": experiment_id,
        "CONFIG_URI": config_uri,
        "ENDPOINT_ID": endpoint_id,
        "EPISODES": str(episodes),
    }
    if horizon is not None:
        env["HORIZON"] = str(horizon)
    if traffic_run is not None:
        env["TRAFFIC_RUN"] = str(int(traffic_run))
    if forget is not None:
        env["FORGET"] = "true" if forget else "false"
    if shifts:
        env["SHIFTS_JSON"] = json.dumps(shifts, separators=(",", ":"))
    if learning == "continuous":
        env["LEARNING_MODE"] = "continuous"
    return [{"name": k, "value": v} for k, v in env.items()]


def build_run_job_request(job_name: str, env: list[dict[str, str]]) -> dict:
    """The ``RunJobRequest`` (as a dict) for ``JobsClient.run_job`` (pure)."""
    return {"name": job_name, "overrides": {"container_overrides": [{"env": env}]}}


def execution_state(execution: Any) -> str:
    """Map a ``run_v2.Execution`` (or a look-alike) to a coarse state."""
    if not getattr(execution, "completion_time", None):
        return "running"
    failed = (getattr(execution, "failed_count", 0) or 0) + (
        getattr(execution, "cancelled_count", 0) or 0
    )
    return "failed" if failed else "succeeded"


class CloudRunJobsRunner:
    def __init__(
        self,
        job: str,
        *,
        project: str | None = None,
        region: str | None = None,
        jobs_client_factory: Callable[[], Any] | None = None,
        executions_client_factory: Callable[[], Any] | None = None,
    ):
        self.project = project or os.getenv("GOOGLE_CLOUD_PROJECT", "")
        self.region = region or os.getenv("GCP_REGION", "us-central1")
        self.job_name = (
            job
            if job.startswith("projects/")
            else f"projects/{self.project}/locations/{self.region}/jobs/{job}"
        )
        self._jobs_factory = jobs_client_factory or self._default_jobs_client
        self._exec_factory = executions_client_factory or self._default_exec_client

    @staticmethod
    def _default_jobs_client() -> Any:
        from google.cloud import run_v2

        return run_v2.JobsClient()

    @staticmethod
    def _default_exec_client() -> Any:
        from google.cloud import run_v2

        return run_v2.ExecutionsClient()

    def _run_blocking(self, request: dict) -> str:
        operation = self._jobs_factory().run_job(request=request)
        metadata = getattr(operation, "metadata", None)
        name = getattr(metadata, "name", "") if metadata is not None else ""
        if not name:  # fall back to the LRO name (still traceable in the console)
            name = getattr(getattr(operation, "operation", None), "name", "") or ""
        return name

    async def run(
        self,
        *,
        experiment_id: str,
        config_uri: str,
        endpoint_id: str,
        episodes: int,
        horizon: int | None = None,
        traffic_run: int | None = None,
        forget: bool | None = None,
        shifts: list[dict] | None = None,
        learning: str | None = None,
    ) -> str:
        env = build_env_overrides(
            experiment_id=experiment_id,
            config_uri=config_uri,
            endpoint_id=endpoint_id,
            episodes=episodes,
            horizon=horizon,
            traffic_run=traffic_run,
            forget=forget,
            shifts=shifts,
            learning=learning,
        )
        return await asyncio.to_thread(
            self._run_blocking, build_run_job_request(self.job_name, env)
        )

    async def state(self, execution: str) -> str:
        if "/executions/" not in execution:
            return "unknown"

        def _get() -> str:
            return execution_state(self._exec_factory().get_execution(name=execution))

        try:
            return await asyncio.to_thread(_get)
        except Exception:  # a status probe never fails the request
            return "unknown"


class FakeJobsRunner:
    """Records runs; ``finish(name, ok)`` flips an execution's state (tests/local)."""

    def __init__(self, auto_finish: bool = False):
        self.runs: list[dict] = []
        self.states: dict[str, str] = {}
        self.auto_finish = auto_finish
        self._seq = itertools.count(1)

    async def run(
        self,
        *,
        experiment_id: str,
        config_uri: str,
        endpoint_id: str,
        episodes: int,
        horizon: int | None = None,
        traffic_run: int | None = None,
        forget: bool | None = None,
        shifts: list[dict] | None = None,
        learning: str | None = None,
    ) -> str:
        name = f"projects/fake/locations/us-central1/jobs/traffic/executions/x{next(self._seq)}"
        self.runs.append(
            {
                "experiment_id": experiment_id,
                "config_uri": config_uri,
                "endpoint_id": endpoint_id,
                "episodes": episodes,
                "horizon": horizon,
                "traffic_run": traffic_run,
                "forget": forget,
                "shifts": shifts,
                "learning": learning,
                "execution": name,
            }
        )
        self.states[name] = "succeeded" if self.auto_finish else "running"
        return name

    async def state(self, execution: str) -> str:
        return self.states.get(execution, "unknown")

    def finish(self, execution: str, ok: bool = True) -> None:
        self.states[execution] = "succeeded" if ok else "failed"
