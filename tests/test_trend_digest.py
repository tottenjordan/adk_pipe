"""trend_scout's trend digest (trend_scout/digest.py + digest_store.py): grounded one-line
summaries of the day's trends + the sensitivity flags, cached per trend list in
BigQuery (`trend_digest`), exposed to the root as the `trend_digest` NodeTool.

Pure helpers are tested directly; the routes are driven through the REAL
resumable trend_scout App with stub models (like tests/test_trend_scout_graph.py)
and a fake BigQuery client.
"""

import asyncio
import json
from typing import Any

import pytest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from tests._fake_bq import FakeBigQueryClient
from tests._fakes import (
    RecordingLlm,
    fc_response,
    text_response,
    user_message,
    walk_nodes,
)

TERMS = ["Texas Football", "Storm Ada", "golden dip"]
REFRESH = "2026-10-13"
SUMMARIES = {
    "items": [
        {
            "term": "texas football",
            "summary": "Texas plays Oklahoma in the Red River Rivalry on Saturday.",
        },
        {"term": "Storm Ada", "summary": "A storm caused deaths on the coast."},
        {"term": "Golden Dip", "summary": "A snack recipe went viral on TikTok."},
    ]
}
FLAGS = {
    "flags": [
        {"term": "Texas Football", "sensitive": False, "reason": ""},
        {"term": "Storm Ada", "sensitive": True, "reason": "deadly storm"},
        {"term": "golden dip", "sensitive": False, "reason": ""},
    ]
}


# --- pure helpers -------------------------------------------------------------


def test_terms_hash_ignores_order_and_case_and_changes_with_terms():
    from trend_scout.digest_store import terms_hash

    base = terms_hash(REFRESH, TERMS)
    assert len(base) == 16
    assert terms_hash(REFRESH, list(reversed(TERMS))) == base
    assert terms_hash(REFRESH, [" texas  FOOTBALL", "storm ada", "Golden Dip"]) == base
    assert terms_hash(REFRESH, [*TERMS[:2], "golden dips"]) != base
    assert terms_hash(REFRESH, TERMS[:2]) != base
    assert terms_hash("2026-10-12", TERMS) != base


def test_normalize_draft_keeps_raw_terms_verbatim_in_raw_order():
    from trend_scout.digest_store import normalize_draft

    draft = {
        "items": [
            {"term": "golden dip", "summary": "  A snack\n recipe  went viral. "},
            {"term": "Unknown Term", "summary": "Not a raw term."},
            {"term": "TEXAS football", "summary": "Texas plays Oklahoma."},
            {"term": "Storm Ada", "summary": ""},  # blank → dropped
        ]
    }
    out = normalize_draft(draft, TERMS, REFRESH)
    assert out == {
        "refresh_date": REFRESH,
        "source": "fresh",
        "items": [
            {"term": "Texas Football", "summary": "Texas plays Oklahoma."},
            {"term": "golden dip", "summary": "A snack recipe went viral."},
        ],
    }


def test_normalize_draft_caps_length_and_reads_json_strings():
    from trend_scout.digest_store import MAX_SUMMARY_CHARS, normalize_draft

    long = "word " * 200
    raw = "```json\n" + json.dumps({"items": [{"term": "Storm Ada", "summary": long}]})
    out = normalize_draft(raw + "\n```", TERMS, REFRESH)
    (item,) = out["items"]
    assert item["term"] == "Storm Ada"
    assert len(item["summary"]) <= MAX_SUMMARY_CHARS


@pytest.mark.parametrize(
    "draft", [None, "", "not json", {}, {"items": "x"}, {"items": [None, 3]}, [1]]
)
def test_normalize_draft_unavailable_on_empty_or_malformed(draft):
    from trend_scout.digest_store import normalize_draft

    assert normalize_draft(draft, TERMS, REFRESH) == {
        "refresh_date": REFRESH,
        "source": "unavailable",
        "items": [],
    }


