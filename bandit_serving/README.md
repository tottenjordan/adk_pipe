# bandit_serving: CPR linear-TS endpoint

`BanditPredictor` (`predictor.py`) is the Vertex AI Custom Prediction Routine behind each
bandit experiment endpoint. It holds one in-memory `bandit.linear_ts` posterior.

- **Wire contract:** [`docs/bandit/contracts.md`](../docs/bandit/contracts.md) §2 covers the `decision` / `reward` / `reset` / `state` instances and `parameters`. §7 covers the PR 2 details: validation, reward acceptance, versions and checkpoints.
- **Image build:** [`deployment/bandit/build_image.py`](../deployment/bandit/build_image.py).
- **Model upload / endpoint:** [`deployment/bandit/endpoint.py`](../deployment/bandit/endpoint.py).

## Single worker (required)

CPR defaults to `max(cores, 2)` uvicorn workers, and each worker would learn from its own slice of the rewards. Always serve with **`VERTEX_CPR_WEB_CONCURRENCY=1`**:
- `endpoint.upload_model` sets it on the Model;
- `build_image.py --local-test` sets it on the local container.

The image itself does not bake it in. `load()` logs a warning if `WEB_CONCURRENCY != 1`.

## Local test (docker, no GCP)

```bash
uv run python deployment/bandit/build_image.py --local-test
# --base-image=python:3.12-slim   fallback base
# --push                          docker push the tag (needs Artifact Registry auth)
```

This builds `us-central1-docker.pkg.dev/$GOOGLE_CLOUD_PROJECT/cpr/trend-trawler-bandit:<git-sha>`. It serves the image with a sample `experiment.json` (3 fixture arms, no `noise_var`, so the calibrated default applies). It then round-trips `reset`, a 20-decision batch, rewards (including a duplicate), and a second batch, checking that `model_version` was bumped. Finally it sends `state` and an invalid instance, and prints timings.

For unit tests without docker, run `uv run pytest tests/test_bandit_predictor.py tests/test_build_image.py`.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `AIP_STORAGE_URI` | set by Vertex / the local endpoint | `gs://…` or local dir holding `experiment.json` + `checkpoints/` |
| `VERTEX_CPR_WEB_CONCURRENCY` | must be `1` | CPR worker count |
| `BANDIT_CHECKPOINT_EVERY` | `50` | checkpoint after this many reward batches |
| `BANDIT_CHECKPOINT_SECONDS` | `120` | …or this many seconds since the last checkpoint (checked on update) |
| `BANDIT_MAX_PENDING` / `BANDIT_MAX_SEEN` | `200000` / `400000` | bounds of the pending-decision map / seen-reward-id set |
| `BANDIT_WARMUP` / `BANDIT_WARMUP_BUCKETS` | `1` / `16,32,64,128` | jit pre-compile at load for these padded batch sizes |

## Checkpoint layout

```
{AIP_STORAGE_URI}/
  experiment.json                     # contracts §1 (written by the api)
  checkpoints/
    {experiment_id}-e{episode}-v{n}.npz   # precision (K,d,d), b (K,d), n (K,), step ()
    latest.json                       # {experiment_id, model_version, npz, episode, seed,
                                      #  n_updates, calls, feature_spec_version, saved_at}
```

Checkpoints are written in the background after every `BANDIT_CHECKPOINT_EVERY` reward batches or `BANDIT_CHECKPOINT_SECONDS`, and on every reset. On restart, `load()` restores `latest.json`.

Pending decisions and seen reward ids are not checkpointed, so a reward for a decision made before a restart comes back `accepted:false`.

The endpoint's service account needs write access to the artifacts prefix. If it lacks it, the writes fail, get logged, and serving continues.
