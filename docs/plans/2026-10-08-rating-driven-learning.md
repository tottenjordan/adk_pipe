# Rating-Driven Learning Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` (or `subagent-driven-development`) to implement this plan task-by-task. Save a copy as `docs/plans/2026-10-08-rating-driven-learning.md` in PR A.

**Goal:** Let human creative ratings, when the user opts in, actively shape future creative runs for the same brand in three ways:
- **prompt guidance:** what worked, and why things failed;
- **style steering:** weight the style shortlist by past ratings;
- **stricter checks:** recurring failure reasons switch on tighter deterministic gates.

**Architecture:**
- **Capture:** ratings gain structured, allowlisted context when they are written: brand, canonical visual style, copy tone, angle and **fail-reason chips**. The free-text note never reaches a prompt.
- **Read:** a no-LLM learning step reads the brand's ratings from BigQuery. It is folded into the existing `load_brand_history` node in the research START fan-out, so there is still one shortlist draw and no added latency.
- **Effects:** the step turns the ratings into three outputs:
  - a ≤80-word note (`rating_signals`);
  - weights for the style shortlist;
  - a list of strictness flags (`rating_strictness`) that the existing copy gate, concept gate and image QA read.
- **Control:** a per-run toggle (default **off**), a global env kill switch, and env knobs per effect and for minimum sample sizes.

**Tech Stack:** Python 3.13 / Google ADK 2.10 graph Workflows, BigQuery (parameterised SQL), FastAPI (`runserver/`), Next.js 16 frontend, pytest, Vitest. uv / ruff / ty.

---

## Context

**Why:** today ratings only calibrate the judge (`/runs` kappa line). The user asked how ratings could actively shape future runs, optionally and under their control.

**User decisions (2026-10-08):**
- **Scope:** **same brand, all users.** This matches brand memory; `creative_evals` already spans all users.
- **Default:** **off; opt in per run**, plus a global env kill switch.
- **Effects:** **all three**: prompt guidance, style steering and stricter checks.

**Facts from the code:**
- **Ratings table:** `creative_ratings` (`runserver/ratings_store.py:29-48`) has `session_id, app_name, creative_key ('visual:<concept_name>'|'copy:<original_id>'), kind, user_id, verdict, score, note, judge_*, judge_source`. It has **no brand column**.
  - `creative_evals` has no `session_id`; its uuid is an 8-char hash of `[session_id,"eval"]`.
  - So the clean join is to **write brand (and creative attributes) onto the rating row at PUT time**. `http_put_rating` (`runserver/ratings.py:388-422`) already loads the session state and the creative (`creative_index`, `_find_eval`).
- **Brand history** (`creative_agent/brand_history.py`) is the pattern to copy: parameterised SELECT, `LOWER(TRIM(brand)) = LOWER(@brand)`, an 8 s job timeout, fail-open to `{}`, allowlisted outputs only (`canonical_style`, the tone Literal, `DIMENSION_LABELS`, gate labels), and a ≤120-word brace-free note.
  - It runs as `load_brand_history` (`creative_agent/agent.py:446-470`) via `brand_history_state_delta` (`brand_history.py:354-388`) with a 10 s `asyncio.wait_for`.
  - It re-draws `style_shortlist` with `pick_style_shortlist(exclude=…)` unless `visual_style_preference` is set.
- **Per-run toggle pattern:** `interactiveTrendPick` follows the chain checkbox (`frontend/src/app/page.tsx:355-372`) → `buildInitialState` (`frontend/src/lib/initial-state.ts`) → session state → backend `setdefault` (`creative_agent/callbacks.py:68-100`) → Duplicate-brief restore (`frontend/src/lib/run-history.ts:203-226`).
- **Trust risk:** verdicts and notes are user-entered and would steer *other users'* runs for the brand. The plan has three mitigations:
  - **Allowlisted values only:** canonical styles, the tone Literal and a fixed fail-reason enum. The free-text note is **never** read by the learning step.
  - **Minimum sample sizes**, plus a cap on each effect's influence.
  - **Every applied signal is visible** in the run, brief and eval report, so it can be audited.

