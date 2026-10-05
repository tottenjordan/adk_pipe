"""CPR ``BanditPredictor`` (contracts §2), driven in-process: no docker, no GCP."""

import json
import threading

import numpy as np
import pytest
from fastapi import HTTPException

from bandit.config import scenario_noise_var
from bandit_serving.predictor import BanditPredictor, resolve_params

ARMS = [
    {"creative_id": "c1", "label": "One", "scores": {"overall": 0.8}},
    {"creative_id": "c2", "label": "Two", "scores": {"overall": 0.6}},
    {"creative_id": "c3", "label": "Three", "scores": {"overall": 0.7}},
]
CTX = {
    "devicetype": "mobile",
    "os": "ios",
    "connectiontype": "wifi",
    "region": "south",
    "age_bucket": "21-34",
    "daypart": "evening",
    "weekend": False,
    "topic_matches_trend": True,
    "interest_matches_product": False,
    "freq_24h": 1,
}
IDS = {"c1", "c2", "c3"}


def _write_config(path, **over):
    cfg = {"experiment_id": "exp1", "arms": ARMS, "scenario": "clear_winner"}
    cfg.update(over)
    path.mkdir(parents=True, exist_ok=True)
    (path / "experiment.json").write_text(json.dumps(cfg))
    return path


def _predictor(path, **kw):
    kw.setdefault("checkpoint_every", 10_000)
    kw.setdefault("checkpoint_seconds", 10_000.0)
    p = BanditPredictor(**kw)
    p.load(str(path))
    return p


def run(p, instances, parameters=None):
    body = {"instances": instances}
    if parameters is not None:
        body["parameters"] = parameters
    out = p.postprocess(p.predict(p.preprocess(body)))
    assert set(out) == {"predictions"}
    assert len(out["predictions"]) == len(instances)
    return out["predictions"]


def _decisions(n, prefix="r", **extra):
    return [
        {"type": "decision", "request_id": f"{prefix}{i}", "ts": i, "context": CTX}
        | extra
        for i in range(n)
    ]


def _rewards(preds, reward=1.0):
    return [
        {
            "type": "reward",
            "request_id": d["request_id"],
            "arm": d["chosen_arm"],
            "reward": reward,
            "clicked": int(reward > 0),
        }
        for d in preds
    ]


def _state(p):
    return run(p, [{"type": "state"}])[0]


@pytest.fixture(autouse=True)
def _no_warmup(monkeypatch):
    monkeypatch.setenv("BANDIT_WARMUP", "0")  # fast tests; covered separately


@pytest.fixture
def art(tmp_path):
    return _write_config(tmp_path / "artifacts")


def test_load_without_checkpoint(art):
    p = _predictor(art)
    s = _state(p)
    assert s["type"] == "state"
    assert (s["episode"], s["step"], s["model_version"]) == (0, 0, "exp1-e0-v0")
    assert s["pulls"] == {"c1": 0, "c2": 0, "c3": 0}
    assert set(s["posterior_mean"]) == IDS
    assert all(len(v) == 19 for v in s["posterior_mean"].values())
    assert s["feature_spec_version"] == "ctx-v1"


def test_load_from_file_uri(art):
    assert _state(_predictor(f"file://{art}"))["model_version"] == "exp1-e0-v0"


def test_decision_shape_and_propensities(art):
    p = _predictor(art)
    preds = run(p, _decisions(20))
    for i, d in enumerate(preds):
        assert d["type"] == "decision" and d["request_id"] == f"r{i}"
        assert d["chosen_arm"] in IDS
        assert ARMS[d["arm_index"]]["creative_id"] == d["chosen_arm"]
        probs = d["arm_probabilities"]
        assert set(probs) == IDS
        assert sum(probs.values()) == pytest.approx(1.0, abs=1e-5)
        assert min(probs.values()) >= 0.02 - 1e-6  # min_propensity floor
        assert d["propensity"] == pytest.approx(probs[d["chosen_arm"]])
        assert isinstance(d["explored"], bool)
        assert d["policy"] == "linear_ts"
        assert d["model_version"] == "exp1-e0-v0"
        assert (d["episode"], d["step"]) == (0, 0)
        assert d["latency_ms"] >= 0
    json.dumps(preds)  # plain JSON types only


