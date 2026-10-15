"""Offline tests for the Gemini 3 temperature A/B harness (experiments/temperature_ab).

Everything runs against fakes: no network, no gcloud, no GCP credentials.
"""

from __future__ import annotations

import io
import json
import random
import statistics
import sys
import types
import urllib.request
from pathlib import Path

import pytest

from experiments.creative_latency import run_trial
from experiments.temperature_ab import analyze, fixtures, metrics, run_ab

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Step 1: P3 auth on the run_trial request helpers
# ---------------------------------------------------------------------------


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_transport(monkeypatch):
    """Capture every urllib Request the helpers send; reply with a canned JSON."""
    sent: list[urllib.request.Request] = []
    replies: list[dict] = []

    def fake_urlopen(req, timeout=None):
        sent.append(req)
        body = replies.pop(0) if replies else {"id": "sid-1"}
        return _FakeResponse(json.dumps(body).encode())

    monkeypatch.setattr(run_trial, "_urlopen", fake_urlopen)
    return sent, replies


def _body(req: urllib.request.Request) -> dict:
    data = req.data
    assert isinstance(data, bytes)
    return json.loads(data.decode())


class TestRunTrialAuth:
    def test_create_session_default_is_backward_compatible(self, fake_transport):
        sent, _ = fake_transport
        sid = run_trial.create_session("https://b", "u1", "tok")
        assert sid == "sid-1"
        (req,) = sent
        assert req.full_url == "https://b/apps/creative_agent/users/u1/sessions"
        assert req.get_method() == "POST"
        assert _body(req) == {"state": {}}
        assert req.get_header("Authorization") == "Bearer tok"
        assert req.get_header("X-tt-user") is None

    def test_create_session_with_state_app_and_user_header(self, fake_transport):
        sent, _ = fake_transport
        run_trial.create_session(
            "https://b",
            "a@x.com",
            "tok",
            state={"brand": "PRS"},
            app="trend_scout",
            tt_user="a@x.com",
        )
        (req,) = sent
        # "@" is a valid pchar: left raw so the path matches X-TT-User exactly.
        assert req.full_url == "https://b/apps/trend_scout/users/a@x.com/sessions"
        assert _body(req) == {"state": {"brand": "PRS"}}
        assert req.get_header("X-tt-user") == "a@x.com"

    def test_start_run_default_app_and_message(self, fake_transport):
        sent, _ = fake_transport
        run_trial.start_run("https://b", "u1", "s1", "tok")
        (req,) = sent
        assert req.full_url == "https://b/runs/creative_agent"
        assert _body(req) == {
            "userId": "u1",
            "sessionId": "s1",
            "message": run_trial.CAMPAIGN_MESSAGE,
        }
        assert req.get_header("X-tt-user") is None

    def test_start_run_with_app_message_and_user(self, fake_transport):
        sent, _ = fake_transport
        run_trial.start_run(
            "https://b",
            "a@x.com",
            "s1",
            "tok",
            app="trend_scout",
            message="hi",
            tt_user="a@x.com",
        )
        (req,) = sent
        assert req.full_url == "https://b/runs/trend_scout"
        assert _body(req)["message"] == "hi"
        assert _body(req)["userId"] == "a@x.com"
        assert req.get_header("X-tt-user") == "a@x.com"
        assert req.get_header("Content-type") == "application/json"

    def test_poll_sends_app_and_user_header(self, fake_transport, monkeypatch):
        sent, replies = fake_transport
        replies.append(
            {"status": "done", "events": [{"id": 1}], "nextCursor": 1, "state": {}}
        )
        monkeypatch.setattr(run_trial.time, "sleep", lambda _s: None)
        events, _, status, _ = run_trial.poll_to_terminal(
            "https://b", "a@x.com", "s1", "tok", app="trend_scout", tt_user="a@x.com"
        )
        assert status == "done" and events == [{"id": 1}]
        (req,) = sent
        assert req.full_url == "https://b/runs/trend_scout/a@x.com/s1?since=0"
        assert req.get_method() == "GET"
        assert req.get_header("X-tt-user") == "a@x.com"

    def test_poll_timeout_is_overridable(self, monkeypatch):
        monkeypatch.setattr(
            run_trial, "_get", lambda *a, **k: {"status": "running", "events": []}
        )
        monkeypatch.setattr(run_trial.time, "sleep", lambda _s: None)
        with pytest.raises(TimeoutError):
            run_trial.poll_to_terminal("https://b", "u", "s", "t", timeout_s=0.0)