## How it works (one run, opted in)

1. **Opt in:** the user checks **"Learn from past ratings for this brand"** on the campaign form. The session gets `learn_from_ratings: true`.
2. **Read the ratings:** the research step `load_brand_history` (renamed in the graph docs as the "learning context" step; the code name is unchanged) reads the latest `creative_evals` rows (as today) **and**, because the toggle is on and `RATING_LEARNING_ENABLED` is true, the brand's `creative_ratings`. That means the last `RATING_LEARNING_WINDOW_DAYS` (90) days, capped at 500 rows.
3. **Compute the outputs** (pure functions, allowlisted values only):
   - **`rating_signals`** (≤80 words), for example:
     > Your team's ratings for PRS (23): rated well: Candid 35mm film photo, Comic panel visuals; Humorous, Relatable/Meme-based copy. Common fail reasons: product hard to see (6), in-image text problems (4). Favour what was rated well and avoid these failure causes.
   - **Style weights:** each canonical family's pass rate. Any family with ≥ `RATING_STYLE_MIN` (3) ratings and a pass rate ≤ 0.34 is excluded. Families with a pass rate ≥ 0.67 are drawn first within their stratum.
   - **`rating_strictness`:** the fail reasons that are both frequent (≥ `RATING_REASON_MIN` (3)) and dominant (≥ 30% of the brand's fails), each mapped to a fixed tightening (table below).
4. **Effects:**
   - The brief writer, ad-copy drafter and art director read `{rating_signals?}`.
   - `style_shortlist` is drawn once, with both brand-history exclusions and rating weights.
   - The copy gate, concept gate and image QA read `rating_strictness` and apply the mapped tightenings.
5. **Recording:** `rating_signals_applied` (`{ratings, signals, strictness, styles_excluded, styles_preferred}`) is written to state. It is shown on the run and results pages ("Learned from 23 ratings: …") and recorded on the eval report as `learning_used` + `learning_flags`, so calibration can separate learned runs.

**Fail-reason enum → tightening** (a fixed map in `creative_agent/rating_signals.py`):

| Reason chip (UI label) | Enum | Tightening when dominant |
|---|---|---|
| Product hard to see | `product_not_visible` | concept guard appends "the product is large and in the foreground"; image QA requires a *prominent* product |
| In-image text problems | `text_problem` | concept gate text cap 2 → 1 concept |
| Unwanted logo / trademark | `unwanted_logo` | render prompt gets an explicit "no logos, brand marks or trademarks other than {brand}" line |
| Weak call to action | `weak_cta` | copy gate CTA ≤ 6 words (from 8) |
| Off-brief / wrong message | `off_brief` | the critic's self-reported `delivers`/proposition check failures become blocking for all items (today only proposition/mandatories) |
| Trend unclear | `trend_unclear` | image QA requires the motif to be *clearly* visible; concept guard always appends the motif |
| Cluttered / weak composition | `cluttered` | guidance only (no deterministic check) |
| Off-brand tone | `off_brand_tone` | guidance only |
| Visual artifacts / quality | `artifacts` | guidance only (image QA already fails severe artifacts) |
| Other | `other` | ignored for strictness |

**Controls:**
- **Per run:** the form checkbox, default off. Restored by Duplicate brief and visible in the run's metadata.
- **Global kill switch:** `RATING_LEARNING_ENABLED` (default `true`, meaning opt-in is allowed). Set to `false` to disable everywhere, even when the toggle is on.
- **Per effect:** `RATING_LEARNING_EFFECTS` (default `guidance,styles,checks`); drop an item to disable that effect.
- **Thresholds:**
  - `RATING_LEARNING_MIN_RATINGS` (default 8): below this, nothing is applied and the run shows "not enough ratings yet".
  - `RATING_STYLE_MIN` (3), `RATING_REASON_MIN` (3), `RATING_LEARNING_WINDOW_DAYS` (90).
- **Engine knobs:** all of the above go in `deployment/deploy_agent.py` `ENV_VAR_DICT` and `.env.example`, using the `BaseAgentConfiguration`/`ResearchConfiguration` `field(default_factory=…)` pattern.
- **Batch runs (CRF):** there is no form, so they stay off unless a future message field sets `learn_from_ratings`. That's out of scope; it's documented.

---

## PR A — Capture structured rating context (`feat/rating-context`)

### Task A1: Extend the rating row schema

**Files:**
- Modify: `runserver/ratings_store.py:29-61` (`RATING_COLUMN_TYPES`, `UPDATABLE`, `build_upsert_sql` covers new columns automatically if it iterates the dict — verify)
- Modify: `deployment/create_bq_tables.sh:76-78`
- Test: `tests/test_ratings_store.py`, `tests/test_create_bq_tables.py`

**Step 1: Write the failing test**

```python
def test_rating_columns_include_learning_context():
    from runserver.ratings_store import RATING_COLUMN_TYPES, UPDATABLE

    for col, typ in {
        "brand": "STRING",
        "visual_style": "STRING",
        "tone_style": "STRING",
        "angle_id": "STRING",
        "fail_reasons": "ARRAY<STRING>",
    }.items():
        assert RATING_COLUMN_TYPES[col] == typ
        assert col in UPDATABLE
```

**Step 2:** `GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/test_ratings_store.py -q`. Expected: FAIL (KeyError: 'brand').

**Step 3:** Add the five columns to `RATING_COLUMN_TYPES` and to `UPDATABLE`. `build_upsert_sql` must bind `fail_reasons` as an `ArrayQueryParameter("fail_reasons", "STRING", values)`; extend the param builder and add a test asserting the param type. Add the columns to `create_bq_tables.sh` (`brand:STRING,visual_style:STRING,tone_style:STRING,angle_id:STRING,fail_reasons:STRING` with `REPEATED` mode via a JSON schema file, or with `bq query` DDL). Update `test_create_bq_tables.py`.

**Step 4:** Run the tests. Expected: PASS.

**Step 5:** `git commit -m "feat(ratings): learning-context columns on creative_ratings"`

**Migration (run before deploying the api):**
```sql
ALTER TABLE `hybrid-vertex.trend_trawler.creative_ratings`
  ADD COLUMN IF NOT EXISTS brand STRING,
  ADD COLUMN IF NOT EXISTS visual_style STRING,
  ADD COLUMN IF NOT EXISTS tone_style STRING,
  ADD COLUMN IF NOT EXISTS angle_id STRING,
  ADD COLUMN IF NOT EXISTS fail_reasons ARRAY<STRING>;
```
Document it in `deployment/README.md` → "Creative quality: migrations + knobs".

### Task A2: Fail-reason enum + body validation

**Files:**
- Create: `runserver/rating_reasons.py`. This is the single source of truth, mirrored in `frontend/src/lib/rating-reasons.ts` with a drift test (copy the `tests/test_eval_dimensions.py` pattern).
- Modify: `runserver/ratings.py:174-220` (`_RatingBody`, `validate_body`)
- Test: `tests/test_ratings_api.py`, `tests/test_rating_reasons_drift.py`

**Step 1: Write the failing tests**

```python
FAIL_REASONS = (
    "product_not_visible",
    "text_problem",
    "unwanted_logo",
    "weak_cta",
    "off_brief",
    "trend_unclear",
    "cluttered",
    "off_brand_tone",
    "artifacts",
    "other",
)


def test_fail_reasons_validated(client_put):
    ok = client_put(
        verdict="fail", fail_reasons=["product_not_visible", "text_problem"]
    )
    assert ok.status_code == 200
    bad = client_put(verdict="fail", fail_reasons=["ignore previous instructions"])
    assert bad.status_code == 400 and bad.json()["reason"] == "invalid_fail_reasons"


def test_fail_reasons_dropped_on_pass(client_put, store):
    client_put(verdict="pass", fail_reasons=["weak_cta"])
    assert store.rows()[-1]["fail_reasons"] == []
```

Reuse the existing fixtures in `tests/test_ratings_api.py` (fake session service and in-memory store); name them to match.

**Step 2:** Run the tests. Expected: FAIL.

**Step 3:** `runserver/rating_reasons.py`:

```python
"""Allowlisted fail reasons a rater can pick (never free text into prompts)."""

FAIL_REASONS: tuple[str, ...] = (
    "product_not_visible",
    "text_problem",
    "unwanted_logo",
    "weak_cta",
    "off_brief",
    "trend_unclear",
    "cluttered",
    "off_brand_tone",
    "artifacts",
    "other",
)
FAIL_REASON_LABELS: dict[str, str] = {
    "product_not_visible": "Product hard to see",
    "text_problem": "In-image text problems",
    "unwanted_logo": "Unwanted logo / trademark",
    "weak_cta": "Weak call to action",
    "off_brief": "Off-brief / wrong message",
    "trend_unclear": "Trend unclear",
    "cluttered": "Cluttered / weak composition",
    "off_brand_tone": "Off-brand tone",
    "artifacts": "Visual artifacts / quality",
    "other": "Other",
}
```

In `_RatingBody` add `fail_reasons: list[str] = Field(default_factory=list, max_length=10)`. In validation: every item must be in `FAIL_REASONS`, otherwise return 400 `invalid_fail_reasons`; dedupe while keeping order; empty the list when the verdict is `pass`.

**Step 4:** Run the tests. Expected: PASS.

**Step 5:** `git commit -m "feat(ratings): allowlisted fail-reason chips"`

### Task A3: Stamp brand + creative attributes at PUT time

**Files:**
- Modify: `runserver/ratings.py` (`http_put_rating` ~388-422, new pure helper `learning_context(state, kind, creative_key)`)
- Test: `tests/test_ratings_api.py`

**Step 1: Write the failing test**

```python
def test_put_stamps_learning_context(client_put, store, seeded_state):
    seeded_state["brand"] = "  Paul Reed Smith (PRS) "
    seeded_state["final_visual_concepts"] = {
        "visual_concepts": [
            {
                "concept_name": "Stage Left",
                "visual_style": "candid 35mm film photo",
                "angle_id": "A2",
            }
        ]
    }
    client_put(kind="visual", creative_key="visual:Stage Left", verdict="pass")
    row = store.rows()[-1]
    assert row["brand"] == "paul reed smith (prs)"  # normalised like brand_history
    assert row["visual_style"] == "Candid 35mm film photo"  # canonical_style
    assert row["angle_id"] == "A2"
```

Add a copy-kind test as well: `tone_style` must be in the tone Literal (otherwise `""`), and `angle_id` must come from `ad_copy_critique`.

**Step 2:** Run the tests. Expected: FAIL.

**Step 3:** Implement `learning_context`:
- brand = `str(state.get("brand","")).strip().lower()`;
- visual style via `creative_agent.style_shortlist.canonical_style`;
- tone validated against `creative_agent.schemas.FinalAdCopy` `tone_style` args.

runserver may import from the creative_agent facade (it already uses `CreativeBrief` via the facade); if `canonical_style` isn't exported there, export it from `creative_agent/__init__.py`. Merge the result into the row before `upsert`.

**Step 4:** Run the tests. Expected: PASS.

**Step 5:** `git commit -m "feat(ratings): stamp brand, style, tone and angle onto each rating"`

### Task A4: Fail-reason chips in the rating UI

**Files:**
- Create: `frontend/src/lib/rating-reasons.ts` (the mirror)
- Modify: `frontend/src/lib/ratings.ts` (`buildRatingPayload` adds `fail_reasons`), `frontend/src/app/results/[sessionId]/rating-control.tsx`
- Test: `frontend/src/__tests__/rating-control.test.tsx`, `ratings.test.ts`, `tests/test_rating_reasons_drift.py`

**Steps:**
1. **Failing test:** selecting Fail reveals the reason chips (a multi-select toggle group with aria-pressed); the payload carries the selected enums; switching to Pass clears them.
2. **Run it:** `cd frontend && npx vitest run src/__tests__/rating-control.test.tsx`. Expected: FAIL.
3. **Implement:** proof-room rules (sentence-case FieldLabel "Why did it fail? (optional)", muted chips, primary only for Save).
4. **Run it:** the tests and `npm run lint && npm run build` pass.
5. **Commit:** `feat(frontend): fail-reason chips on ratings`

**PR A gate:** `uv run ruff format . && uv run ruff format --check . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 4`; frontend `npm run lint && npm test && npm run build`. Open the PR and **merge only after every GitHub check passes**. Run the ALTER before deploying the api.

---

## PR B — Learning step: signals, guidance, style steering, per-run toggle (`feat/rating-learning`)

### Task B1: Config knobs

**Files:** `creative_agent/config.py` (next to the brand-history knobs at :53-54, :102-113, :175-183), `deployment/deploy_agent.py` `ENV_VAR_DICT` (:67-70), `.env.example`, `tests/test_config.py`.

**Step 1: Failing test** (defaults and clamping):

```python
def test_rating_learning_defaults(monkeypatch):
    for k in (
        "RATING_LEARNING_ENABLED",
        "RATING_LEARNING_EFFECTS",
        "RATING_LEARNING_MIN_RATINGS",
    ):
        monkeypatch.delenv(k, raising=False)
    from creative_agent.config import ResearchConfiguration

    c = ResearchConfiguration()
    assert c.rating_learning_enabled is True
    assert c.rating_learning_effects == frozenset({"guidance", "styles", "checks"})
    assert c.rating_learning_min_ratings == 8
    assert c.rating_style_min == 3 and c.rating_reason_min == 3
    assert c.rating_learning_window_days == 90
```

Also test that `RATING_LEARNING_EFFECTS="guidance"` gives `{"guidance"}`, that unknown items are ignored, and that `RATING_LEARNING_MIN_RATINGS` is clamped to 1–200.

**Steps 2–5:** run the test (fails), implement with `field(default_factory=…)` parsers, run it (passes), commit `feat(config): rating-learning knobs`.

### Task B2: `rating_signals.py` — query + aggregation (pure)

**Files:**
- Create: `creative_agent/rating_signals.py`
- Test: `tests/test_rating_signals.py`

**Step 1: Failing tests** (pure aggregation from fake rows):

```python
from creative_agent.rating_signals import aggregate_ratings

ROWS = (
    [
        {
            "kind": "visual",
            "verdict": "pass",
            "visual_style": "Candid 35mm film photo",
            "fail_reasons": [],
        }
    ]
    * 3
    + [
        {
            "kind": "visual",
            "verdict": "fail",
            "visual_style": "Isometric miniature world",
            "fail_reasons": ["product_not_visible"],
        }
    ]
    * 3
    + [
        {
            "kind": "ad_copy",
            "verdict": "pass",
            "tone_style": "Humorous",
            "fail_reasons": [],
        }
    ]
    * 2
    + [
        {
            "kind": "visual",
            "verdict": "fail",
            "visual_style": "evil {style}",
            "fail_reasons": ["text_problem"],
        }
    ]
)


def test_aggregate_ratings_allowlisted_and_thresholded():
    s = aggregate_ratings(ROWS, style_min=3, reason_min=3)
    assert s["ratings"] == 9
    assert s["styles_preferred"] == ["Candid 35mm film photo"]
    assert s["styles_excluded"] == ["Isometric miniature world"]
    assert s["tones_preferred"] == ["Humorous"]
    assert s["fail_reasons"]["product_not_visible"] == 3
    assert "evil {style}" not in str(s)  # non-canonical style dropped
    assert s["strictness"] == ["product_not_visible"]  # ≥3 and ≥30% of 4 fails
```

Also: below `min_ratings` → `aggregate_ratings` still returns counts, but `apply` (B3) yields nothing.

**Step 2:** Run the tests. Expected: FAIL (module missing).

**Step 3:** Implement the module:
- **`build_ratings_query(table, *, brand_param="@brand", days_param="@days")`:** `SELECT kind, verdict, visual_style, tone_style, fail_reasons FROM {table} WHERE brand = LOWER(TRIM(@brand)) AND updated_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY) ORDER BY updated_at DESC LIMIT 500`. Fully parameterised; the table name comes only from config (`BQ_TABLE_RATINGS`, default `creative_ratings`, added to `BaseAgentConfiguration` alongside `BQ_TABLE_EVALS`).
- **`fetch_ratings(brand, *, days, bq_client=None) -> list[dict]`:** an 8 s job timeout, fail-open to `[]`. Copy `brand_history.fetch_brand_history`'s client handling and logging.
- **`aggregate_ratings(rows, *, style_min, reason_min) -> dict`:**
  - styles and tones are allowlisted via `canonical_style` and the tone Literal;
  - fail reasons via `runserver`-independent `FAIL_REASONS`. Keep the enum in `creative_agent/rating_signals.py` as the agent-side copy, and add a test asserting it equals `runserver.rating_reasons.FAIL_REASONS`, so agents don't import runserver.
  - Strictness = reasons with count ≥ `reason_min` **and** ≥ 30% of all fail ratings, minus `other`.
- **`format_rating_signals(s, brand) -> str`:** ≤80 words, brace-free (reuse `brand_history._clean` by moving it to a tiny shared helper), using only allowlisted labels (`FAIL_REASON_LABELS`).

**Step 4:** Run the tests. Expected: PASS.

**Step 5:** `git commit -m "feat(creative_agent): rating signals — query, allowlisted aggregation, note"`

### Task B3: Fold into the learning step + one shortlist draw

**Files:**
- Modify: `creative_agent/brand_history.py` (`brand_history_state_delta` :354-388) and `creative_agent/style_shortlist.py` (`pick_style_shortlist` gains `prefer: Sequence[str] = ()`)
- Modify: `creative_agent/callbacks.py` (`setdefault("learn_from_ratings", False)`)
- Test: `tests/test_brand_history.py`, `tests/test_style_shortlist.py`, `tests/test_creative_agent_graph.py`

**Step 1: Failing tests:**

```python
def test_rating_signals_only_when_opted_in(monkeypatch):
    # toggle off → no ratings query, no rating keys
    delta = run_delta(
        state={"brand": "PRS", "learn_from_ratings": False}, ratings=ROWS * 2
    )
    assert (
        "rating_signals" not in delta and fake_bq.queries_for("creative_ratings") == []
    )


def test_rating_signals_applied_when_opted_in_and_enough_ratings():
    delta = run_delta(
        state={"brand": "PRS", "learn_from_ratings": True}, ratings=ROWS * 2
    )
    assert delta["rating_signals"].startswith("Your team's ratings for PRS")
    assert delta["rating_strictness"] == ["product_not_visible"]
    assert "Isometric miniature world" not in delta["style_shortlist"]
    assert delta["rating_signals_applied"]["ratings"] == 18


def test_kill_switch_overrides_toggle(monkeypatch):
    monkeypatch.setattr(config, "rating_learning_enabled", False)
    assert "rating_signals" not in run_delta(
        state={"brand": "PRS", "learn_from_ratings": True}, ratings=ROWS * 2
    )


def test_below_min_ratings_reports_not_enough():
    d = run_delta(state={"brand": "PRS", "learn_from_ratings": True}, ratings=ROWS[:3])
    assert d["rating_signals_applied"] == {
        "ratings": 3,
        "applied": False,
        "reason": "not_enough_ratings",
    }


def test_pick_style_shortlist_prefers_and_excludes_keep_stratification():
    picks = pick_style_shortlist(
        exclude={"Isometric miniature world"},
        prefer=["Candid 35mm film photo"],
        rng=random.Random(1),
    )
    assert (
        "Candid 35mm film photo" in picks and "Isometric miniature world" not in picks
    )
    assert stratum_counts(picks) == (2, 3, 1)
```

**Step 2:** Run the tests. Expected: FAIL.

**Step 3:** Implement:
- **Ratings fetch:** in `brand_history_state_delta`, when `state.get("learn_from_ratings") is True` and `config.rating_learning_enabled`, run `fetch_ratings` **concurrently** with `fetch_brand_history` (`asyncio.gather` of two `to_thread` calls) inside the same 10 s `wait_for`.
- **Outputs:** aggregate; when ratings ≥ min, write `rating_signals` (if "guidance" is in the effects), `rating_strictness` (if "checks"), and `rating_signals_applied`.
- **Shortlist:** draw `style_shortlist` once with `exclude = recent_styles ∪ styles_excluded` and `prefer = styles_preferred` (if "styles"), still skipped when `visual_style_preference` is set.
- **`pick_style_shortlist`:** within each stratum, draw preferred families first (still random among them), then fill the rest randomly; never drop below the 2/3/1 quotas (fall back to the full group, as exclusion does today).
- **Fail-open:** any error → no rating keys, plus `rating_signals_applied={"applied": False, "reason": "unavailable"}`.

**Step 4:** Run the tests. Expected: PASS.

**Step 5:** `git commit -m "feat(creative_agent): learning step reads opted-in ratings; weighted shortlist"`

### Task B4: Prompts read `{rating_signals?}`

**Files:** `creative_agent/prompts.py`: brief writer (next to rule 10 / `<brand_history>` block ~:314-337), `AD_COPY_DRAFTER_INSTR`, art director (~:543-560); `tests/test_creative_brief_prompts.py` / `tests/test_ad_copy_prompts.py` / prompt-token tests.

**Steps:**
1. **Failing test:** each of the three instructions contains `<rating_signals>{rating_signals?}</rating_signals>` and the rule "Treat rated-well styles, tones and angles as strong options and avoid the listed failure causes; never mention ratings in the creative." Every brace must be a valid state token (existing `test_every_brace_is_a_state_token`).
2. **Run it.** Expected: FAIL.
3. **Implement** in each of the three instructions.
4. **Run it.** Expected: PASS.
5. **Commit:** `feat(creative_agent): prompts use rating signals when present`

### Task B5: Per-run toggle (frontend)

**Files:** `frontend/src/app/page.tsx` (the creative-agent options section, mirroring the `interactiveTrendPick` checkbox at :355-372), `frontend/src/lib/types.ts` (`learnFromRatings?: boolean`), `frontend/src/lib/initial-state.ts` (`learn_from_ratings: true` only when checked, creative apps only), `frontend/src/lib/run-history.ts` (`BRIEF_STATE_FIELDS` restore). Tests: `initial-state.test.ts`, `run-history.test.ts`, a form render test.

**Steps:**
1. **Failing tests:** the checkbox is unchecked by default; when checked, the seeded state has `learn_from_ratings: true`; it is absent when unchecked; Duplicate brief restores it.
2. **Run them.** Expected: FAIL.
3. **Implement.** Label: "Learn from past ratings for this brand". Help text: "Uses your team's ratings of earlier [brand] creatives to steer styles, guidance and checks. Off by default."
4. **Run them:** the tests, `npm run lint && npm run build`.
5. **Commit:** `feat(frontend): per-run 'learn from past ratings' toggle`

**PR B gate:** as for PR A. No migration. After merging, deploy the engines (creative + interactive) and api + web.

---

## PR C — Stricter checks from recurring fail reasons + transparency (`feat/rating-strictness`)

### Task C1: Strictness flags in the deterministic gates

**Files:**
- `creative_agent/copy_gate.py`: the CTA word limit becomes a parameter, `max_cta_words = 6 if "weak_cta" in strictness else 8`; `off_brief` makes every critic-reported proposition/`delivers` failure blocking.
- `creative_agent/concept_guard.py`: `concept_issues(..., max_text_concepts=1 if "text_problem" in strictness else 2)`; `ensure_trend_and_product` appends "The product is large and in the foreground." when `product_not_visible` is set, and always appends the motif when `trend_unclear` is set.
- `creative_agent/agent.py`: `copy_gate` / `concept_gate` read `state.get("rating_strictness")`.
- Tests: `tests/test_copy_gate.py`, `tests/test_concept_guard.py`.

**Step 1: Failing tests** (one per flag), e.g.:

```python
def test_weak_cta_flag_tightens_cta_limit():
    copy = {
        **GOOD_COPY,
        "call_to_action": "Find your local dealer and play one today",
    }  # 8 words
    assert gate_copies([copy], target_product="SE CE24", strictness=[]) == {}
    issues = gate_copies([copy], target_product="SE CE24", strictness=["weak_cta"])
    assert "call to action" in issues["1"][0].text.lower()


def test_text_problem_flag_caps_text_concepts_at_one():
    assert (
        len(concept_issues(TWO_TEXT_CONCEPTS, COPIES, strictness=["text_problem"])) == 1
    )
```

**Steps 2–5:** run them (fail), implement (keyword-only `strictness: Sequence[str] = ()` params, default behaviour unchanged), run (pass), commit `feat(creative_agent): rating strictness tightens copy and concept gates`.

### Task C2: Strictness in image QA and render prompts

**Files:** `creative_agent/image_qa.py` (the instruction says "prominent" for product/motif when the flag is set; `qa_failed_rules` unchanged; pass `strictness` into `inspect_image`), `creative_agent/image_tools.py` (the `unwanted_logo` flag appends a no-other-logos line to the render prompt; pass `strictness` through). Tests: `tests/test_image_qa.py`, `tests/test_image_reference.py`.

**Steps:**
1. **Failing tests:** the QA instruction text contains "prominent" only with the flag; the render contents contain the no-logo line only with `unwanted_logo`.
2. **Run them.** Expected: FAIL.
3. **Implement.**
4. **Run them.** Expected: PASS.
5. **Commit:** `feat(creative_agent): rating strictness in image QA and render prompts`

### Task C3: Transparency (eval report + UI)

**Files:**
- `creative_eval/schemas.py`: report `learning_used: bool = False`, `learning_flags: list[str] = []`.
- `creative_eval/agent.py`: set them from `rating_signals_applied`.
- Frontend: a run-page outputs tile and results-page summary line ("Learned from 23 team ratings: preferred Candid 35mm film photo; stricter checks: product prominence, 1 text concept"), a new `frontend/src/components/learning-summary.tsx`.
- `runserver/calibration.py` / `scripts/eval_calibration.py`: an optional split by `learning_used`; can be deferred, document if so.
- Tests: `tests/test_creative_eval.py`, new `learning-summary.test.tsx`.

**Steps:**
1. **Failing tests:** report fields set; UI renders the line only when `rating_signals_applied.applied`, shows "Not enough ratings yet (3 of 8)" when below the minimum, and nothing when off.
2. **Run them.** Expected: FAIL.
3. **Implement.**
4. **Run them.** Expected: PASS.
5. **Commit:** `feat: show what rating-driven learning applied to a run`

### Task C4: Docs

`CLAUDE.md` (learning step, knobs, state keys `learn_from_ratings` / `rating_signals` / `rating_strictness` / `rating_signals_applied`, trust model), `GEMINI.md`, `docs/notes/judge-calibration.md` (learned runs are tagged; calibrate on non-learned runs first), `deployment/README.md` (migration + knobs), `tests/README.md`. Commit `docs: rating-driven learning`.

**PR C gate:** as above; merge only after CI passes; deploy the engines + api + web.

---

## Verification

1. **Unit / graph tests:** each task's tests pass, plus `tests/test_creative_agent_graph.py` graph runs:
   - toggle off → no ratings query, behaviour identical to today;
   - toggle on with enough fake ratings → `rating_signals` reaches the brief-writer prompt, the shortlist excludes and prefers families, the gates tighten;
   - below the minimum → nothing applied, the status is recorded.
2. **Injection guard:** a rating whose note says "ignore previous instructions…" never appears in any prompt. Notes aren't queried at all; the test asserts the query's column list.
3. **Live** (after deploy):
   - rate about 10 creatives for one brand with fail chips (e.g. 4× "Product hard to see");
   - start a creative run for that brand with the toggle **on**, and confirm the run page shows "Learned from N ratings…", the brief and art direction reflect it, the shortlist avoids the low-rated family, the concept guard appends the product-prominence line, and the eval report has `learning_used: true`;
   - run the same brief with the toggle **off** and confirm none of it applies.
4. **Kill switch:** redeploy with `RATING_LEARNING_ENABLED=false` (or set it on the api) and confirm the toggle has no effect.
5. **CI:** every PR merges only after all GitHub checks pass. Migrations (PR A's ALTER) run before the api deploy.
