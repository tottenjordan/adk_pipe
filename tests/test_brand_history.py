"""Brand history (creative_agent/brand_history.py): read past runs for a brand."""

from __future__ import annotations

import datetime
import json
from typing import Any

import pytest

from creative_agent import brand_history as bh
from tests._fake_bq import FakeBigQueryClient

BUCKET = "tt-bucket"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(bh.config, "BQ_PROJECT_ID", "p")
    monkeypatch.setattr(bh.config, "BQ_DATASET_ID", "d")
    monkeypatch.setattr(bh.config, "BQ_TABLE_EVALS", "creative_evals")
    monkeypatch.setattr(bh.config, "GCS_BUCKET_NAME", BUCKET)


def _gate(name: str, passed: bool, advisory: bool = False) -> dict:
    return {"gate": name, "passed": passed, "note": "", "advisory": advisory}


def _score(overall: float, gates: list[dict] | None = None) -> dict:
    return {
        "overall_score": overall,
        "passed": overall >= 0.7,
        "verdicts": [],
        "strengths": [],
        "improvements": [],
        "gates": gates or [],
    }


def _report(visuals: list[tuple[str, float]], copies=(), gates=()) -> dict:
    return {
        "ad_copy_evaluations": [
            {
                "original_id": i,
                "headline": f"H{i}",
                "tone_style": tone,
                "angle_id": "A1",
                "score": _score(score, list(gates)),
            }
            for i, (tone, score) in enumerate(copies)
        ],
        "visual_concept_evaluations": [
            {
                "ad_copy_id": i,
                "concept_name": f"C{i}",
                "visual_style": style,
                "score": _score(score),
            }
            for i, (style, score) in enumerate(visuals)
        ],
    }


class FakeBlob:
    def __init__(self, data: bytes):
        self.size = len(data)
        self._data = data

    def download_as_bytes(self, **_: Any) -> bytes:
        return self._data


class FakeGcs:
    def __init__(self, objects: dict[str, bytes] | None = None, error=None):
        self.objects = objects or {}
        self.error = error
        self.reads: list[tuple[str, str]] = []

    def bucket(self, name: str):
        gcs = self

        class _Bucket:
            def get_blob(self, obj: str):
                gcs.reads.append((name, obj))
                if gcs.error:
                    raise gcs.error
                data = gcs.objects.get(f"{name}/{obj}")
                return None if data is None else FakeBlob(data)

        return _Bucket()


def _row(n: int, uri: str, weak: str = "") -> dict:
    return {
        "datetime": datetime.datetime(2026, 10, 7 - n),
        "target_trend": f"trend {n}",
        "overall_pass_rate": 0.75,
        "avg_ad_copy_score": 0.8,
        "avg_visual_score": 0.7,
        "weakest_dimension_labels": weak,
        "gates_pass_rate": 0.9,
        "eval_report_gcs_uri": uri,
    }


def _uri(n: int) -> str:
    return f"gs://{BUCKET}/runs/{n}/eval.json"


def _fixture():
    reports = {
        f"{BUCKET}/runs/1/eval.json": _report(
            [("Watercolor / gouache", 0.9), ("Comic panel", 0.6)],
            copies=[("Deadpan", 0.95), ("Earnest", 0.5)],
            gates=[_gate("product_named", False), _gate("brand_cue", False, True)],
        ),
        f"{BUCKET}/runs/2/eval.json": _report(
            [("Meme aesthetic", 0.85), ("Anime / manga", 0.4)],
            copies=[("Playful", 0.8)],
            gates=[_gate("product_named", False), _gate("cta_clear", False)],
        ),
        f"{BUCKET}/runs/3/eval.json": _report([("Diecut sticker", 0.8)]),
    }
    rows = [
        _row(1, _uri(1), "Visual clarity, Trend connection"),
        _row(2, _uri(2), "Visual clarity"),
        _row(3, _uri(3), "Copy quality"),
    ]
    gcs = FakeGcs({k: json.dumps(v).encode() for k, v in reports.items()})
    return FakeBigQueryClient(rows), gcs