class TestMintProxyIdToken:
    def test_impersonates_with_include_email(self, monkeypatch):
        seen: dict = {}

        class FakeImpersonated:
            def __init__(self, **kwargs):
                seen["impersonated"] = kwargs

        class FakeIdToken:
            def __init__(self, target_credentials, target_audience=None, **kwargs):
                seen["id_token"] = {
                    "target_credentials": target_credentials,
                    "target_audience": target_audience,
                    **kwargs,
                }
                self.token = None

            def refresh(self, request):
                seen["refreshed_with"] = request
                self.token = "minted"

        fake_mod = types.SimpleNamespace(
            Credentials=FakeImpersonated, IDTokenCredentials=FakeIdToken
        )
        monkeypatch.setattr(run_trial, "_impersonation_module", lambda: fake_mod)
        monkeypatch.setattr(
            run_trial, "_default_credentials", lambda: ("source-creds", "proj")
        )
        monkeypatch.setattr(run_trial, "_auth_request", lambda: "req")

        token = run_trial.mint_proxy_id_token(
            "https://api.run.app", "tt-web-sa@p.iam.gserviceaccount.com"
        )
        assert token == "minted"
        assert seen["impersonated"]["source_credentials"] == "source-creds"
        assert (
            seen["impersonated"]["target_principal"]
            == "tt-web-sa@p.iam.gserviceaccount.com"
        )
        assert seen["id_token"]["include_email"] is True
        assert seen["id_token"]["target_audience"] == "https://api.run.app"
        assert isinstance(seen["id_token"]["target_credentials"], FakeImpersonated)
        assert seen["refreshed_with"] == "req"

    def test_requires_a_service_account(self):
        with pytest.raises(ValueError):
            run_trial.mint_proxy_id_token("https://api.run.app", "")

    def test_real_google_auth_api_has_include_email(self):
        """Pin the installed google-auth API the minting relies on."""
        import inspect

        from google.auth import impersonated_credentials

        params = inspect.signature(
            impersonated_credentials.IDTokenCredentials.__init__
        ).parameters
        assert "include_email" in params and "target_audience" in params


# ---------------------------------------------------------------------------
# Step 2: fixtures
# ---------------------------------------------------------------------------


class TestFixtures:
    def test_two_campaigns_matching_the_evalset(self):
        evalset = json.loads(
            (ROOT / "tests/eval/evalsets/creative_agent_evalset.json").read_text()
        )
        texts = [
            c["conversation"][0]["user_content"]["parts"][0]["text"]
            for c in evalset["eval_cases"]
        ]
        assert [c.key for c in fixtures.CAMPAIGNS] == ["prs", "anr"]
        for campaign, text in zip(fixtures.CAMPAIGNS, texts, strict=True):
            assert f'Brand Name: "{campaign.brand}"' in text
            assert f'Target Product: "{campaign.target_product}"' in text
            assert f'Target Audience: "{campaign.target_audience}"' in text
            assert f'Key Selling Points: "{campaign.key_selling_points}"' in text
            assert f'Target Search Trend: "{campaign.trend}"' in text

    def test_creative_initial_state_has_frontend_keys(self):
        state = fixtures.initial_state("creative_agent", fixtures.CAMPAIGNS[0])
        assert state["ui_app"] == "creative_agent"
        for key in (
            "brand",
            "target_audience",
            "target_product",
            "key_selling_points",
            "target_search_trends",
        ):
            assert isinstance(state[key], str) and state[key]
        assert state["target_search_trends"] == fixtures.CAMPAIGNS[0].trend

    def test_trend_scout_state_is_auto_pick(self):
        state = fixtures.initial_state("trend_scout", fixtures.CAMPAIGNS[1])
        assert state == {"ui_app": "trend_scout"}

    def test_kickoff_messages(self):
        c = fixtures.CAMPAIGNS[0]
        creative = fixtures.kickoff_message("creative_agent", c)
        scout = fixtures.kickoff_message("trend_scout", c)
        assert creative.startswith(f'Brand Name: "{c.brand}"')
        assert f'target_search_trends: "{c.trend}"' in creative
        assert "target_search_trends" not in scout
        assert f'Key Selling Points: "{c.key_selling_points}"' in scout

    def test_unknown_app_rejected(self):
        with pytest.raises(ValueError):
            fixtures.initial_state("interactive_creative", fixtures.CAMPAIGNS[0])

    def test_experiment_user(self):
        assert (
            fixtures.EXPERIMENT_USER_ID == "temperature-ab@jordantotten.altostrat.com"
        )


# ---------------------------------------------------------------------------
# Step 3: metrics
# ---------------------------------------------------------------------------


def creative_state(**overrides) -> dict:
    state = {
        "finalize_done": True,
        "creative_evaluation_report": {
            "summary": {"overall_pass_rate": 0.75, "gates_pass_rate": 0.5},
            "ad_copy_evaluations": [
                {"score": {"overall_score": 0.8, "passed": True}},
                {"score": {"overall_score": 0.6, "passed": False}},
            ],
            "visual_concept_evaluations": [],
        },
        "ad_copy_critique": {
            "ad_copies": [
                {"headline": "Shred the Summer"},
                {"headline": "shred the night"},
                {"headline": "Quiet Tone"},
            ]
        },
        "generated_images": {
            "a": {"attempts": 1, "gcs_uri": "gs://x/a.png"},
            "b": {"attempts": 2},
            "c": {"gcs_uri": "gs://x/c.png"},
        },
        "creative_brief__issues": ["one", "two"],
        "image_qa__issues": "a string issue",
        "ad_copy_critique__issues": None,
        "final_visual_concepts__issues": [],
        "creative_brief__retry_exhausted": True,
        "trend_situation_draft__retry_exhausted": None,
    }
    state.update(overrides)
    return state


