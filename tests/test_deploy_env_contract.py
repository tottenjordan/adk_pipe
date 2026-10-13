"""deploy.env ↔ ENV_VAR_DICT contract: every runtime knob reaches the engines.

Agent Engine cloudpickles module-level functions BY REFERENCE, so module globals
(``agent_common.genai_retry.MODEL_REQUEST_TIMEOUT_SECONDS``, ``BaseAgentConfiguration``
class defaults like ``image_size``) and lazily built configs (the ``creative_eval``
judge's ``EvalConfig``) are re-evaluated inside the engine from the ENGINE's env,
not the deployer's. A deploy.env key the engine reads at runtime therefore only
takes effect when deploy_agent.py ships it in ``ENV_VAR_DICT``. Every deploy.env
key must be shipped or listed below as a deploy input with a reason.
"""

import os

import dotenv
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROFILE = os.path.join(ROOT, "deployment", "deploy.env")

# deploy.env keys that are deliberately NOT shipped as engine env vars.
DEPLOY_INPUT_ONLY = {
    # Reserved by Agent Engine (FAILED_PRECONDITION "is reserved"); the engine
    # injects its own project. Used by the deployer (client + wrapper check).
    "GOOGLE_CLOUD_PROJECT",
    # Reserved by Agent Engine (injected as the engine's region); models are
    # pinned to `global` via MODEL_LOCATION instead.
    "GOOGLE_CLOUD_LOCATION",
    # The region the deployer creates the engine in (agentplatform client);
    # no agent package reads it.
    "GCP_REGION",
    # Read only by agent_common.safety.build_safety_plugins at agent-module
    # import; the resulting App (with its plugin list) is pickled BY VALUE, so
    # the deployer's values are captured at deploy.
    "MODEL_ARMOR_TEMPLATE",
    "MODEL_ARMOR_RESPONSE_TEMPLATE",
    "MODEL_ARMOR_FAIL_CLOSED",
}

# The knobs that were labelled "pickled at deploy" but are read in the engine.
RUNTIME_KNOBS = (
    "MODEL_REQUEST_TIMEOUT_SECONDS",
    "IMAGE_SIZE",
    "IMAGE_ASPECT_RATIO_DEFAULT",
    "EVAL_MODEL",
    "EVAL_MODEL_LOCATION",
    "EVAL_MAX_WORKERS",
    "EVAL_MAX_VISUAL_WAVE_WORKERS",
)


def _da():
    try:
        import deployment.deploy_agent as deploy_agent
    except ImportError as e:  # agentplatform/absl missing in a bare env
        pytest.skip(f"deploy_agent import unavailable: {e}")
    return deploy_agent


def _shippable(da) -> set[str]:
    """Keys ENV_VAR_DICT always ships plus those it ships only when set."""
    return set(da.ENV_VAR_DICT) | set(da.OPTIONAL_ENV_VARS)


def test_every_profile_key_is_shipped_or_deploy_input():
    da = _da()
    profile = dotenv.dotenv_values(PROFILE)
    shippable = _shippable(da)
    unaccounted = sorted(k for k in profile if k not in shippable | DEPLOY_INPUT_ONLY)
    assert not unaccounted, (
        f"deploy.env keys neither shipped in ENV_VAR_DICT nor in DEPLOY_INPUT_ONLY: "
        f"{unaccounted}"
    )


def test_deploy_input_only_keys_are_not_shipped():
    da = _da()
    assert not DEPLOY_INPUT_ONLY & _shippable(da)


def test_runtime_knobs_are_shipped():
    da = _da()
    shippable = _shippable(da)
    for key in (*RUNTIME_KNOBS, "CRITIC_FALLBACK_MODEL", "CAMPAIGN_RESEARCH_PLACEMENT"):
        assert key in shippable, key
    assert "MODEL_LOCATION" in da.ENV_VAR_DICT


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("IMAGE_SIZE", "2K"),
        ("IMAGE_ASPECT_RATIO_DEFAULT", "9:16"),
        ("MODEL_REQUEST_TIMEOUT_SECONDS", "240"),
        ("EVAL_MODEL", "gemini-3.1-pro-preview"),
        ("EVAL_MAX_WORKERS", "2"),
        ("EVAL_MAX_VISUAL_WAVE_WORKERS", "4"),
        ("CAMPAIGN_RESEARCH_PLACEMENT", "global_altbucket"),
        ("MODEL_LOCATION", "global"),
    ],
)
def test_shipped_defaults_match_config_defaults(key, expected):
    """With the knob unset, the shipped value equals the config parser's default."""
    da = _da()
    assert da.ENV_VAR_DICT[key] == os.environ.get(key, expected)


def test_config_parser_defaults_are_the_documented_ones(monkeypatch):
    """Pin the parser side of the defaults compared above."""
    from agent_common.genai_retry import DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS
    from creative_agent.config import DEFAULT_CAMPAIGN_ARM

    assert DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS == 240
    assert DEFAULT_CAMPAIGN_ARM == "global_altbucket"
    for key in ("EVAL_MODEL", "EVAL_MAX_WORKERS", "EVAL_MAX_VISUAL_WAVE_WORKERS"):
        monkeypatch.delenv(key, raising=False)
    from creative_eval.config import EvalConfig

    cfg = EvalConfig()
    assert cfg.eval_model == "gemini-3.1-pro-preview"
    assert cfg.max_eval_workers == 2
    assert cfg.max_visual_wave_workers == 4


class TestOptionalEnvVars:
    """CRITIC_FALLBACK_MODEL="" is a kill switch: ship it only when set, never invent it."""

    def test_unset_is_not_shipped(self, monkeypatch):
        da = _da()
        monkeypatch.delenv("CRITIC_FALLBACK_MODEL", raising=False)
        monkeypatch.delenv("EVAL_MODEL_LOCATION", raising=False)
        assert da.optional_env_vars() == {}

    def test_empty_kill_switch_is_shipped(self, monkeypatch):
        da = _da()
        monkeypatch.setenv("CRITIC_FALLBACK_MODEL", "")
        assert da.optional_env_vars()["CRITIC_FALLBACK_MODEL"] == ""

    def test_set_values_are_shipped(self, monkeypatch):
        da = _da()
        monkeypatch.setenv("CRITIC_FALLBACK_MODEL", "gemini-3.8-flash")
        monkeypatch.setenv("EVAL_MODEL_LOCATION", "us-central1")
        assert da.optional_env_vars() == {
            "CRITIC_FALLBACK_MODEL": "gemini-3.8-flash",
            "EVAL_MODEL_LOCATION": "us-central1",
        }

    def test_no_none_values_in_env_var_dict_optional_part(self, monkeypatch):
        da = _da()
        monkeypatch.delenv("CRITIC_FALLBACK_MODEL", raising=False)
        assert None not in da.optional_env_vars().values()