class TestFetch:
    def test_query_is_parameterised(self):
        bq, gcs = _fixture()
        bh.fetch_brand_history(
            "ACME'; DROP TABLE x --", limit=3, bq_client=bq, gcs_client=gcs
        )
        sql, job_config = bq.queries[0]
        assert "ACME" not in sql and "DROP" not in sql
        assert "`p.d.creative_evals`" in sql
        assert "LOWER(TRIM(brand)) = LOWER(@brand)" in sql
        assert "ORDER BY datetime DESC" in sql and "LIMIT @limit" in sql
        params = {p.name: p.value for p in job_config.query_parameters}
        assert params == {"brand": "ACME'; DROP TABLE x --", "limit": 3}

    def test_aggregates_runs(self):
        bq, gcs = _fixture()
        d = bh.fetch_brand_history("ACME", bq_client=bq, gcs_client=gcs)
        assert d["brand"] == "ACME" and d["runs"] == 3
        # Most recent RECENT_STYLE_RUNS runs, in recency order.
        assert d["recent_styles"] == [
            "Watercolor / gouache",
            "Comic panel",
            "Meme aesthetic",
            "Anime / manga",
        ]
        assert d["strongest_styles"][:2] == ["Watercolor / gouache", "Meme aesthetic"]
        assert "Anime / manga" not in d["strongest_styles"]
        assert d["strongest_tones"][:2] == ["Deadpan", "Playful"]
        assert d["weaknesses"][0] == ("Visual clarity", 2)
        # Advisory gates never count as failures.
        assert d["failed_checks"][0] == ("product_named", 2)
        assert all(g != "brand_cue" for g, _ in d["failed_checks"])

    def test_empty_brand_or_zero_limit_skips_query(self):
        bq, gcs = _fixture()
        assert bh.fetch_brand_history("  ", bq_client=bq, gcs_client=gcs) == {}
        assert bh.fetch_brand_history("A", limit=0, bq_client=bq, gcs_client=gcs) == {}
        assert bq.queries == []

    def test_unconfigured_table_skips_query(self, monkeypatch):
        monkeypatch.setattr(bh.config, "BQ_TABLE_EVALS", None)
        bq, gcs = _fixture()
        assert bh.fetch_brand_history("ACME", bq_client=bq, gcs_client=gcs) == {}
        assert bq.queries == []

    def test_no_rows_is_empty(self):
        assert (
            bh.fetch_brand_history(
                "ACME", bq_client=FakeBigQueryClient([]), gcs_client=FakeGcs()
            )
            == {}
        )

    def test_bq_error_fails_open(self, caplog):
        def boom(sql, cfg):
            raise RuntimeError("bq down")

        out = bh.fetch_brand_history(
            "ACME", bq_client=FakeBigQueryClient(boom), gcs_client=FakeGcs()
        )
        assert out == {}
        assert "brand history unavailable" in caplog.text

    def test_gcs_error_keeps_bq_aggregates(self):
        bq, _ = _fixture()
        gcs = FakeGcs(error=RuntimeError("gcs down"))
        d = bh.fetch_brand_history("ACME", bq_client=bq, gcs_client=gcs)
        assert d["runs"] == 3 and d["recent_styles"] == []
        assert d["weaknesses"][0] == ("Visual clarity", 2)

    def test_only_configured_bucket_is_read(self):
        bq = FakeBigQueryClient(
            [
                _row(1, "gs://other-bucket/x.json"),
                _row(2, "https://evil.example/x.json"),
                _row(3, f"gs://{BUCKET}"),
            ]
        )
        gcs = FakeGcs({"other-bucket/x.json": b"{}"})
        d = bh.fetch_brand_history("ACME", bq_client=bq, gcs_client=gcs)
        assert gcs.reads == [] and d["runs"] == 3

    def test_oversized_report_is_skipped(self, monkeypatch):
        monkeypatch.setattr(bh, "REPORT_MAX_BYTES", 10)
        bq, gcs = _fixture()
        d = bh.fetch_brand_history("ACME", bq_client=bq, gcs_client=gcs)
        assert d["recent_styles"] == []

    def test_garbage_report_is_skipped(self):
        bq = FakeBigQueryClient([_row(1, _uri(1))])
        gcs = FakeGcs({f"{BUCKET}/runs/1/eval.json": b"not json"})
        d = bh.fetch_brand_history("ACME", bq_client=bq, gcs_client=gcs)
        assert d["runs"] == 1 and d["recent_styles"] == []

    def test_uses_module_getters_by_default(self, monkeypatch):
        bq, gcs = _fixture()
        monkeypatch.setattr(bh, "_get_bigquery_client", lambda: bq)
        monkeypatch.setattr(bh, "_get_gcs_client", lambda: gcs)
        assert bh.fetch_brand_history("ACME")["runs"] == 3