class TestCreativeMetrics:
    def test_full_state(self):
        m = metrics.creative_metrics(creative_state())
        assert m["finalized"] is True
        assert m["gates_pass_rate"] == 0.5
        assert m["pass_rate"] == 0.75
        assert m["mean_score"] == pytest.approx(0.7)
        assert m["n_issues"] == 3
        assert m["n_retry_exhausted"] == 1
        assert m["image_qa_attempts"] == 3
        # pairs: {shred,the,summer}/{shred,the,night}=2/4; vs {quiet,tone}=0; 0
        assert m["headline_jaccard"] == pytest.approx(0.5 / 3)

    def test_empty_state(self):
        m = metrics.creative_metrics({})
        assert m["finalized"] is False
        assert m["gates_pass_rate"] is None
        assert m["mean_score"] is None
        assert m["pass_rate"] is None
        assert m["n_issues"] == 0
        assert m["n_retry_exhausted"] == 0
        assert m["image_qa_attempts"] == 0
        assert m["headline_jaccard"] is None

    def test_headline_jaccard_identical_and_disjoint(self):
        assert metrics.headline_jaccard(["a b", "A B"]) == 1.0
        assert metrics.headline_jaccard(["a b", "c d"]) == 0.0
        assert metrics.headline_jaccard(["only one"]) is None
        assert metrics.headline_jaccard(["Go, now!", "go now"]) == 1.0

    def test_headlines_tolerate_bad_shapes(self):
        assert metrics.final_headlines({"ad_copy_critique": "garbage"}) == []
        assert metrics.final_headlines({"ad_copy_critique": {"ad_copies": None}}) == []
        assert metrics.final_headlines(
            {"ad_copy_critique": {"ad_copies": [{"headline": "x"}, {"nope": 1}, "s"]}}
        ) == ["x"]

    def test_count_issues_values(self):
        assert metrics.count_issues({"a__issues": "x", "b__issues": ""}) == 1
        assert metrics.count_issues({"a__issues": ["x"], "notissues": ["y", "z"]}) == 1

    def test_finalized_must_be_truthy(self):
        assert metrics.creative_metrics({"finalize_done": False})["finalized"] is False


SELECTED_MD = """## Strategy
### Summer Jam
**The Hook:** x
### guitar solos
**Context:** y
### Phish tour
"""


class TestTrendScoutMetrics:
    def test_completed_with_three_trends(self):
        state = {
            "selected_gtrends": SELECTED_MD,
            "trend_flags": {
                "flags": [
                    {"term": "Summer Jam", "sensitive": False},
                    {"term": "storm", "sensitive": True, "reason": "disaster"},
                    {"nope": 1},
                ]
            },
            "info_gtrends__retry_exhausted": None,
        }
        m = metrics.trend_scout_metrics(state)
        assert m["completed"] is True
        assert m["n_selected"] == 3
        assert m["trend_flags"] == {"Summer Jam": False, "storm": True}
        assert m["info_gtrends_exhausted"] is False
        assert m["n_retry_exhausted"] == 0

    def test_incomplete(self):
        m = metrics.trend_scout_metrics(
            {
                "selected_gtrends": "### only one\n",
                "info_gtrends__retry_exhausted": True,
            }
        )
        assert m["completed"] is False
        assert m["n_selected"] == 1
        assert m["info_gtrends_exhausted"] is True
        assert m["n_retry_exhausted"] == 1
        assert m["trend_flags"] == {}

    def test_missing(self):
        m = metrics.trend_scout_metrics({})
        assert m["completed"] is False and m["n_selected"] == 0


class TestLogScrape:
    def test_filter_scopes_revision_window_and_signals(self):
        f = metrics.build_signal_filter(
            revision="rev-1", start_epoch=1_784_000_000.0, end_epoch=1_784_000_600.0
        )
        assert 'resource.labels.revision_name="rev-1"' in f
        assert "timestamp>=" in f and "timestamp<=" in f
        for needle in (
            "empty/abnormal model turn",
            "returned an empty turn",
            "ValidationError",
            "429",
        ):
            assert needle in f

    def test_no_revision_no_filter(self):
        assert (
            metrics.build_signal_filter(revision="", start_epoch=0, end_epoch=1) == ""
        )
        assert metrics.scrape_log_counts("", 0, 1, reader=lambda f: []) is None

    def test_classify_entries(self):
        entries = [
            {"textPayload": "WARNING empty/abnormal model turn in x"},
            {"textPayload": "Model m returned an empty turn (finish_reason=STOP"},
            {"jsonPayload": {"message": "pydantic ValidationError: 1 error"}},
            {"textPayload": "429 RESOURCE_EXHAUSTED"},
            {"httpRequest": {"status": 429}},
            {"textPayload": "RESOURCE_EXHAUSTED quota"},
            {"textPayload": "took 14290 ms"},  # not a 429
            {"textPayload": "unrelated"},
        ]
        assert metrics.classify_log_entries(entries) == {
            "abnormal_turns": 1,
            "empty_turns": 1,
            "validation_errors": 1,
            "http_429": 3,
        }

    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            ({"textPayload": "took 14290 ms"}, 0),
            ({"textPayload": "RESOURCE_EXHAUSTED quota"}, 1),
            ({"jsonPayload": {"message": "HTTP 429 Too Many Requests"}}, 1),
            ({"httpRequest": {"status": 429}}, 1),
            ({"httpRequest": {"status": 503}}, 0),
        ],
    )
    def test_429_classification(self, entry, expected):
        assert metrics.classify_log_entries([entry])["http_429"] == expected

    def test_scrape_uses_injected_reader(self):
        seen = []

        def reader(log_filter):
            seen.append(log_filter)
            return [{"textPayload": "ValidationError"}]

        counts = metrics.scrape_log_counts("rev", 0.0, 10.0, reader=reader)
        assert counts == {
            "abnormal_turns": 0,
            "empty_turns": 0,
            "validation_errors": 1,
            "http_429": 0,
        }
        assert "rev" in seen[0]

    def test_scrape_degrades_to_none(self):
        def boom(_f):
            raise RuntimeError("no gcloud")

        assert metrics.scrape_log_counts("rev", 0.0, 1.0, reader=boom) is None

    def test_quota_filter_unchanged_by_default(self):
        f = run_trial._build_log_filter(revision="r", start_epoch=0, end_epoch=1)
        assert "503" in f and "RESOURCE_EXHAUSTED" in f