def test_eligible_arms_respected(art):
    p = _predictor(art)
    preds = run(p, _decisions(10, eligible_arms=["c2"]))
    assert {d["chosen_arm"] for d in preds} == {"c2"}
    assert preds[0]["arm_probabilities"]["c1"] == 0.0
    assert preds[0]["arm_probabilities"]["c2"] == pytest.approx(1.0)


def test_reward_updates_and_bumps_version(art):
    p = _predictor(art)
    preds = run(p, _decisions(5))
    acks = run(p, _rewards(preds))
    assert all(a == {
        "type": "reward", "request_id": d["request_id"], "accepted": True,
        "model_version": "exp1-e0-v1",
    } for a, d in zip(acks, preds, strict=True))  # fmt: skip
    s = _state(p)
    assert s["step"] == 5 and sum(s["pulls"].values()) == 5
    assert s["model_version"] == "exp1-e0-v1"
    # the next decision batch reports the new version
    assert run(p, _decisions(2, "n"))[0]["model_version"] == "exp1-e0-v1"


def test_duplicate_unknown_and_mismatched_rewards_rejected(art):
    p = _predictor(art)
    preds = run(p, _decisions(3))
    first = run(p, _rewards(preds[:1]))
    assert first[0]["accepted"] is True
    wrong_arm = next(a for a in IDS if a != preds[1]["chosen_arm"])
    acks = run(
        p,
        _rewards(preds[:1])  # duplicate across requests
        + _rewards(preds[2:3]) * 2  # duplicate within one request
        + [
            {"type": "reward", "request_id": "nope", "arm": "c1", "reward": 1.0,
             "clicked": 1},
            {"type": "reward", "request_id": "r1", "arm": wrong_arm, "reward": 1.0,
             "clicked": 1},
        ],
    )  # fmt: skip
    assert [a["accepted"] for a in acks] == [False, True, False, False, False]
    assert all(a.get("reason") for a in acks if not a["accepted"])
    s = _state(p)
    assert s["step"] == 2 and s["model_version"] == "exp1-e0-v2"


def test_rejected_only_rewards_do_not_bump_version(art):
    p = _predictor(art)
    run(p, [{"type": "reward", "request_id": "x", "arm": "c1", "reward": 0.0,
             "clicked": 0}])  # fmt: skip
    assert _state(p)["model_version"] == "exp1-e0-v0"


def test_reset_clears_state(art):
    p = _predictor(art)
    preds = run(p, _decisions(4))
    run(p, _rewards(preds[:2]))
    out = run(p, [{"type": "reset", "episode": 3, "seed": 11}])[0]
    assert out == {"type": "reset", "episode": 3, "model_version": "exp1-e3-v0"}
    s = _state(p)
    assert s["step"] == 0 and sum(s["pulls"].values()) == 0
    assert s["episode"] == 3
    # decisions from the old episode can't be rewarded any more
    assert run(p, _rewards(preds[2:3]))[0]["accepted"] is False


def test_reset_seed_is_deterministic(art):
    a, b = _predictor(art), _predictor(art)
    for p in (a, b):
        run(p, [{"type": "reset", "episode": 1, "seed": 7}])
    pick = [d["chosen_arm"] for d in run(a, _decisions(30))]
    assert pick == [d["chosen_arm"] for d in run(b, _decisions(30))]


