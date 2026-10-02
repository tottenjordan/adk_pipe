"""Cloud Run Job entrypoint / CLI for the bandit synthetic-traffic generator.

    # Cloud Run Job (env from the api's execution overrides):
    EXPERIMENT_ID=... CONFIG_URI=gs://.../experiment.json \\
      ENDPOINT_ID=projects/P/locations/R/endpoints/N EPISODES=20 \\
      BQ_PROJECT_ID=... BQ_DATASET_ID=... python -m bandit_traffic.main

    # Local, no GCP: in-process fake endpoint, JSONL instead of BigQuery
    python -m bandit_traffic.main --in-process --config /tmp/experiment.json \\
      --episodes 2 --horizon 4000 --dry-run --out /tmp/traffic

Every setting is a flag or the env var named in its help. Exactly one target:
``--in-process`` (the ``fake_endpoint`` stand-in), ``--local-url`` (a local CPR
container's predict URL) or ``ENDPOINT_ID`` (a Vertex endpoint, full resource
name). Exit codes: 0 done, 1 run failure (endpoint / BigQuery / error rate),
2 bad configuration.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from bandit.config import (
    REWARD_MODES,
    ExperimentConfig,
    load_experiment_config,
    validate_experiment_config,
)
from bandit_traffic import bq
from bandit_traffic.endpoint_client import (
    EndpointClient,
    EndpointError,
    HttpClient,
    InProcessClient,
    VertexEndpointClient,
)
from bandit_traffic.traffic import TrafficError, TrafficSettings, run_traffic

log = logging.getLogger("bandit_traffic")

EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2


class ConfigError(ValueError):
    pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m bandit_traffic.main")
    p.add_argument("--experiment-id", help="env EXPERIMENT_ID (default: config's)")
    p.add_argument("--config", help="env CONFIG_URI: gs:// or local experiment.json")
    target = p.add_mutually_exclusive_group()
    target.add_argument("--endpoint-id", help="env ENDPOINT_ID (full resource name)")
    target.add_argument("--local-url", help="env LOCAL_URL: local CPR predict URL")
    target.add_argument(
        "--in-process", action="store_true", help="use the in-process fake endpoint"
    )
    p.add_argument("--episodes", type=int, help="env EPISODES (default: config)")
    p.add_argument("--horizon", type=int, help="env HORIZON (default: config)")
    p.add_argument("--batch-size", type=int, help="env BATCH_SIZE (default: config)")
    p.add_argument("--reward-mode", choices=REWARD_MODES, help="env REWARD_MODE")
    p.add_argument(
        "--error-threshold",
        type=float,
        help="env ERROR_THRESHOLD: max decision error rate per episode (0.05)",
    )
    p.add_argument("--dry-run", action="store_true", help="write JSONL, not BigQuery")
    p.add_argument("--out", default="traffic_out", help="--dry-run output directory")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p


def _pick(flag: Any, env: Mapping[str, str], name: str) -> Any:
    if flag is not None and flag is not False:
        return flag
    value = env.get(name, "").strip()
    return value or None


def _int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def read_config_source(uri: str, storage_client: Any = None) -> dict:
    """``experiment.json`` from ``gs://bucket/path`` or a local path."""
    if uri.startswith("gs://"):
        if storage_client is None:
            from google.cloud import storage

            storage_client = storage.Client()
        from google.cloud.storage import Blob

        text = Blob.from_string(uri, client=storage_client).download_as_text()
    else:
        text = Path(uri).read_text()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ConfigError(f"{uri}: experiment config must be a JSON object")
    return data