class TestFormat:
    def test_empty(self):
        assert bh.format_brand_history({}) == ""
        assert bh.format_brand_history({"brand": "A", "runs": 0}) == ""

    def test_full_summary(self):
        bq, gcs = _fixture()
        text = bh.format_brand_history(
            bh.fetch_brand_history("ACME", bq_client=bq, gcs_client=gcs)
        )
        assert text.startswith("Recent runs for ACME (3):")
        assert "styles used recently: Watercolor / gouache" in text
        assert "strongest: " in text and "Deadpan" in text
        assert "recurring weaknesses: Visual clarity (2 of 3 runs)" in text
        assert "checks often failed: product_named (2 of 3 runs)" in text
        assert text.endswith(
            "Build on what worked, fix the weaknesses, and avoid repeating the "
            "recent styles."
        )
        assert len(text.split()) <= 120
        # Spliced into ADK instructions as state: never carries braces.
        assert "{" not in text and "}" not in text

    def test_braces_in_data_are_stripped(self):
        text = bh.format_brand_history(
            {"brand": "{ACME}", "runs": 1, "weaknesses": [("{x}", 1)]}
        )
        assert "{" not in text and "}" not in text

    def test_word_cap(self):
        many = [f"style number {i}" for i in range(40)]
        text = bh.format_brand_history(
            {"brand": "A", "runs": 5, "recent_styles": many, "strongest_styles": many}
        )
        assert len(text.split()) <= 120


def _delta(state: dict, **kw: Any) -> dict:
    import asyncio

    kw.setdefault("enabled", True)
    kw.setdefault("runs", 5)
    return asyncio.run(bh.brand_history_state_delta(state, **kw))


_HISTORY = {
    "brand": "ACME",
    "runs": 2,
    "recent_styles": ["Photoreal / editorial", "Comic panel"],
    "strongest_styles": ["Comic panel"],
    "strongest_tones": [],
    "weaknesses": [("Visual clarity", 2)],
    "failed_checks": [],
}


class TestStateDelta:
    def test_disabled_makes_no_query(self, monkeypatch):
        calls = []
        monkeypatch.setattr(bh, "fetch_brand_history", lambda *a, **k: calls.append(1))
        assert _delta({"brand": "ACME"}, enabled=False) == {}
        assert _delta({"brand": "ACME"}, runs=0) == {}
        assert calls == []

    def test_writes_note_and_repicks_shortlist_without_recent_styles(self, monkeypatch):
        seen = {}

        def fake(brand, *, limit):
            seen.update(brand=brand, limit=limit)
            return _HISTORY

        monkeypatch.setattr(bh, "fetch_brand_history", fake)
        delta = _delta({"brand": "ACME", "visual_style_preference": ""}, runs=3)
        assert seen == {"brand": "ACME", "limit": 3}
        assert delta["brand_history"].startswith("Recent runs for ACME (2):")
        shortlist = delta["style_shortlist"].split("; ")
        assert len(shortlist) == 6
        assert not set(shortlist) & set(_HISTORY["recent_styles"])

    def test_user_style_preference_keeps_the_shortlist(self, monkeypatch):
        monkeypatch.setattr(bh, "fetch_brand_history", lambda *a, **k: _HISTORY)
        delta = _delta({"brand": "ACME", "visual_style_preference": "Comic panel"})
        assert "style_shortlist" not in delta and delta["brand_history"]

    def test_no_history_writes_empty_note_only(self, monkeypatch):
        monkeypatch.setattr(bh, "fetch_brand_history", lambda *a, **k: {})
        assert _delta({"brand": "ACME"}) == {"brand_history": ""}

    def test_error_fails_open(self, monkeypatch, caplog):
        def boom(*a, **k):
            raise RuntimeError("kaput")

        monkeypatch.setattr(bh, "fetch_brand_history", boom)
        assert _delta({"brand": "ACME"}) == {"brand_history": ""}
        assert "brand history skipped" in caplog.text

    def test_timeout_fails_open(self, monkeypatch, caplog):
        import time

        def slow(*a, **k):
            time.sleep(0.5)
            return _HISTORY

        monkeypatch.setattr(bh, "fetch_brand_history", slow)

        async def timed() -> tuple[dict, float]:
            started = time.monotonic()
            delta = await bh.brand_history_state_delta(
                {"brand": "ACME"}, enabled=True, runs=5, timeout=0.05
            )
            return delta, time.monotonic() - started

        import asyncio

        # Measured inside the loop: asyncio.run itself waits for the worker
        # thread at shutdown, a long-running server loop does not.
        delta, elapsed = asyncio.run(timed())
        assert delta == {"brand_history": ""}
        assert elapsed < 0.4
        assert "brand history skipped" in caplog.text