def test_mixed_batch_preserves_order(art):
    p = _predictor(art)
    d0 = run(p, _decisions(1, "a"))[0]
    preds = run(
        p,
        [
            *_rewards([d0]),
            {"type": "state"},
            *_decisions(2, "b"),
            "junk",
            {"type": "reset", "episode": 2, "seed": 1},
            {"type": "state"},
        ],
    )
    assert [x["type"] for x in preds] == [
        "reward", "state", "decision", "decision", "error", "reset", "state",
    ]  # fmt: skip
    assert preds[0]["accepted"] is True
    assert preds[1]["step"] == 1  # state sees the reward that preceded it
    assert preds[2]["model_version"] == "exp1-e0-v1"
    assert preds[6]["step"] == 0 and preds[6]["episode"] == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"request_id": "x"},  # no type
        {"type": "teleport", "request_id": "x"},
        {"type": "decision", "request_id": "x", "context": {**CTX, "ip": "1.2.3.4"}},
        {"type": "decision", "request_id": "x", "context": {"devicetype": "mobile"}},
        {"type": "decision", "request_id": "x", "context": CTX, "eligible_arms": ["zz"]},
        {"type": "decision", "request_id": "x", "context": CTX, "eligible_arms": []},
        {"type": "decision", "context": CTX},  # no request_id
        {"type": "reward", "request_id": "x", "arm": "c1", "reward": "lots",
         "clicked": 1},
        {"type": "reward", "request_id": "x", "arm": "c1", "reward": float("nan"),
         "clicked": 1},
        {"type": "reward", "request_id": "x", "arm": "c1", "reward": 1.0, "clicked": 3},
        {"type": "reset", "episode": -1, "seed": 0},
        {"type": "reset", "episode": "1", "seed": 0},
        42,
    ],
)  # fmt: skip
def test_invalid_instance_is_a_per_instance_error(art, bad):
    p = _predictor(art)
    preds = run(p, [*_decisions(1, "ok"), bad, *_decisions(1, "ok2")])
    assert preds[0]["type"] == preds[2]["type"] == "decision"
    err = preds[1]
    assert err["type"] == "error" and err["error"]
    assert "request_id" in err
    if isinstance(bad, dict) and "request_id" in bad:
        assert err["request_id"] == bad["request_id"]


def test_malformed_request_is_400(art):
    p = _predictor(art)
    for body in (
        {"nope": []},
        [],
        {"instances": "x"},
        {"instances": [], "parameters": 1},
    ):
        with pytest.raises(HTTPException) as e:
            p.preprocess(body)
        assert e.value.status_code == 400


def test_instance_limit(art):
    p = _predictor(art)
    assert len(run(p, _decisions(1000))) == 1000
    with pytest.raises(HTTPException) as e:
        p.preprocess({"instances": [{"type": "state"}] * 1001})
    assert e.value.status_code == 400


def test_traffic_job_request_id_format(art):
    """PR 3 ids look like ``{experiment_id}-e{episode}-r{round}``."""
    p = _predictor(art)
    preds = run(p, _decisions(3, "exp1-e0-r"))
    assert [d["request_id"] for d in preds] == [
        "exp1-e0-r0",
        "exp1-e0-r1",
        "exp1-e0-r2",
    ]
    assert all(a["accepted"] for a in run(p, _rewards(preds)))


def test_parameters_are_clamped(art):
    p = _predictor(art)
    base = p.params
    hi = resolve_params(base, {"exploration_scale": 100, "propensity_samples": 10**6})
    assert (hi.exploration_scale, hi.propensity_samples) == (5.0, 5000)
    lo = resolve_params(base, {"exploration_scale": 0, "propensity_samples": 1})
    assert (lo.exploration_scale, lo.propensity_samples) == (0.1, 100)
    assert resolve_params(base, {"exploration_scale": "x"}) == base
    q = resolve_params(base, {"exploration_scale": 1.234, "propensity_samples": 1249})
    assert (q.exploration_scale, q.propensity_samples) == (1.2, 1200)  # quantised
    assert resolve_params(base, None) == base
    preds = run(p, _decisions(3), parameters={"exploration_scale": 50})
    assert all(d["type"] == "decision" for d in preds)


