"""Tests for Grounded Brand Assist API and regional/metro scoping in trend_scout."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from runserver import brand_assist
from runserver.authz import (
    AuthzMode,
    UserAuthzMiddleware,
    decide,
    path_user_id,
)
from tests._fakes import FakeToolContext
from trend_scout import callbacks as ts_callbacks
from trend_scout import prompts as ts_prompts
from trend_scout import tools as ts_tools

ALICE = "alice@example.com"
BOB = "bob@example.com"


# ---------------------------------------------------------------------------
# 1. Regional & Metro Scoping in trend_scout
# ---------------------------------------------------------------------------


class TestBuildDailyGtrendsQuery:
    def test_default_us_national_uses_top_terms_with_in_table_max_date(self):
        sql, params = ts_tools._build_daily_gtrends_query()
        assert "`bigquery-public-data.google_trends.top_terms`" in sql
        assert "international_top_terms" not in sql
        assert "INTERVAL 7 DAY" in sql
        assert "SELECT MAX(refresh_date)" in sql
        assert "LIMIT 25" in sql
        assert "dma_name" not in sql
        assert params == []

    def test_us_metro_filter_uses_parameterized_dma_like(self):
        sql, params = ts_tools._build_daily_gtrends_query("us", "  New   York ")
        assert "`bigquery-public-data.google_trends.top_terms`" in sql
        assert "LOWER(dma_name) LIKE LOWER(@region_pattern) ESCAPE '\\\\'" in sql
        param_map = {p.name: p.value for p in params}
        assert param_map == {"region_pattern": "%New York%"}

    def test_international_country_and_region_use_in_table_country_max_date(self):
        sql_gb, params_gb = ts_tools._build_daily_gtrends_query("GB", "England")
        assert "`bigquery-public-data.google_trends.international_top_terms`" in sql_gb
        assert "google_trends.top_terms`" not in sql_gb
        assert "WHERE country_code = @country_code" in sql_gb
        # Subquery for MAX(refresh_date) must also filter by @country_code so
        # international lag relative to US top_terms never yields 0 rows.
        assert sql_gb.count("country_code = @country_code") == 2
        assert "LOWER(region_name) LIKE LOWER(@region_pattern) ESCAPE '\\\\'" in sql_gb
        assert "LIMIT 25" in sql_gb
        assert {p.name: p.value for p in params_gb} == {
            "country_code": "GB",
            "region_pattern": "%England%",
        }

        sql_jp, params_jp = ts_tools._build_daily_gtrends_query("jp", "")
        assert "`bigquery-public-data.google_trends.international_top_terms`" in sql_jp
        assert "region_name" not in sql_jp
        assert {p.name: p.value for p in params_jp} == {"country_code": "JP"}

    def test_escapes_sql_like_wildcards_in_region(self):
        _, params = ts_tools._build_daily_gtrends_query("US", r"100%_real\metro")
        param_map = {p.name: p.value for p in params}
        assert param_map["region_pattern"] == r"%100\%\_real\\metro%"

    @pytest.mark.parametrize(
        "bad_country",
        ["USA", "U", "12", "U1", "us; DROP TABLE x", "../US"],
    )
    def test_rejects_invalid_country_codes(self, bad_country: str):
        with pytest.raises(ValueError, match="trend_country_code"):
            ts_tools._build_daily_gtrends_query(bad_country, "")

    def test_rejects_overlong_region(self):
        with pytest.raises(ValueError, match="trend_region"):
            ts_tools._build_daily_gtrends_query("US", "x" * 81)


class _FakeQueryJob:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = list(rows)
        self.timeouts: list[float | None] = []

    def result(self, timeout: float | None = None) -> list[dict[str, Any]]:
        self.timeouts.append(timeout)
        return list(self._rows)


class _FakeTrendsBQClient:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = list(rows)
        self.calls: list[tuple[str, Any, _FakeQueryJob]] = []

    def query(self, sql: str, job_config: Any = None) -> _FakeQueryJob:
        job = _FakeQueryJob(self.rows)
        self.calls.append((sql, job_config, job))
        return job


class TestGetDailyGtrendsExecution:
    def test_populates_raw_gtrends_refresh_date_and_staleness_for_25_rows(
        self, monkeypatch
    ):
        rows = [
            {
                "term": f"trend-{i}",
                "refresh_date": "2026-10-28",
                "best_rank": i,
            }
            for i in range(1, 26)
        ]
        fake_bq = _FakeTrendsBQClient(rows)
        monkeypatch.setattr(ts_tools, "_get_bigquery_client", lambda: fake_bq)

        ctx = FakeToolContext(
            {
                "trend_country_code": "JP",
                "trend_region": "Tokyo",
                "current_date": "2026-10-28",
            }
        )
        md = asyncio.run(ts_tools.get_daily_gtrends(ctx))

        assert len(fake_bq.calls) == 1
        sql, job_cfg, job = fake_bq.calls[0]
        assert "international_top_terms" in sql
        assert int(job_cfg.job_timeout_ms) == ts_tools.GTRENDS_JOB_TIMEOUT_MS
        assert job.timeouts == [ts_tools.GTRENDS_RESULT_TIMEOUT_SECONDS]
        assert {p.name: p.value for p in job_cfg.query_parameters} == {
            "country_code": "JP",
            "region_pattern": "%Tokyo%",
        }
        assert ctx.state["raw_gtrends"] == [f"trend-{i}" for i in range(1, 26)]
        assert ctx.state["gtrends_refresh_date"] == "2026-10-28"
        assert ctx.state["gtrends_staleness"]["refresh_date"] == "2026-10-28"
        assert ctx.state["gtrends_staleness"]["stale"] is False
        assert "trend-1" in md
        assert "trend-25" in md
        assert "rank" in md

    def test_us_metro_filter_uses_single_regional_query_and_records_refresh_date(
        self, monkeypatch
    ):
        rows = [{"term": "subway-series", "refresh_date": "2026-10-27", "best_rank": 1}]
        fake_bq = _FakeTrendsBQClient(rows)
        monkeypatch.setattr(ts_tools, "_get_bigquery_client", lambda: fake_bq)

        ctx = FakeToolContext(
            {
                "trend_country_code": "US",
                "trend_region": "New York",
                "current_date": "2026-10-28",
            }
        )
        md = asyncio.run(ts_tools.get_daily_gtrends(ctx))

        assert len(fake_bq.calls) == 1
        sql, job_cfg, _ = fake_bq.calls[0]
        assert "`bigquery-public-data.google_trends.top_terms`" in sql
        assert "LOWER(dma_name) LIKE LOWER(@region_pattern)" in sql
        assert {p.name: p.value for p in job_cfg.query_parameters} == {
            "region_pattern": "%New York%"
        }
        assert ctx.state["raw_gtrends"] == ["subway-series"]
        assert ctx.state["gtrends_refresh_date"] == "2026-10-27"
        assert "subway-series" in md

    def test_empty_results_sets_empty_raw_gtrends_and_returns_message(
        self, monkeypatch
    ):
        fake_bq = _FakeTrendsBQClient([])
        monkeypatch.setattr(ts_tools, "_get_bigquery_client", lambda: fake_bq)

        ctx = FakeToolContext(
            {"trend_country_code": "GB", "trend_region": "Nonexistent Shire"}
        )
        msg = asyncio.run(ts_tools.get_daily_gtrends(ctx))
        assert ctx.state["raw_gtrends"] == []
        assert "No Google Search trends matched" in msg
        assert "GB" in msg


class TestTrendScoutCallbacksAndPrompts:
    def _fake_callback_ctx(self, initial_state: dict[str, Any]) -> Any:
        return SimpleNamespace(
            agent_name="trend_scout",
            invocation_id="inv-1",
            user_id=ALICE,
            session=SimpleNamespace(id="sess-1"),
            state=dict(initial_state),
        )

    def test_load_session_state_preserves_caller_seeded_country_and_region(self):
        ctx = self._fake_callback_ctx(
            {"trend_country_code": "jp", "trend_region": "  Tokyo  "}
        )
        ts_callbacks.load_session_state(ctx)
        assert ctx.state["trend_country_code"] == "JP"
        assert ctx.state["trend_region"] == "Tokyo"
        # Turn 2 invocation also preserves them
        ts_callbacks.load_session_state(ctx)
        assert ctx.state["trend_country_code"] == "JP"
        assert ctx.state["trend_region"] == "Tokyo"

    def test_load_session_state_defaults_country_and_region_when_omitted(self):
        ctx = self._fake_callback_ctx({})
        ts_callbacks.load_session_state(ctx)
        assert ctx.state["trend_country_code"] == "US"
        assert ctx.state["trend_region"] == ""

    def test_prompts_include_trend_scope_tokens(self):
        expected = (
            "<trend_scope>Country: {trend_country_code?} | "
            "Region/Metro: {trend_region?}</trend_scope>"
        )
        assert expected in ts_prompts.UNDERSTAND_TRENDS_SEARCHER_INSTR
        assert expected in ts_prompts.PICK_TRENDS_INSTR


# ---------------------------------------------------------------------------
# 2. Grounded Brand Assist Endpoint & Authz
# ---------------------------------------------------------------------------


def _build_test_app(*, mode: AuthzMode) -> FastAPI:
    app = FastAPI()
    app.include_router(brand_assist.router)
    app.add_middleware(
        UserAuthzMiddleware,
        mode=mode,
        caller_ok=lambda auth: auth == "Bearer proxy-ok",
    )
    return app


def _request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    async def _send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.request(method, path, json=json_body, headers=headers)

    return asyncio.run(_send())


class TestBrandAssistAuthzAndValidation:
    def test_path_user_id_and_decide_enforce(self):
        assert path_user_id(f"/brand-assist/{ALICE}") == ALICE
        assert path_user_id(f"/brand-assist/{ALICE}/") == ALICE
        assert path_user_id("/brand-assist") is None
        assert path_user_id(f"/brand-assist/{ALICE}/extra") is None

        assert decide(AuthzMode.ENFORCE, f"/brand-assist/{ALICE}", None) == (
            401,
            "missing trusted user identity",
        )
        assert decide(AuthzMode.ENFORCE, f"/brand-assist/{BOB}", ALICE) == (
            403,
            "user mismatch",
        )
        assert decide(AuthzMode.ENFORCE, f"/brand-assist/{ALICE}", ALICE) is None

    def test_enforce_middleware_returns_401_and_403(self):
        brand_assist.configure(
            authz_mode=AuthzMode.ENFORCE,
            generator=lambda req: brand_assist.BrandAssistSuggestion(
                suggested_products=["SE CE24"]
            ),
        )
        app = _build_test_app(mode=AuthzMode.ENFORCE)

        res_401 = _request(
            app, "POST", f"/brand-assist/{ALICE}", json_body={"brand": "PRS"}
        )
        assert res_401.status_code == 401

        res_403 = _request(
            app,
            "POST",
            f"/brand-assist/{BOB}",
            json_body={"brand": "PRS"},
            headers={"Authorization": "Bearer proxy-ok", "X-TT-User": ALICE},
        )
        assert res_403.status_code == 403

        res_200 = _request(
            app,
            "POST",
            f"/brand-assist/{ALICE}",
            json_body={"brand": "PRS"},
            headers={"Authorization": "Bearer proxy-ok", "X-TT-User": ALICE},
        )
        assert res_200.status_code == 200
        assert res_200.json()["suggested_products"] == ["SE CE24"]

    def test_observe_mode_allows_missing_and_mismatched_user(self, caplog):
        brand_assist.configure(
            authz_mode=AuthzMode.OBSERVE,
            generator=lambda req: brand_assist.BrandAssistSuggestion(
                suggested_products=["Custom 24"]
            ),
        )
        app = _build_test_app(mode=AuthzMode.OBSERVE)

        with caplog.at_level("WARNING"):
            res_missing = _request(
                app, "POST", f"/brand-assist/{ALICE}", json_body={"brand": "PRS"}
            )
            assert res_missing.status_code == 200
            assert res_missing.json()["suggested_products"] == ["Custom 24"]

            res_mismatch = _request(
                app,
                "POST",
                f"/brand-assist/{BOB}",
                json_body={"brand": "PRS"},
                headers={"Authorization": "Bearer proxy-ok", "X-TT-User": ALICE},
            )
            assert res_mismatch.status_code == 200
            assert res_mismatch.json()["suggested_products"] == ["Custom 24"]

        assert any("authz observe" in rec.message for rec in caplog.records)

    def test_cached_genai_client_reuses_instance_per_model(self, monkeypatch):
        created: list[dict[str, Any]] = []

        def _fake_client(**kwargs: Any):
            inst = SimpleNamespace(**kwargs)
            created.append(kwargs)
            return inst

        brand_assist._cached_genai_client.cache_clear()
        try:
            monkeypatch.setattr(brand_assist.genai, "Client", _fake_client)
            monkeypatch.setenv("BRAND_ASSIST_MODEL", "gemini-3.5-flash")
            c1 = brand_assist._get_genai_client()
            c2 = brand_assist._get_genai_client()
            assert c1 is c2
            assert len(created) == 1

            monkeypatch.setenv("BRAND_ASSIST_MODEL", "gemini-3.8-flash")
            c3 = brand_assist._get_genai_client()
            assert c3 is not c1
            assert len(created) == 2
        finally:
            brand_assist._cached_genai_client.cache_clear()

    @pytest.mark.parametrize(
        "bad_payload",
        [
            {"brand": "   "},
            {"brand": ""},
            {"brand": "PRS", "extra_field": "nope"},
            {"brand": "PRS", "domain_hint": "127.0.0.1"},
            {"brand": "PRS", "domain_hint": "https://169.254.169.254/latest/meta-data"},
            {"brand": "PRS", "domain_hint": "localhost"},
            {"brand": "PRS", "domain_hint": "https://localhost:8080/"},
            {"brand": "PRS", "domain_hint": "metadata.google.internal"},
            {"brand": "PRS", "domain_hint": "service.local"},
            {"brand": "PRS", "domain_hint": "https://user:pass@example.com"},
            {"brand": "PRS", "domain_hint": "ftp://example.com"},
            {"brand": "PRS", "domain_hint": "not a valid domain"},
        ],
    )
    def test_rejects_empty_brand_extra_fields_and_ssrf_domain_hints(
        self, bad_payload: dict[str, Any]
    ):
        brand_assist.configure(authz_mode=AuthzMode.TRUST_CLIENT)
        app = _build_test_app(mode=AuthzMode.TRUST_CLIENT)
        res = _request(app, "POST", f"/brand-assist/{ALICE}", json_body=bad_payload)
        assert res.status_code == 422

    @pytest.mark.parametrize(
        ("raw_hint", "expected_host"),
        [
            ("", ""),
            ("  prsguitars.com  ", "prsguitars.com"),
            ("https://www.PRSGuitars.com/products/se/", "www.prsguitars.com"),
            ("http://example.co.uk/", "example.co.uk"),
            ("https://example.com", "example.com"),
        ],
    )
    def test_accepts_and_normalizes_valid_domain_hints(
        self, raw_hint: str, expected_host: str
    ):
        req = brand_assist.BrandAssistRequest(
            brand="  PRS Guitars ", target_product=" SE CE24 ", domain_hint=raw_hint
        )
        assert req.brand == "PRS Guitars"
        assert req.target_product == "SE CE24"
        assert req.domain_hint == expected_host

    def test_filters_and_normalizes_palette_hex_and_clamps_lists(self, monkeypatch):
        captured: dict[str, Any] = {}

        class _FakeModels:
            def generate_content(self, *, model: str, contents: str, config: Any):
                captured["model"] = model
                captured["contents"] = contents
                captured["config"] = config
                raw_json = (
                    "```json\n"
                    "{\n"
                    '  "suggested_products": ["SE CE24", "Custom 24", "Silver Sky", "McCarty 594", "S2 Vela", "Extra 6th"],\n'
                    '  "target_audience": "  Gigging guitarists and tone enthusiasts. ",\n'
                    '  "key_selling_points": " Mahogany warmth; coil-split versatility ",\n'
                    '  "brand_palette_hex": ["#1a1a1a", "not-a-hex", "#123", "#f5f2eb", "#c9A227", "#000000"],\n'
                    '  "tone_summary": " Warm editorial stage lighting and figured maple grain. ",\n'
                    '  "brand_cues": ["bird inlays", "figured maple top", "PRS headstock", "nickel hardware", "extra cue"]\n'
                    "}\n"
                    "```"
                )
                return SimpleNamespace(text=raw_json, parsed=None)

        monkeypatch.setattr(
            brand_assist,
            "_get_genai_client",
            lambda: SimpleNamespace(models=_FakeModels()),
        )
        brand_assist.configure(authz_mode=AuthzMode.TRUST_CLIENT, generator=None)
        app = _build_test_app(mode=AuthzMode.TRUST_CLIENT)

        res = _request(
            app,
            "POST",
            f"/brand-assist/{ALICE}",
            json_body={
                "brand": "PRS <Guitars>",
                "target_product": "SE CE24",
                "domain_hint": "https://prsguitars.com/guitars",
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert len(body["suggested_products"]) == 5
        assert body["brand_palette_hex"] == ["#1A1A1A", "#F5F2EB", "#C9A227"]
        assert len(body["brand_cues"]) == 4
        assert body["target_audience"] == "Gigging guitarists and tone enthusiasts."
        # Prompt escapes XML angle brackets and passes normalized bare domain
        assert "<brand>PRS &lt;Guitars&gt;</brand>" in captured["contents"]
        assert "<domain_hint>prsguitars.com</domain_hint>" in captured["contents"]

    def test_maps_upstream_errors_to_502_and_timeouts_to_504(self):
        def _boom(_req: brand_assist.BrandAssistRequest):
            raise RuntimeError("secret internal traceback detail")

        brand_assist.configure(authz_mode=AuthzMode.TRUST_CLIENT, generator=_boom)
        app = _build_test_app(mode=AuthzMode.TRUST_CLIENT)
        res_502 = _request(
            app, "POST", f"/brand-assist/{ALICE}", json_body={"brand": "PRS"}
        )
        assert res_502.status_code == 502
        assert "secret internal" not in res_502.text

        def _timeout(_req: brand_assist.BrandAssistRequest):
            raise TimeoutError("upstream deadline")

        brand_assist.configure(authz_mode=AuthzMode.TRUST_CLIENT, generator=_timeout)
        res_504 = _request(
            app, "POST", f"/brand-assist/{ALICE}", json_body={"brand": "PRS"}
        )
        assert res_504.status_code == 504
        assert "upstream deadline" not in res_504.text