# ---------------------------------------------------------------------------
# Step 4: driver
# ---------------------------------------------------------------------------

ARMS = {
    "tuned": {
        "base_url": "https://tuned---api.run.app/",
        "audience": "https://api.run.app",
        "revision": "api-00001-tun",
        "tag": "tuned",
    },
    "default": {
        "base_url": "https://default---api.run.app",
        "audience": "https://api.run.app",
        "revision": "api-00002-def",
        "tag": "default",
    },
}


class TestPlan:
    def test_interleaved_and_balanced(self):
        cells = run_ab.plan_cells(
            ["tuned", "default"], ["creative_agent", "trend_scout"], reps=4
        )
        assert len(cells) == 2 * 2 * 2 * 4
        # counterbalanced ABBA: pair k is tuned→default for even k, else reversed
        arms_seq = [c.arm for c in cells]
        pairs = [arms_seq[i : i + 2] for i in range(0, len(arms_seq), 2)]
        for k, pair in enumerate(pairs):
            expected = ["tuned", "default"] if k % 2 == 0 else ["default", "tuned"]
            assert pair == expected, (k, pair)
        # each pair is one (app, campaign, rep) block run on both arms
        for i in range(0, len(cells), 2):
            a, b = cells[i], cells[i + 1]
            assert (a.app, a.campaign, a.rep) == (b.app, b.campaign, b.rep)
        # each arm gets every (app, campaign) the same number of times
        for arm in ("tuned", "default"):
            combos = [(c.app, c.campaign) for c in cells if c.arm == arm]
            assert len(set(combos)) == 4
            assert all(combos.count(x) == 4 for x in set(combos))
        # unique, deterministic ids
        ids = [c.cell_id for c in cells]
        assert len(set(ids)) == len(ids)
        assert ids == [
            c.cell_id
            for c in run_ab.plan_cells(
                ["tuned", "default"], ["creative_agent", "trend_scout"], reps=4
            )
        ]

    @pytest.mark.parametrize(
        ("apps", "reps"),
        [
            (["creative_agent", "trend_scout"], 4),
            (["creative_agent", "trend_scout"], 5),
            (["trend_scout"], 2),
            (["creative_agent"], 10),
        ],
    )
    def test_each_arm_goes_first_in_half_the_pairs(self, apps, reps):
        cells = run_ab.plan_cells(["tuned", "default"], apps, reps=reps)
        firsts = [cells[i].arm for i in range(0, len(cells), 2)]
        assert firsts.count("tuned") == firsts.count("default") == len(firsts) // 2

    def test_first_arm_balanced_within_each_block(self):
        """Even reps: every (app, campaign) gets each arm first equally often."""
        cells = run_ab.plan_cells(
            ["tuned", "default"], ["creative_agent", "trend_scout"], reps=4
        )
        firsts: dict[tuple[str, str], list[str]] = {}
        for i in range(0, len(cells), 2):
            c = cells[i]
            firsts.setdefault((c.app, c.campaign), []).append(c.arm)
        assert len(firsts) == 4
        for arms_first in firsts.values():
            assert arms_first.count("tuned") == arms_first.count("default") == 2

    def test_resume_set_independent_of_order(self):
        """Cell ids name (app, campaign, rep, arm) only; reps extend the plan."""
        small = {
            c.cell_id
            for c in run_ab.plan_cells(["tuned", "default"], ["trend_scout"], 1)
        }
        large = {
            c.cell_id
            for c in run_ab.plan_cells(["tuned", "default"], ["trend_scout"], 3)
        }
        assert small <= large
        assert "trend_scout-anr-r00-default" in small

    def test_cell_id_and_path(self, tmp_path):
        cell = run_ab.plan_cells(["tuned", "default"], ["trend_scout"], reps=1)[0]
        assert cell.cell_id == "trend_scout-prs-r00-tuned"
        assert run_ab.record_path(tmp_path, cell) == (
            tmp_path / "tuned" / "trend_scout" / "trend_scout-prs-r00-tuned.json"
        )

    def test_apps_interleave_within_a_rep(self):
        cells = run_ab.plan_cells(
            ["tuned", "default"], ["creative_agent", "trend_scout"], reps=2
        )
        first_rep = [c.app for c in cells if c.rep == 0]
        # apps alternate between arm pairs rather than all creative first
        assert first_rep[:4] == [
            "creative_agent",
            "creative_agent",
            "trend_scout",
            "trend_scout",
        ]

    def test_unknown_app_rejected(self):
        with pytest.raises(ValueError):
            run_ab.plan_cells(["tuned"], ["nope"], reps=1)