def resolve_config(
    args: argparse.Namespace,
    env: Mapping[str, str],
    *,
    loader=read_config_source,
) -> tuple[ExperimentConfig, str]:
    """Load the experiment config and apply the overrides; returns ``(cfg, id)``."""
    uri = _pick(args.config, env, "CONFIG_URI")
    if not uri:
        raise ConfigError("CONFIG_URI (--config) is required")
    try:
        cfg = load_experiment_config(loader(uri))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"bad experiment config {uri}: {exc}") from exc
    overrides: dict[str, Any] = {}
    for field, flag, name in (
        ("episodes", args.episodes, "EPISODES"),
        ("horizon", args.horizon, "HORIZON"),
        ("batch_size", args.batch_size, "BATCH_SIZE"),
    ):
        value = _int(_pick(flag, env, name), name)
        if value is not None:
            overrides[field] = value
    reward_mode = _pick(args.reward_mode, env, "REWARD_MODE")
    if reward_mode:
        overrides["reward_mode"] = reward_mode
    if overrides:
        cfg = dataclasses.replace(cfg, **overrides)
        if "horizon" in overrides and "batch_size" not in overrides:
            cfg = dataclasses.replace(cfg, batch_size=min(cfg.batch_size, cfg.horizon))
        try:
            cfg = validate_experiment_config(cfg)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    experiment_id = _pick(args.experiment_id, env, "EXPERIMENT_ID") or cfg.experiment_id
    if experiment_id != cfg.experiment_id:
        log.warning(
            "EXPERIMENT_ID %s != config experiment_id %s; rows use %s",
            experiment_id,
            cfg.experiment_id,
            experiment_id,
        )
    return cfg, experiment_id


def build_client(
    args: argparse.Namespace, env: Mapping[str, str], cfg: ExperimentConfig
) -> EndpointClient:
    if args.in_process:
        from bandit_traffic.fake_endpoint import FakeBanditEndpoint

        return InProcessClient(FakeBanditEndpoint(cfg))
    url = _pick(args.local_url, env, "LOCAL_URL")
    if url:
        return HttpClient(url)
    endpoint_id = _pick(args.endpoint_id, env, "ENDPOINT_ID")
    if endpoint_id:
        try:
            return VertexEndpointClient(endpoint_id)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    raise ConfigError("need one of ENDPOINT_ID, --local-url or --in-process")


def build_writer(args: argparse.Namespace, env: Mapping[str, str]) -> bq.RowWriter:
    tables = bq.table_names(env)
    if args.dry_run:
        return bq.JsonlWriter(args.out, tables)
    missing = [n for n in ("BQ_PROJECT_ID", "BQ_DATASET_ID") if not env.get(n)]
    if missing:
        raise ConfigError(f"missing {', '.join(missing)} (or pass --dry-run)")
    return bq.BigQueryWriter(tables)


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    client: EndpointClient | None = None,
    writer: bq.RowWriter | None = None,
) -> int:
    """Run the job. ``client`` / ``writer`` override the env-built ones (tests)."""
    env = os.environ if env is None else env
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg, experiment_id = resolve_config(args, env)
        client = client or build_client(args, env, cfg)
        writer = writer or build_writer(args, env)
        threshold = _pick(args.error_threshold, env, "ERROR_THRESHOLD")
        settings = TrafficSettings(
            error_threshold=float(threshold) if threshold is not None else 0.05
        )
    except (ConfigError, ValueError) as exc:
        log.error("configuration error: %s", exc)
        return EXIT_CONFIG

    log.info(
        "experiment %s: %s/%s/%s, %d arms, %d episodes x %d rounds (batch %d)",
        experiment_id,
        cfg.scenario,
        cfg.ctr_mode,
        cfg.reward_mode,
        len(cfg.arms),
        cfg.episodes,
        cfg.horizon,
        cfg.batch_size,
    )
    try:
        summary = run_traffic(
            cfg, client, writer, settings, experiment_id=experiment_id
        )
    except (TrafficError, EndpointError, bq.BigQueryWriteError) as exc:
        log.error("traffic run failed: %s", exc)
        return EXIT_FAILED
    except Exception:
        log.exception("traffic run failed unexpectedly")
        return EXIT_FAILED

    log.info(
        "done: %d episodes, %d events, %d requests, %d decision errors, "
        "%d reward errors, %d rewards rejected",
        summary.episodes_done,
        summary.events_written,
        summary.requests,
        summary.decision_errors,
        summary.reward_errors,
        summary.rewards_rejected,
    )
    for policy, regret in summary.regret_by_policy().items():
        print(f"{policy:20s} mean cumulative_regret {regret:10.3f}", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