def test_noise_var_calibrated_when_absent(tmp_path):
    p = _predictor(_write_config(tmp_path / "a", ctr_mode="realistic"))
    assert p.params.noise_var == scenario_noise_var(
        "clear_winner", "realistic", "click"
    )
    explicit = _write_config(tmp_path / "b", policy={"noise_var": 0.3})
    assert _predictor(explicit).params.noise_var == 0.3
    none = _write_config(tmp_path / "c", policy={"noise_var": None, "discount": 0.99})
    q = _predictor(none)
    assert q.params.noise_var == scenario_noise_var("clear_winner", "demo", "click")
    assert q.params.discount == 0.99


def test_loads_config_with_scenario_overrides(tmp_path):
    ov = {
        "segment_mix": [0.6, 0.2, 0.2],
        "gap_scale": 1.5,
        "judge_wrong": 0.8,
        "noise_scale": 0,
        "drift_at_frac": 0.3,
    }
    p = _predictor(_write_config(tmp_path, scenario="drift", scenario_overrides=ov))
    assert p.config.scenario_overrides is not None
    assert p.config.scenario_overrides.judge_wrong == 0.8
    assert p.params.noise_var == scenario_noise_var("drift", "demo", "click")
    preds = run(p, _decisions(3))
    assert all(d["type"] == "decision" for d in preds)
    # unknown override keys are rejected by the strict loader
    with pytest.raises(ValueError, match="surprise"):
        _predictor(_write_config(tmp_path / "bad", scenario_overrides={"surprise": 1}))


def _flip_run(p, before: int, after: int, batch: int = 100) -> float:
    """c1 pays until ``before`` batches, then only c2 pays for ``after`` batches
    (the drift swap); returns c2's mean probability on a final decision batch."""
    for b in range(before + after):
        winner = "c1" if b < before else "c2"
        preds = run(p, _decisions(batch, prefix=f"b{b}-"))
        rewards = _rewards(preds)
        for r in rewards:
            r["reward"] = 1.0 if r["arm"] == winner else 0.0
            r["clicked"] = int(r["reward"] > 0)
        run(p, rewards)
    final = run(p, _decisions(batch, prefix="final-"))
    return float(np.mean([d["arm_probabilities"]["c2"] for d in final]))


def test_discounted_config_forgets_after_a_flip(tmp_path):
    """The endpoint honours ``policy.discount`` (contracts §7): after the best
    creative flips, a discounted posterior moves to the new winner while a
    full-memory one is still anchored to the old one."""
    arms = ARMS[:2]
    full = _predictor(
        _write_config(tmp_path / "full", arms=arms, scenario="drift", seed=3)
    )
    forget = _predictor(
        _write_config(
            tmp_path / "forget",
            arms=arms,
            scenario="drift",
            seed=3,
            policy={"discount": 0.8},
        )
    )
    assert full.params.discount == 1.0 and forget.params.discount == 0.8
    p_full = _flip_run(full, before=20, after=4, batch=50)
    p_forget = _flip_run(forget, before=20, after=4, batch=50)
    assert p_forget > 0.9
    assert p_full < 0.5
    assert _state(forget)["pulls"]["c2"] > 0


def test_engaged_rewards_are_scaled_by_base_dwell(tmp_path):
    click = _predictor(_write_config(tmp_path / "a", policy={"noise_var": 0.1}))
    engaged = _predictor(
        _write_config(tmp_path / "b", reward_mode="engaged", policy={"noise_var": 0.1})
    )
    for p, reward in ((click, 1.0), (engaged, 30.0)):
        run(p, [{"type": "reset", "episode": 0, "seed": 5}])
        preds = run(p, _decisions(6))
        run(p, _rewards(preds, reward))
    np.testing.assert_allclose(
        np.array(list(_state(click)["posterior_mean"].values())),
        np.array(list(_state(engaged)["posterior_mean"].values())),
        rtol=1e-5,
        atol=1e-6,
    )