class FakeApi:
    def __init__(self, *, fail_start=False, poll_result=None, poll_exc=None):
        self.calls: list[tuple] = []
        self.fail_start = fail_start
        self.poll_result = poll_result
        self.poll_exc = poll_exc
        self.n = 0

    def mint(self, audience, sa):
        self.calls.append(("mint", audience, sa))
        return "tok"

    def create_session(self, base_url, user_id, token, *, state, app, tt_user):
        self.n += 1
        self.calls.append(("create", base_url, user_id, state, app, tt_user))
        return f"sid-{self.n}"

    def start_run(self, base_url, user_id, sid, token, *, app, message, tt_user):
        self.calls.append(("start", base_url, app, message, tt_user))
        if self.fail_start:
            raise RuntimeError("401 Unauthorized")
        return {}

    def poll(self, base_url, user_id, sid, token, *, app, tt_user):
        self.calls.append(("poll", base_url, app, tt_user))
        if self.poll_exc:
            raise self.poll_exc
        return self.poll_result or (
            [{"timestamp": 100.0}, {"timestamp": 160.0}],
            creative_state(),
            "done",
            None,
        )


def _deps(api: FakeApi, log_counts=None, clock=None):
    ticks = iter(clock or [1000.0, 1100.0, 1200.0, 1300.0, 1400.0, 1500.0] * 50)
    return run_ab.Deps(
        mint=api.mint,
        create_session=api.create_session,
        start_run=api.start_run,
        poll=api.poll,
        log_counts=lambda rev, s, e: log_counts,
        sleep=lambda _s: None,
        clock=lambda: next(ticks),
    )


class TestRunCell:
    def _cell(self, app="creative_agent"):
        return next(
            c
            for c in run_ab.plan_cells(["tuned", "default"], [app], reps=1)
            if c.arm == "default"
        )

    def test_success_record(self):
        api = FakeApi()
        counts = {"abnormal_turns": 1, "empty_turns": 0, "validation_errors": 0}
        rec = run_ab.run_cell(
            self._cell(), ARMS["default"], invoker_sa="sa@x", deps=_deps(api, counts)
        )
        assert rec["status"] == "done" and rec["error"] is None
        assert rec["arm"] == "default" and rec["app"] == "creative_agent"
        assert rec["revision"] == "api-00002-def"
        assert rec["session_id"] == "sid-1"
        assert rec["user_id"] == fixtures.EXPERIMENT_USER_ID
        assert rec["metrics"]["finalized"] is True
        assert rec["log_counts"] == counts
        assert rec["wall_s"] == 60.0  # event span preferred over harness time
        assert rec["harness_wall_s"] == 100.0
        assert rec["state"]["finalize_done"] is True
        mint, create, start, poll = api.calls
        assert mint == ("mint", "https://api.run.app", "sa@x")
        assert create[1] == "https://default---api.run.app"
        assert create[3]["brand"] == fixtures.CAMPAIGNS[0].brand
        assert create[4] == "creative_agent"
        assert create[5] == fixtures.EXPERIMENT_USER_ID
        assert start[3] == fixtures.kickoff_message(
            "creative_agent", fixtures.CAMPAIGNS[0]
        )
        assert poll[3] == fixtures.EXPERIMENT_USER_ID

    def test_failed_start_is_data(self):
        api = FakeApi(fail_start=True)
        rec = run_ab.run_cell(
            self._cell(), ARMS["default"], invoker_sa="sa", deps=_deps(api)
        )
        assert rec["status"] == "exception"
        assert "401" in rec["error"]
        assert rec["session_id"] == "sid-1"
        assert rec["metrics"]["finalized"] is False

    def test_timeout_is_data(self):
        api = FakeApi(poll_exc=TimeoutError("too slow"))
        rec = run_ab.run_cell(
            self._cell("trend_scout"), ARMS["tuned"], invoker_sa="sa", deps=_deps(api)
        )
        assert rec["status"] == "timeout"
        assert rec["metrics"]["completed"] is False

    def test_error_status(self):
        api = FakeApi(poll_result=([], {}, "error", "boom"))
        rec = run_ab.run_cell(
            self._cell(), ARMS["default"], invoker_sa="sa", deps=_deps(api)
        )
        assert rec["status"] == "error" and rec["error"] == "boom"
        assert rec["wall_s"] == rec["harness_wall_s"]


class TestRunAb:
    def test_writes_records_resumes_and_gaps(self, tmp_path):
        api = FakeApi()
        sleeps: list[float] = []
        deps = _deps(api)
        deps = run_ab.Deps(**{**deps.__dict__, "sleep": sleeps.append})
        cells = run_ab.plan_cells(["tuned", "default"], ["trend_scout"], reps=1)
        # Pre-existing record for the first cell: skipped.
        existing = run_ab.record_path(tmp_path, cells[0])
        existing.parent.mkdir(parents=True)
        existing.write_text("{}")

        written = run_ab.run_ab(
            ARMS,
            cells,
            results_root=tmp_path,
            gap_secs=60,
            invoker_sa="sa",
            log_settle_secs=5,
            deps=deps,
        )
        assert len(written) == len(cells) - 1
        assert existing.read_text() == "{}"
        for path in written:
            rec = json.loads(path.read_text())
            assert rec["cell_id"] == path.stem
        # gap only between executed cells; settle sleep before each scrape
        assert sleeps.count(60) == len(written) - 1
        assert sleeps.count(5) == len(written)
        # a second pass runs nothing
        assert (
            run_ab.run_ab(
                ARMS,
                cells,
                results_root=tmp_path,
                gap_secs=60,
                invoker_sa="sa",
                deps=deps,
            )
            == []
        )

    def test_unknown_arm_in_cells_rejected(self, tmp_path):
        cells = run_ab.plan_cells(["tuned", "other"], ["trend_scout"], reps=1)
        with pytest.raises(KeyError):
            run_ab.run_ab(
                ARMS,
                cells,
                results_root=tmp_path,
                gap_secs=0,
                invoker_sa="sa",
                deps=_deps(FakeApi()),
            )