def test_sql_builders_bind_every_value():
    from trend_scout.digest_store import build_insert_sql, build_lookup_sql

    sql, params = build_lookup_sql("p.d.trend_digest", REFRESH, "h" * 16)
    assert REFRESH not in sql and "h" * 16 not in sql
    assert "refresh_date = @refresh_date" in sql
    assert "terms_hash = @terms_hash" in sql
    assert "ORDER BY created_at DESC" in sql and "LIMIT 1" in sql
    assert {p.name: p.value for p in params} == {
        "refresh_date": REFRESH,
        "terms_hash": "h" * 16,
    }

    digest = json.dumps({"items": [{"term": "it's", "summary": "a'b"}], "flags": []})
    sql, params = build_insert_sql(
        "p.d.trend_digest",
        digest_id="id1",
        refresh_date=REFRESH,
        terms_hash="h" * 16,
        digest=digest,
        models="m",
    )
    for value in ("id1", REFRESH, "h" * 16, digest, "it's"):
        assert value not in sql
    assert "WHEN NOT MATCHED THEN" in sql and "WHEN MATCHED" not in sql
    assert {p.name: p.value for p in params} == {
        "digest_id": "id1",
        "refresh_date": REFRESH,
        "terms_hash": "h" * 16,
        "digest": digest,
        "models": "m",
    }


def test_insert_columns_match_the_table_schema():
    import re
    from pathlib import Path

    from trend_scout.digest_store import build_insert_sql

    schema = Path(__file__).parent.parent / "deployment" / "bq_schemas"
    fields = json.loads((schema / "trend_digest.json").read_text())
    names = [f["name"] for f in fields]
    assert names == [
        "digest_id",
        "refresh_date",
        "terms_hash",
        "digest",
        "models",
        "created_at",
    ]
    sql, _ = build_insert_sql(
        "t", digest_id="i", refresh_date=REFRESH, terms_hash="h", digest="{}", models=""
    )
    cols = re.search(r"INSERT \((.*?)\)", sql, re.S)
    assert cols and [c.strip() for c in cols.group(1).split(",")] == names


# --- graph runs ------------------------------------------------------------------


@pytest.fixture
def digest_env(monkeypatch):
    """A configured cache table + a fake BigQuery client per test."""
    import trend_scout.digest_store as store
    from trend_scout.config import config

    monkeypatch.setattr(config, "trend_digest_enabled", True)
    monkeypatch.setattr(config, "BQ_PROJECT_ID", "p")
    monkeypatch.setattr(config, "BQ_DATASET_ID", "d")
    monkeypatch.setattr(config, "BQ_TABLE_TREND_DIGEST", "trend_digest")

    def use(client: Any) -> None:
        monkeypatch.setattr(store, "_get_bigquery_client", lambda: client)

    return use


def _cached_row():
    digest = {"items": SUMMARIES["items"], "flags": FLAGS["flags"]}
    return {"digest": json.dumps(digest)}


class _Run:
    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        import trend_scout.agent as ts

        nodes = {n.name: n for n in walk_nodes(ts.trend_digest)}
        self.ts = ts
        self.root = RecordingLlm()
        self.searcher = RecordingLlm()
        self.synth = RecordingLlm()
        self.classify = RecordingLlm()
        pairs = (
            (ts.root_agent, self.root),
            (nodes["trend_describe_searcher"], self.searcher),
            (nodes["trend_describe_synthesizer"], self.synth),
            (nodes["classify_trends_agent"], self.classify),
        )
        for agent, llm in pairs:
            monkeypatch.setattr(agent, "model", llm)
            monkeypatch.setattr(agent, "before_model_callback", None)
            if getattr(agent, "planner", None) is not None:
                monkeypatch.setattr(agent, "planner", None)
        monkeypatch.setattr(nodes["trend_describe_searcher"], "tools", [])
        monkeypatch.setattr(ts.root_agent, "instruction", "orchestrate")
        monkeypatch.setattr(ts.root_agent, "before_agent_callback", None)
        self.root.push(
            fc_response("trend_digest", {"request": "go"}, "fc1"),
            text_response("ROOT DONE"),
        )

    def go(self, state: dict[str, Any]) -> tuple[dict[str, Any], list[Any]]:
        async def run() -> tuple[dict[str, Any], list[Any]]:
            svc = InMemorySessionService()
            runner = Runner(app=self.ts.app, session_service=svc)
            session = await svc.create_session(
                app_name="trend_scout", user_id="u", state=state
            )
            events = [
                e
                async for e in runner.run_async(
                    user_id="u", session_id=session.id, new_message=user_message("hi")
                )
            ]
            final = await svc.get_session(
                app_name="trend_scout", user_id="u", session_id=session.id
            )
            assert final is not None
            responses = [
                fr.response for e in events for fr in e.get_function_responses()
            ]
            return dict(final.state), responses

        return asyncio.run(run())