def test_checkpoint_round_trip(art):
    p = _predictor(art, checkpoint_every=1)
    preds = run(p, _decisions(12))
    run(p, _rewards(preds, 1.0))
    p.flush()
    assert (art / "checkpoints" / "latest.json").exists()
    q = _predictor(art)
    s1, s2 = _state(p), _state(q)
    assert s1["model_version"] == s2["model_version"] == "exp1-e0-v1"
    assert s1["pulls"] == s2["pulls"] and s1["step"] == s2["step"] == 12
    for cid in IDS:
        np.testing.assert_allclose(
            s1["posterior_mean"][cid], s2["posterior_mean"][cid], rtol=1e-6
        )
    # the restored predictor keeps learning from the same version counter
    more = run(q, _decisions(2, "z"))
    assert run(q, _rewards(more))[0]["model_version"] == "exp1-e0-v2"


def test_checkpoint_on_reset_and_time(art):
    p = _predictor(art, checkpoint_every=10_000, checkpoint_seconds=0.0)
    run(p, [{"type": "reset", "episode": 4, "seed": 2}])
    p.flush()
    meta = json.loads((art / "checkpoints" / "latest.json").read_text())
    assert meta["episode"] == 4 and meta["model_version"] == "exp1-e4-v0"
    preds = run(p, _decisions(2))
    run(p, _rewards(preds))  # T = 0 s -> checkpoint on the next update
    p.flush()
    meta = json.loads((art / "checkpoints" / "latest.json").read_text())
    assert meta["model_version"] == "exp1-e4-v1"
    assert (art / "checkpoints" / meta["npz"]).exists()


def test_checkpoint_for_other_experiment_is_ignored(art, tmp_path):
    p = _predictor(art, checkpoint_every=1)
    run(p, _rewards(run(p, _decisions(3))))
    p.flush()
    cfg = json.loads((art / "experiment.json").read_text()) | {"experiment_id": "exp2"}
    (art / "experiment.json").write_text(json.dumps(cfg))
    assert _state(_predictor(art))["model_version"] == "exp2-e0-v0"


def test_checkpoint_failure_never_blocks_decisions(art, caplog):
    p = _predictor(art, checkpoint_every=1)

    def boom(*a, **k):
        raise OSError("disk on fire")

    p._storage.write_bytes = boom  # type: ignore[method-assign]
    for i in range(3):
        preds = run(p, _decisions(4, f"b{i}"))
        assert all(a["accepted"] for a in run(p, _rewards(preds)))
    p.flush()
    assert _state(p)["model_version"] == "exp1-e0-v3"
    assert "checkpoint" in caplog.text.lower()


def test_pending_map_is_bounded(art):
    p = _predictor(art, max_pending=3)
    preds = run(p, _decisions(5))
    acks = run(p, _rewards(preds))
    assert [a["accepted"] for a in acks] == [False, False, True, True, True]


def test_concurrency_smoke(art):
    p = _predictor(art)
    errors: list[BaseException] = []
    per_thread, rounds, batch = 6, 5, 4

    def worker(t):
        try:
            for r in range(rounds):
                preds = run(p, _decisions(batch, f"t{t}-{r}-"))
                assert all(d["type"] == "decision" for d in preds)
                assert all(a["accepted"] for a in run(p, _rewards(preds)))
                run(p, [{"type": "state"}])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(per_thread)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    s = _state(p)
    total = per_thread * rounds * batch
    assert s["step"] == total and sum(s["pulls"].values()) == total
    assert s["model_version"] == f"exp1-e0-v{per_thread * rounds}"


def test_warmup_buckets_and_warm_load(art, monkeypatch, caplog):
    from bandit_serving.predictor import warmup_buckets

    assert warmup_buckets() == []
    monkeypatch.setenv("BANDIT_WARMUP", "1")
    monkeypatch.setenv("BANDIT_WARMUP_BUCKETS", "1,20,x,5000")
    assert warmup_buckets() == [16, 32, 1024]
    monkeypatch.setenv("BANDIT_WARMUP_BUCKETS", "16")
    caplog.set_level("INFO", logger="bandit_serving.predictor")
    p = _predictor(art)
    assert "jit warm-up" in caplog.text
    assert _state(p)["step"] == 0  # warm-up never touches the live posterior