class TestCli:
    def test_dry_run_prints_plan(self, tmp_path, capsys):
        arms = tmp_path / "arms.json"
        arms.write_text(json.dumps(ARMS))
        run_ab.main(
            [
                "--arms",
                str(arms),
                "--apps",
                "creative_agent,trend_scout",
                "--reps",
                "1",
                "--dry-run",
                "--results-root",
                str(tmp_path / "res"),
            ]
        )
        out = capsys.readouterr().out
        assert "creative_agent-prs-r00-tuned" in out
        assert "trend_scout-anr-r00-default" in out
        assert not (tmp_path / "res").exists()

    def test_live_requires_invoker_sa(self, tmp_path, monkeypatch):
        arms = tmp_path / "arms.json"
        arms.write_text(json.dumps(ARMS))
        monkeypatch.delenv("EXP_INVOKER_SA", raising=False)
        with pytest.raises(SystemExit):
            run_ab.main(["--arms", str(arms), "--invoker-sa", ""])

    def test_example_arms_file_is_valid(self):
        example = json.loads(
            (ROOT / "experiments/temperature_ab/arms.example.json").read_text()
        )
        assert set(example) == {"tuned", "default"}
        for cfg in example.values():
            assert {"base_url", "audience", "revision", "tag"} <= set(cfg)

    def test_real_arms_and_results_are_gitignored(self):
        gitignore = (ROOT / ".gitignore").read_text()
        assert "experiments/temperature_ab/arms.json" in gitignore
        assert "experiments/temperature_ab/results/" in gitignore


# ---------------------------------------------------------------------------
# Step 5: analysis
# ---------------------------------------------------------------------------


class TestBootstrap:
    def test_seeded_determinism(self):
        rng = random.Random(3)
        a = [rng.gauss(0.0, 1.0) for _ in range(15)]
        b = [rng.gauss(0.3, 1.0) for _ in range(15)]
        r1 = analyze.bootstrap_diff(a, b, n_resamples=500, seed=7)
        r2 = analyze.bootstrap_diff(a, b, n_resamples=500, seed=7)
        r3 = analyze.bootstrap_diff(a, b, n_resamples=500, seed=8)
        assert r1 == r2
        assert r1 is not None and r3 is not None
        assert r1.diff == r3.diff and (r1.lo, r1.hi) != (r3.lo, r3.hi)

    def test_diff_direction_and_ci_contains_true_diff(self):
        rng = random.Random(0)
        tuned = [rng.gauss(10.0, 1.0) for _ in range(200)]
        default = [rng.gauss(10.5, 1.0) for _ in range(200)]
        res = analyze.bootstrap_diff(default, tuned, seed=1)
        assert res is not None
        assert res.diff == pytest.approx(
            statistics.mean(default) - statistics.mean(tuned)
        )
        assert res.lo < 0.5 < res.hi
        assert res.lo < res.diff < res.hi

    def test_default_resamples(self):
        assert analyze.N_RESAMPLES == 10_000

    def test_empty_side(self):
        assert analyze.bootstrap_diff([], [1.0]) is None
        assert analyze.bootstrap_diff([1.0], []) is None


def rec(
    arm,
    app="creative_agent",
    *,
    status="done",
    wall=100.0,
    logs=None,
    started=1_790_000_000.0,
    **m,
):
    base_metrics = (
        {
            "finalized": True,
            "gates_pass_rate": 0.8,
            "mean_score": 0.75,
            "pass_rate": 0.75,
            "n_issues": 2,
            "n_retry_exhausted": 0,
            "image_qa_attempts": 5,
            "headline_jaccard": 0.1,
        }
        if app == "creative_agent"
        else {
            "completed": True,
            "n_selected": 3,
            "trend_flags": {"a": False, "b": True},
            "info_gtrends_exhausted": False,
            "n_retry_exhausted": 0,
        }
    )
    base_metrics.update(m)
    return {
        "cell_id": f"{app}-{arm}-{random.random()}",
        "arm": arm,
        "app": app,
        "campaign": "prs",
        "rep": 0,
        "status": status,
        "wall_s": wall,
        "started_at": started,
        "metrics": base_metrics,
        "log_counts": logs
        if logs is not None
        else {
            "abnormal_turns": 1,
            "empty_turns": 0,
            "validation_errors": 0,
            "http_429": 0,
        },
    }


def baseline_records():
    out = []
    for arm in ("tuned", "default"):
        for _ in range(3):
            out.append(rec(arm))
            out.append(rec(arm, "trend_scout"))
    return out


def check(records, name):
    results = {c.name: c for c in analyze.evaluate_rule(records)}
    return results[name]