_SEED = {"raw_gtrends": TERMS, "gtrends_refresh_date": REFRESH}


def test_cache_hit_restores_state_and_skips_the_models(monkeypatch, digest_env):
    from trend_scout.digest_store import terms_hash

    bq = FakeBigQueryClient([_cached_row()])
    digest_env(bq)
    run = _Run(monkeypatch)
    state, responses = run.go(dict(_SEED))

    (sql, job_config) = bq.queries[0]
    assert len(bq.queries) == 1  # the lookup; nothing written
    # (QueryJobConfig round-trips a DATE parameter's value to a datetime.date)
    params = {p.name: str(p.value) for p in job_config.query_parameters}
    assert params == {"refresh_date": REFRESH, "terms_hash": terms_hash(REFRESH, TERMS)}
    assert "terms_hash = @terms_hash" in sql
    assert (run.searcher.calls, run.synth.calls, run.classify.calls) == (0, 0, 0)
    assert state["trend_flags"] == FLAGS
    assert state["trend_summaries"]["source"] == "cache"
    assert state["trend_summaries"]["refresh_date"] == REFRESH
    # verbatim raw terms, raw order
    assert [i["term"] for i in state["trend_summaries"]["items"]] == TERMS
    (response,) = responses
    assert response and "cache" in str(response)
    assert run.root.calls == 2  # the root got its function response


def _script_miss(run: _Run, *, synth_turns: list[str], flags=FLAGS) -> None:
    for i, turn in enumerate(synth_turns):
        run.searcher.push(text_response(f"RAW FACTS {i}"))
        run.synth.push(text_response(turn))
    run.classify.push(text_response(json.dumps(flags)))


def test_cache_miss_describes_classifies_and_stores_once(monkeypatch, digest_env):
    from agent_common import stable_row_id
    from trend_scout.digest_store import terms_hash

    bq = FakeBigQueryClient([])  # lookup finds nothing
    digest_env(bq)
    run = _Run(monkeypatch)
    _script_miss(run, synth_turns=[json.dumps(SUMMARIES)])
    state, responses = run.go(dict(_SEED))

    assert (run.searcher.calls, run.synth.calls, run.classify.calls) == (1, 1, 1)
    # The classifier judges sensitivity on the live summaries.
    classify_instr = str(run.classify.requests[0].config.system_instruction)
    assert "Red River Rivalry" in classify_instr
    assert state["trend_summaries"]["source"] == "fresh"
    assert [i["term"] for i in state["trend_summaries"]["items"]] == TERMS
    assert state["trend_flags"] == FLAGS
    assert "trend_summaries_draft__retry_exhausted" not in state

    lookup, merge = bq.queries  # one lookup, one MERGE
    assert lookup[0].lstrip().startswith("SELECT")
    assert "MERGE" in merge[0]
    params = {p.name: p.value for p in merge[1].query_parameters}
    h = terms_hash(REFRESH, TERMS)
    assert params["terms_hash"] == h
    assert params["digest_id"] == stable_row_id(REFRESH, h)
    stored = json.loads(params["digest"])
    assert stored["flags"] == FLAGS["flags"]
    assert stored["items"] == state["trend_summaries"]["items"]
    (response,) = responses
    assert response


def test_failed_describe_still_classifies_and_writes_nothing(monkeypatch, digest_env):
    bq = FakeBigQueryClient([])
    digest_env(bq)
    run = _Run(monkeypatch)
    _script_miss(run, synth_turns=["  ", "  "])  # both attempts empty
    state, responses = run.go(dict(_SEED))

    assert (run.searcher.calls, run.synth.calls, run.classify.calls) == (2, 2, 1)
    assert state["trend_summaries"] == {
        "refresh_date": REFRESH,
        "source": "unavailable",
        "items": [],
    }
    assert state["trend_flags"] == FLAGS
    assert len(bq.queries) == 1  # the lookup only, never a MERGE
    # A failed optional describe is not a research gap (marker cleared).
    assert not state.get("trend_summaries_draft__retry_exhausted")
    assert responses and responses[0]


