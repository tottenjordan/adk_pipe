"""Context feature encoding for the bandit (contracts §1 features, §4 context)."""

import numpy as np
import pytest

from bandit import features

BASE_CTX = {
    "devicetype": "mobile",
    "os": "ios",
    "connectiontype": "wifi",
    "region": "northeast",
    "age_bucket": "21-34",
    "daypart": "morning",
    "weekend": False,
    "topic_matches_trend": False,
    "interest_matches_product": False,
    "freq_24h": "0",
}


def test_version_constant():
    assert features.FEATURE_SPEC_VERSION == "ctx-v1"


def test_dimension_and_order():
    names = features.feature_names()
    assert names[0] == "bias"
    assert len(names) == features.DIM == 19
    assert len(set(names)) == len(names)
    # groups appear in contract §4 order, reference level dropped
    assert names[1:3] == ["devicetype=desktop", "devicetype=tablet"]
    assert "devicetype=mobile" not in names
    assert "region=northeast" not in names
    assert names[-2:] == ["freq_24h=1", "freq_24h=2+"]


def test_reference_context_is_bias_only():
    x = features.encode_context(BASE_CTX)
    assert x.shape == (features.DIM,)
    expected = np.zeros(features.DIM)
    expected[0] = 1.0
    np.testing.assert_array_equal(x, expected)


def test_one_hot_correctness():
    ctx = dict(
        BASE_CTX,
        devicetype="tablet",
        region="west",
        weekend=True,
        freq_24h="2+",
        topic_matches_trend=True,
    )
    x = features.encode_context(ctx)
    names = features.feature_names()
    on = {names[i] for i in np.flatnonzero(x)}
    assert on == {
        "bias",
        "devicetype=tablet",
        "region=west",
        "weekend",
        "freq_24h=2+",
        "topic_matches_trend",
    }
    assert set(np.unique(x)) <= {0.0, 1.0}


def test_freq_accepts_int():
    x_int = features.encode_context(dict(BASE_CTX, freq_24h=5))
    x_str = features.encode_context(dict(BASE_CTX, freq_24h="2+"))
    np.testing.assert_array_equal(x_int, x_str)


@pytest.mark.parametrize(
    "key", ["user_id", "ip", "lat", "longitude", "zip", "city", "gender", "device_id"]
)
def test_sensitive_keys_rejected(key):
    with pytest.raises(ValueError, match="sensitive"):
        features.encode_context(dict(BASE_CTX, **{key: "x"}))


def test_unknown_key_rejected():
    with pytest.raises(ValueError, match="unknown"):
        features.encode_context(dict(BASE_CTX, favourite_colour="blue"))


def test_unknown_level_rejected():
    with pytest.raises(ValueError, match="devicetype"):
        features.encode_context(dict(BASE_CTX, devicetype="smart_fridge"))


def test_under_21_rejected():
    with pytest.raises(ValueError):
        features.encode_context(dict(BASE_CTX, age_bucket="13-17"))


def test_missing_key_rejected():
    ctx = dict(BASE_CTX)
    del ctx["os"]
    with pytest.raises(ValueError, match="missing"):
        features.encode_context(ctx)


def test_bool_fields_require_bool():
    with pytest.raises(ValueError, match="weekend"):
        features.encode_context(dict(BASE_CTX, weekend="yes"))


def test_encode_batch():
    ctxs = [BASE_CTX, dict(BASE_CTX, os="android")]
    X = features.encode_batch(ctxs)
    assert X.shape == (2, features.DIM)
    np.testing.assert_array_equal(X[1], features.encode_context(ctxs[1]))
    assert features.encode_batch([]).shape == (0, features.DIM)


def test_levels_roundtrip():
    """decode_levels(levels) -> contexts that re-encode to levels_to_matrix(levels)."""
    rng = np.random.default_rng(0)
    sizes = [len(levels) for _, levels in features.CONTEXT_SPEC]
    levels = np.stack([rng.integers(0, s, size=50) for s in sizes], axis=1)
    ctxs = features.decode_levels(levels)
    np.testing.assert_array_equal(
        features.encode_batch(ctxs), features.levels_to_matrix(levels)
    )
    assert set(ctxs[0]) == set(features.CONTEXT_KEYS)