class TestRule:
    def test_baseline_all_pass(self):
        checks = analyze.evaluate_rule(baseline_records())
        assert [c.name for c in checks] == [
            "completion",
            "robustness",
            "creative_quality",
            "creative_diversity",
            "trend_scout_stability",
            "latency",
        ]
        assert all(c.status == "PASS" for c in checks), checks
        assert analyze.verdict(checks) == "PASS"

    # 1. completion
    def test_completion_allows_one_extra_failure(self):
        records = baseline_records()
        records.append(rec("default", status="error"))
        assert check(records, "completion").status == "PASS"

    def test_completion_fails_on_two_extra(self):
        records = baseline_records()
        records.append(rec("default", status="error"))
        records.append(rec("default", "trend_scout", completed=False))
        assert check(records, "completion").status == "FAIL"

    def test_completion_unfinished_counts(self):
        records = baseline_records()
        records.append(rec("default", finalized=False))
        records.append(rec("default", finalized=False))
        assert check(records, "completion").status == "FAIL"
        # the same failures on tuned make it PASS again
        records.append(rec("tuned", finalized=False))
        assert check(records, "completion").status == "PASS"

    # 2. robustness
    def test_robustness_turns(self):
        records = baseline_records()
        records.append(
            rec(
                "default",
                logs={
                    "abnormal_turns": 3,
                    "empty_turns": 2,
                    "validation_errors": 0,
                    "http_429": 0,
                },
            )
        )
        assert check(records, "robustness").status == "FAIL"

    def test_robustness_equal_passes(self):
        assert check(baseline_records(), "robustness").status == "PASS"

    def test_robustness_validation_errors(self):
        records = baseline_records()
        records.append(
            rec(
                "default",
                logs={
                    "abnormal_turns": 1,
                    "empty_turns": 0,
                    "validation_errors": 1,
                    "http_429": 0,
                },
            )
        )
        assert check(records, "robustness").status == "FAIL"

    def test_robustness_retry_exhausted(self):
        records = baseline_records()
        records.append(rec("default", "trend_scout", n_retry_exhausted=1))
        assert check(records, "robustness").status == "FAIL"

    def test_robustness_lower_default_passes(self):
        records = baseline_records()
        records.append(rec("tuned", n_retry_exhausted=2))
        assert check(records, "robustness").status == "PASS"

    def test_robustness_na_without_logs(self):
        records = [
            {**r, "log_counts": None} for r in baseline_records()
        ]  # retry_exhausted is still known, turns are not
        c = check(records, "robustness")
        assert c.status == "N/A"
        assert analyze.verdict(analyze.evaluate_rule(records)) == "INCONCLUSIVE"

    # 3. quality
    def test_quality_gates_threshold(self):
        tuned = [rec("tuned", gates_pass_rate=0.8) for _ in range(2)]
        ok = [rec("default", gates_pass_rate=0.75) for _ in range(2)]
        bad = [rec("default", gates_pass_rate=0.74) for _ in range(2)]
        assert check(tuned + ok, "creative_quality").status == "PASS"
        assert check(tuned + bad, "creative_quality").status == "FAIL"

    def test_quality_mean_score_threshold(self):
        tuned = [rec("tuned", mean_score=0.75)]
        assert (
            check(tuned + [rec("default", mean_score=0.72)], "creative_quality").status
            == "PASS"
        )
        assert (
            check(tuned + [rec("default", mean_score=0.71)], "creative_quality").status
            == "FAIL"
        )

    def test_quality_issues_threshold(self):
        tuned = [rec("tuned", n_issues=2)]
        assert (
            check(tuned + [rec("default", n_issues=3)], "creative_quality").status
            == "PASS"
        )
        assert (
            check(tuned + [rec("default", n_issues=4)], "creative_quality").status
            == "FAIL"
        )

    def test_quality_ignores_none_values(self):
        records = baseline_records() + [rec("default", gates_pass_rate=None)]
        assert check(records, "creative_quality").status == "PASS"

    def test_quality_na_without_creative_runs(self):
        records = [r for r in baseline_records() if r["app"] == "trend_scout"]
        assert check(records, "creative_quality").status == "N/A"

    # 4. diversity
    def test_diversity_threshold(self):
        tuned = [rec("tuned", headline_jaccard=0.10)]
        assert (
            check(
                tuned + [rec("default", headline_jaccard=0.15)], "creative_diversity"
            ).status
            == "PASS"
        )
        assert (
            check(
                tuned + [rec("default", headline_jaccard=0.16)], "creative_diversity"
            ).status
            == "FAIL"
        )

    # 5. stability
    def test_classifier_agreement(self):
        day = 1_790_000_000.0
        runs = [
            rec("tuned", "trend_scout", started=day, trend_flags={"a": True, "b": 0}),
            rec("tuned", "trend_scout", started=day + 60, trend_flags={"a": True}),
            rec(
                "tuned",
                "trend_scout",
                started=day + 120,
                trend_flags={"a": True, "b": True},
            ),
            # another day: a single run contributes nothing
            rec("tuned", "trend_scout", started=day + 3 * 86400, trend_flags={"z": 1}),
        ]
        # a agrees in 3 runs; b disagrees (False vs True) -> 1/2
        assert analyze.classifier_agreement(runs, "tuned") == 0.5
        assert analyze.classifier_agreement(runs, "default") is None

    def test_stability_check(self):
        records = baseline_records()
        assert check(records, "trend_scout_stability").status == "PASS"
        records.append(rec("default", "trend_scout", trend_flags={"a": True}))
        assert check(records, "trend_scout_stability").status == "FAIL"

    def test_stability_na(self):
        records = [r for r in baseline_records() if r["app"] == "creative_agent"]
        assert check(records, "trend_scout_stability").status == "N/A"

    # 6. latency
    def test_latency_threshold(self):
        tuned = [rec("tuned", wall=100.0), rec("tuned", wall=100.0)]
        assert check(tuned + [rec("default", wall=120.0)], "latency").status == "PASS"
        assert check(tuned + [rec("default", wall=121.0)], "latency").status == "FAIL"

    def test_latency_is_per_app(self):
        records = baseline_records() + [
            rec("default", "trend_scout", wall=1000.0) for _ in range(5)
        ]
        c = check(records, "latency")
        assert c.status == "FAIL"
        assert "trend_scout" in c.detail

    def test_latency_uses_only_completed_runs(self):
        records = baseline_records() + [
            rec("default", status="error", wall=5000.0) for _ in range(4)
        ]
        assert check(records, "latency").status == "PASS"

    def test_verdict_fail_beats_na(self):
        checks = [
            analyze.CheckResult("a", "PASS", ""),
            analyze.CheckResult("b", "N/A", ""),
            analyze.CheckResult("c", "FAIL", ""),
        ]
        assert analyze.verdict(checks) == "FAIL"