def test_low_coverage_writes_nothing(monkeypatch, digest_env):
    bq = FakeBigQueryClient([])
    digest_env(bq)
    run = _Run(monkeypatch)
    partial = {"items": SUMMARIES["items"][:2]}  # 2 of 3 terms < 80%
    _script_miss(run, synth_turns=[json.dumps(partial)])
    state, _ = run.go(dict(_SEED))

    assert state["trend_summaries"]["source"] == "fresh"
    assert len(state["trend_summaries"]["items"]) == 2
    assert len(bq.queries) == 1


def test_classifier_without_flags_writes_nothing(monkeypatch, digest_env):
    bq = FakeBigQueryClient([])
    digest_env(bq)
    run = _Run(monkeypatch)
    _script_miss(run, synth_turns=[json.dumps(SUMMARIES)], flags={"flags": []})
    run.go(dict(_SEED))
    assert len(bq.queries) == 1


def test_different_terms_hash_misses(monkeypatch, digest_env):
    """The cache is keyed by the term list: rows for another list never match."""
    from trend_scout.digest_store import terms_hash

    other = terms_hash(REFRESH, ["something else"])

    def rows(sql, job_config):
        params = {p.name: p.value for p in job_config.query_parameters}
        if "MERGE" in sql or params.get("terms_hash") != other:
            return []
        return [_cached_row()]

    bq = FakeBigQueryClient(rows)
    digest_env(bq)
    run = _Run(monkeypatch)
    _script_miss(run, synth_turns=[json.dumps(SUMMARIES)])
    state, _ = run.go(dict(_SEED))
    assert state["trend_summaries"]["source"] == "fresh"
    assert run.classify.calls == 1


def test_bigquery_error_routes_miss(monkeypatch, digest_env):
    def boom(sql, job_config):
        if sql.lstrip().startswith("SELECT"):
            raise RuntimeError("Not found: Table p:d.trend_digest")
        raise RuntimeError("write failed")  # a failed write only logs

    bq = FakeBigQueryClient(boom)
    digest_env(bq)
    run = _Run(monkeypatch)
    _script_miss(run, synth_turns=[json.dumps(SUMMARIES)])
    state, responses = run.go(dict(_SEED))
    assert state["trend_summaries"]["source"] == "fresh"
    assert state["trend_flags"] == FLAGS
    assert len(bq.queries) == 2  # lookup + the (failed) write attempt
    assert responses and responses[0]


def test_lookup_timeout_is_a_miss(monkeypatch, digest_env):
    import time

    import trend_scout.digest_store as store

    monkeypatch.setattr(store, "LOOKUP_TIMEOUT_SECONDS", 0.05)

    def slow(sql, job_config):
        time.sleep(0.5)
        return [_cached_row()]

    digest_env(FakeBigQueryClient(slow))
    assert asyncio.run(store.lookup_cached_digest(dict(_SEED))) is None


def test_lookup_without_refresh_date_or_terms_skips_the_query(digest_env):
    import trend_scout.digest_store as store

    bq = FakeBigQueryClient([_cached_row()])
    digest_env(bq)
    for state in ({"raw_gtrends": TERMS}, {"gtrends_refresh_date": REFRESH}, {}):
        assert asyncio.run(store.lookup_cached_digest(state)) is None
    assert bq.queries == []


def test_malformed_cached_row_is_a_miss(digest_env):
    import trend_scout.digest_store as store

    for row in ({"digest": "not json"}, {"digest": json.dumps({"items": []})}):
        digest_env(FakeBigQueryClient([row]))
        assert asyncio.run(store.lookup_cached_digest(dict(_SEED))) is None


def test_disabled_runs_only_the_classifier(monkeypatch, digest_env):
    """TREND_DIGEST_ENABLED=false: today's behaviour — no describe step, no cache
    I/O, `trend_flags` from the classifier, and no `trend_summaries` key."""
    from trend_scout.config import config

    monkeypatch.setattr(config, "trend_digest_enabled", False)
    bq = FakeBigQueryClient([_cached_row()])
    digest_env(bq)
    run = _Run(monkeypatch)
    run.classify.push(text_response(json.dumps(FLAGS)))
    state, responses = run.go(dict(_SEED))

    assert bq.queries == []
    assert (run.searcher.calls, run.synth.calls, run.classify.calls) == (0, 0, 1)
    assert state["trend_flags"] == FLAGS
    assert "trend_summaries" not in state
    # the summaries line renders empty
    instr = str(run.classify.requests[0].config.system_instruction)
    assert "None" not in instr.split("<trend_summaries>")[1].split("</")[0]
    assert responses and responses[0]