class TestAnalyzeOutputs:
    def test_metric_table_and_files(self, tmp_path):
        records = baseline_records()
        for r in records:
            path = tmp_path / r["arm"] / r["app"] / f"{r['cell_id']}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(r))
        loaded = analyze.load_records(tmp_path)
        assert len(loaded) == len(records)

        rows = analyze.metric_table(loaded, n_resamples=200)
        gates = next(
            r
            for r in rows
            if r["app"] == "creative_agent" and r["metric"] == "gates_pass_rate"
        )
        assert gates["n_tuned"] == 3 and gates["n_default"] == 3
        assert gates["mean_tuned"] == pytest.approx(0.8)
        assert gates["median_default"] == pytest.approx(0.8)
        assert gates["diff"] == pytest.approx(0.0)

        summary = analyze.write_outputs(loaded, tmp_path, n_resamples=200)
        text = (tmp_path / "summary.md").read_text()
        assert summary == text
        assert "Overall verdict: **PASS**" in text
        assert "completion" in text and "| gates_pass_rate |" in text
        csv_lines = (tmp_path / "records.csv").read_text().splitlines()
        assert len(csv_lines) == len(records) + 1
        assert "gates_pass_rate" in csv_lines[0] and "trend_flags" not in csv_lines[0]

    def test_main_on_empty_dir(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            analyze.main(["--results-root", str(tmp_path)])


def test_no_agent_imports():
    """The harness must stay out of the agent import graph (and vice versa)."""
    for name in list(sys.modules):
        if name.startswith("experiments.temperature_ab"):
            mod = sys.modules[name]
            src = open(mod.__file__ or "").read() if mod.__file__ else ""
            assert "import creative_agent" not in src
            assert "from creative_agent" not in src


class TestRehydrate:
    """--rehydrate re-reads each record's full state (one replay, no new runs)."""

    def _write(self, root: Path, arm: str, app: str, cell_id: str, sid: str | None):
        path = root / arm / app / f"{cell_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "cell_id": cell_id,
                    "arm": arm,
                    "app": app,
                    "base_url": f"https://{arm}",
                    "user_id": "u@x",
                    "session_id": sid,
                    "state": {"brand": "X"},
                    "metrics": {"finalized": False},
                }
            )
        )
        return path

    def test_rewrites_state_and_metrics_from_the_replay(self, tmp_path):
        arms = {
            "tuned": {"audience": "https://aud"},
            "default": {"audience": "https://aud"},
        }
        done = self._write(tmp_path, "tuned", "creative_agent", "c1", "s1")
        no_sid = self._write(tmp_path, "default", "creative_agent", "c2", None)
        minted, replays = [], []

        def replay(base_url, user_id, sid, token, *, app, tt_user):
            replays.append((base_url, sid, token, app, tt_user))
            return [], {"brand": "X", "finalize_done": True}

        deps = run_ab.Deps(
            mint=lambda aud, sa: minted.append(aud) or "tok", replay=replay
        )

        updated = run_ab.rehydrate(
            arms, results_root=tmp_path, invoker_sa="sa", deps=deps
        )

        assert updated == [done]
        record = json.loads(done.read_text())
        assert record["state"]["finalize_done"] is True
        assert record["metrics"]["finalized"] is True
        assert replays == [("https://tuned", "s1", "tok", "creative_agent", "u@x")]
        assert minted == ["https://aud"]  # one token per audience
        assert json.loads(no_sid.read_text())["metrics"] == {"finalized": False}

    def test_an_empty_replay_leaves_the_record_alone(self, tmp_path):
        arms = {"tuned": {"audience": "https://aud"}}
        path = self._write(tmp_path, "tuned", "creative_agent", "c1", "s1")
        before = path.read_text()
        deps = run_ab.Deps(mint=lambda aud, sa: "tok", replay=lambda *a, **k: ([], {}))

        assert run_ab.rehydrate(arms, results_root=tmp_path, deps=deps) == []
        assert path.read_text() == before