# --- TS-1 / TS-3: the shared cache never holds a term without a verdict -------


def test_should_cache_requires_a_verdict_for_every_term():
    from trend_scout.digest_store import normalize_draft, normalize_flags, should_cache

    summaries = normalize_draft(SUMMARIES, TERMS, REFRESH)
    full = normalize_flags(FLAGS, TERMS)
    partial = normalize_flags({"flags": FLAGS["flags"][:2]}, TERMS)
    assert should_cache(summaries, full, TERMS)
    assert not should_cache(summaries, partial, TERMS)
    # Summaries keep the MIN_CACHE_COVERAGE bar (optional enrichment).
    many = [f"term {i}" for i in range(10)]
    nine = normalize_draft(
        {"items": [{"term": t, "summary": "s"} for t in many[:9]]}, many, REFRESH
    )
    all_flags = normalize_flags(
        {"flags": [{"term": t, "sensitive": False} for t in many]}, many
    )
    assert should_cache(nine, all_flags, many)
    # ...but flags must cover every term, even above MIN_CACHE_COVERAGE (9/10).
    nine_flags = normalize_flags({"flags": all_flags["flags"][:9]}, many)
    assert not should_cache(nine, nine_flags, many)


def test_partial_flag_coverage_writes_nothing(monkeypatch, digest_env, caplog):
    """M7: full summaries, flags for 2 of 3 terms: never cached (the missing
    verdict would be read as 'not sensitive' by every later cache hit)."""
    bq = FakeBigQueryClient([])
    digest_env(bq)
    run = _Run(monkeypatch)
    partial = {"flags": FLAGS["flags"][:2]}
    _script_miss(run, synth_turns=[json.dumps(SUMMARIES)], flags=partial)
    with caplog.at_level("WARNING", logger="trend_scout.digest"):
        state, _ = run.go(dict(_SEED))

    assert state["trend_summaries"]["source"] == "fresh"
    assert len(state["trend_summaries"]["items"]) == 3
    assert len(bq.queries) == 1  # the lookup only, never a MERGE
    assert any("1 of 3" in r.getMessage() for r in caplog.records)


def test_cached_row_without_full_flags_is_a_miss(digest_env):
    """M8: a row with summaries but no (or partial) flags is never a hit."""
    import trend_scout.digest_store as store

    rows = (
        {"items": SUMMARIES["items"]},
        {"items": SUMMARIES["items"], "flags": []},
        {"items": SUMMARIES["items"], "flags": FLAGS["flags"][:2]},
    )
    for row in rows:
        digest_env(FakeBigQueryClient([{"digest": json.dumps(row)}]))
        assert asyncio.run(store.lookup_cached_digest(dict(_SEED))) is None


def test_stale_draft_is_cleared_so_the_describe_step_runs(monkeypatch, digest_env):
    """M9: a draft left in state by an earlier run must not satisfy the
    describe step's output key on a miss."""
    bq = FakeBigQueryClient([])
    digest_env(bq)
    run = _Run(monkeypatch)
    _script_miss(run, synth_turns=[json.dumps(SUMMARIES)])
    stale = {"items": [{"term": "Storm Ada", "summary": "STALE yesterday"}]}
    state, _ = run.go({**_SEED, "trend_summaries_draft": stale})

    assert (run.searcher.calls, run.synth.calls) == (1, 1)
    items = state["trend_summaries"]["items"]
    assert "STALE" not in json.dumps(items)
    assert len(items) == 3


def test_stale_draft_never_passes_for_a_failed_describe(monkeypatch, digest_env):
    """M9: RetryUntilKeyNode counts an already-populated key as success, so an
    uncleared stale draft would turn a failed describe into 'fresh' summaries
    (and be cached). Cleared on the miss route, both attempts run and fail."""
    bq = FakeBigQueryClient([])
    digest_env(bq)
    run = _Run(monkeypatch)
    _script_miss(run, synth_turns=["  ", "  "])
    stale = {"items": [{"term": t, "summary": "STALE"} for t in TERMS]}
    state, _ = run.go({**_SEED, "trend_summaries_draft": stale})

    assert (run.searcher.calls, run.synth.calls) == (2, 2)
    assert state["trend_summaries"]["source"] == "unavailable"
    assert len(bq.queries) == 1  # never cached
