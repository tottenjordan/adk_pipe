# Advanced traffic controls: tunable synthetic scenarios with a live preview

> **Status:** COMPLETE (2026-10-05). #235 (bandit core + traffic job), #236 (api + `bandit_experiments.scenario_overrides`), #237 (deploy-panel tuning + live preview + custom badge) merged and rolled out: BigQuery column added (prod + eval), CPR serving + traffic job images `b24936b`, api `00143-qec`, web `00036-bz6`.

## Context
Right now the synthetic traffic behind a bandit experiment can only be steered with presets: scenario, click-rate mode and reward mode. Everything that actually decides how hard the problem is lives in fixed YAML (`bandit/scenarios/*.yaml`):
- who the readers are (segment mix);
- how far apart the creatives are;
- how much the eval judge's scores predict real clicks;
- the drift point and the noise.

The user wants to tune these from the results-page Deploy panel. **Decisions confirmed (2026-10-03):**
- **All four knobs:** audience mix, gap between creatives, judge reliability, and drift timing + noise.
- **Collapsed "Advanced" section:** presets fill the sliders, with a **live preview** of the expected click rate per creative × segment before deploying.
- **Provenance:** the experiment page shows a **"(custom)" badge** and an expandable list of the settings, and insights say when the judge was set to mislead.

### What exploration found (why the change crosses several layers)
- **`experiment.json` carries only the scenario name.** The traffic job (`bandit_traffic/traffic.py:137`, `simulate.build_environment(cfg)`) and the predictor reload the YAML by name.
- **`load_experiment_config` is strict** (`bandit/config.py:139-174`, `_strict_kwargs`). Today's CPR and traffic images would therefore **reject** a config with a new key.
- **The override hooks already exist but nothing in production uses them:**
  - `build_true_model(..., scenario=)` and `simulate.build_environment(cfg, *, scenario=)`;
  - `with_segment_mix(sc, weights)` (`config.py:366`);
  - the CLI's `dataclasses.replace` overrides (`bandit/cli.py:60-76`).
- **`bandit/` has no bounds checks** on `judge_wrong`, `lift_pp`, `rank_ctrs`, `noise_sd` or `drift.at_frac`. A `rank_ctrs` value outside (0, 1) produces a NaN logit.
- **The api body drops unknown fields.** `_CreateBody` (`runserver/experiments.py:803`) uses pydantic's default `extra="ignore"`. runserver never imports `bandit/`; it duplicates constants under parity tests (`SCENARIO_TARGET_CTR` → `test_api_noise_var_parity_with_bandit`).
- **The BigQuery table has nowhere suitable to store overrides.** `bandit_experiments` has no such column (`progress` is overwritten by the job), so a new column plus a migration is needed.
- **The seed is unaffected.** It comes from `crc32(scenario name)` (`simulate.scenario_key`), so a tuned experiment keeps the same random draws, which suits A/B comparisons.
- **noise_var calibration doesn't change.** None of the knobs touch `target_ctr`.

---

## Override schema (new contracts §9; binding for all layers)
`experiment.json` gains one optional key. It is written **only when at least one override is set**, so default experiments stay compatible with the images already deployed:
```jsonc
"scenario_overrides": {            // every field optional
  "segment_mix":   [0.6, 0.2, 0.2], // length = the scenario's segment count (clear_winner/drift 3, segment_winners 4); each in [0.05, 1], renormalised to sum 1
  "gap_scale":     1.5,             // [0.25, 2.0]. segment_winners: lift_pp × g. rank_ctrs: each ctr = mid + g·(ctr − mid) in logit space, so the result stays in (0,1)
  "judge_wrong":   0.8,             // [0, 1]. 0 = the judge is right, 0.5 = it carries no information, 1 = it is backwards
  "noise_scale":   1.0,             // [0, 2]. Multiplies noise_sd and theta_sd
  "drift_at_frac": 0.3              // [0.2, 0.8]. Only valid when the scenario is drift
}
```
REST uses camelCase: `scenarioOverrides {segmentMix, gapScale, judgeWrong, noiseScale, driftAtFrac}`. Validation failures return 400 with the reason `invalid_scenario_overrides`, plus a `detail.field`.

## PR A: bandit core + traffic job (`feat/bandit-scenario-overrides`)
- **`bandit/config.py`:**
  - Add the frozen dataclass `ScenarioOverrides`, with `validate_scenario_overrides(ov, scenario)` (bounds above; segment count; `drift_at_frac` only for drift).
  - Add `apply_scenario_overrides(sc, ov) -> ScenarioConfig`. It reuses `with_segment_mix`, scales `lift_pp` or applies a logit-space spread to `rank_ctrs`, and replaces `judge_wrong`, `noise_sd`/`theta_sd` and `drift.at_frac`.
  - Add `ExperimentConfig.scenario_overrides: ScenarioOverrides | None = None`.
  - Add `resolve_scenario(cfg)`, which returns `apply(load_scenario(cfg.scenario), cfg.scenario_overrides)`.
  - Make `load_experiment_config` parse the nested dict strictly, and make `experiment_config_to_dict` omit the key when it is None, so the round trip is unchanged.
- **`bandit_traffic/traffic.py:137`:** `simulate.build_environment(cfg, scenario=resolve_scenario(cfg))`. Baselines share `self.env`, so they get it too.
- **Predictor:** needs no logic change, because it reads only `target_ctr` and `dwell_base_s`. The strict loader just has to accept the field.
- **`bandit/cli.py`:** move the existing override flags onto `apply_scenario_overrides`. Add `--gap-scale`, `--noise-scale` and `--drift-at`, and give the existing flags the same bounds.
- **Tests:**
  - `test_bandit_config.py`: round trip with and without overrides; every bound fails; wrong segment count; `drift_at_frac` on a non-drift scenario.
  - `test_bandit_environment.py`:
    - `segment_mix` changes the sampled segment frequencies;
    - `gap_scale` > 1 widens and < 1 narrows the oracle gap;
    - `judge_wrong=1` reverses the ranking (the existing test, now driven through overrides);
    - `noise_scale=0` makes the model deterministic for a given key;
    - `drift_at_frac` moves the flip point;
    - calibration still hits `target_ctr`.
  - `test_bandit_traffic.py`: an in-process run with overrides uses the tuned environment.
  - `test_bandit_predictor.py`: loads a config that has overrides.

## PR B: api (`feat/experiments-scenario-overrides-api`)
- **`runserver/experiments.py`:**
  - `_CreateBody.scenarioOverrides: dict | None`.
  - Add `validate_scenario_overrides(scenario, ov)`, which uses duplicated constants `SCENARIO_SEGMENTS = {clear_winner: 3, segment_winners: 4, drift: 3}` and `OVERRIDE_BOUNDS`. It never imports `bandit`.
  - `build_experiment_config(..., scenario_overrides=)` writes snake_case, and only when the overrides are non-empty.
  - The store row gets `scenario_overrides`, and `to_summary` exposes `scenarioOverrides`.
- **Parity tests:**
  - `SCENARIO_SEGMENTS` and `OVERRIDE_BOUNDS` against `bandit` (`load_scenario` segment counts plus the bandit bounds constants), in the same style as `test_api_noise_var_parity_with_bandit`;
  - an api-built config with overrides passes the strict `bandit.load_experiment_config` (the dev-group test can import both).
- **BigQuery:**
  - add `scenario_overrides STRING` (JSON) to the DDL in `deployment/create_bq_tables.sh`;
  - add it to `EXPERIMENT_COLUMN_TYPES` and `JSON_COLUMNS` (`runserver/experiments_store.py:36-58`);
  - document the `ALTER TABLE … ADD COLUMN IF NOT EXISTS` migration in `deployment/README.md` (prod and `trend_trawler_eval`).
- **Contracts:** new §9, with §1 / §3 / §5 updated. Update `test_column_map_matches_contract_and_ddl`, `test_build_experiment_config_matches_section_1_shape` and `test_create_validation_errors`.

## PR C: frontend (`feat/deploy-advanced-controls`), with the `frontend-design` skill
- **Scenario presets in TS:**
  - generate `frontend/src/lib/scenario-presets.generated.json` from the YAML (segments with names, weights and marginals; `arm_effect`; `rank_ctrs`; `lift_pp`; κ/λ/η; `target_ctr`; drift; base marginals; feature spec);
  - the generator lives at `scripts/gen_scenario_presets.py`, run with `uv run`;
  - `tests/test_scenario_presets_sync.py` fails if the JSON is out of date.
- **Preview maths in `src/lib/scenario-preview.ts`** (pure, unit-tested):
  - a **noise-free** TS port of `environment.build_true_model`'s deterministic structure (arm base from the judge scores with `judge_wrong`, segment affinity, segment-winner lift, θ interactions, α bisection to `target_ctr`, `rank_ctrs` spread);
  - expected click rate per segment is averaged over a fixed seeded Monte Carlo sample of contexts drawn from the segment marginals;
  - output: the click-rate matrix (creative × segment), the oracle creative per segment, and the overall gap.
  - **Parity golden.** `tests/test_scenario_preview_golden.py` (dev group, JAX) computes the same matrix with `bandit` (`noise_scale=0`, large Monte Carlo sample) for a fixed set of arm scores × 3 scenarios × a few override combinations. It writes and compares `frontend/src/__tests__/fixtures/scenario-preview-golden.json`. The Vitest test asserts the TS matrix is within ±0.2 pp, and that the oracle creatives are identical.
- **Deploy panel** (`src/app/results/[sessionId]/deploy-panel.tsx`): add a collapsed **"Advanced: tune the simulated readers"** section below the current grid, built on the existing `ui/collapsible`.
  - **Audience mix:** one slider per segment, with the segment name and its live percentage; the others rebalance proportionally.
  - **Gap between creatives:** "Subtle ↔ Obvious", 0.25–2×.
  - **Judge reliability:** "Judge is right · No information · Judge is backwards", mapped to `judge_wrong` 0–1, with a plain-language note at the extremes.
  - **Noise:** 0–2×.
  - **Drift point:** shown only for drift, 20–80% of the run.
  - "Reset to preset"; changing the scenario reloads the preset values.
  - A slider primitive from `@base-ui/react` (already a dependency), wrapped as `components/ui/slider.tsx`, with keyboard and aria value text. No new npm dependency.
  - **Live preview** inside the section: a compact creative × segment grid of expected click rates in the style of the Overview segment grid (creative colours, "Best" outline), plus one reading sentence, e.g. "Mobile scrollers will prefer X by 1.8 pts; the judge favours Y, so the bandit has to overrule it." It is labelled "Expected rates before random variation".
  - `selectionToPayload` adds `scenarioOverrides` only for values that differ from the preset.
- **Experiment page** (`src/app/experiments/[experimentId]/page.tsx:230`):
  - "(custom)" after the scenario label, plus an InfoTip or disclosure listing the overrides in plain language;
  - the list page (`experiments/page.tsx:119`) gets the same badge;
  - `experiment-insights.ts` adds a note when `judgeWrong > 0.5` ("The eval judge was set to mislead; …") and when the mix is skewed.
- **Help:** add copy for every knob in `experiment-help.ts` (the existing completeness test covers it).
- **Tests:**
  - `scenario-preview.test.ts` (golden parity, rebalancing maths, `rank_ctrs` stay inside (0,1));
  - `deploy-selection.test.ts` (overrides omitted at preset values, camelCase mapping);
  - `experiment-insights` notes.
- **Screenshots:** `10-deploy-panel.png` with Advanced open, and a header with the custom badge.

## Rollout (after merge, in this order)
1. **BigQuery migration:** `ALTER TABLE bandit_experiments ADD COLUMN IF NOT EXISTS scenario_overrides STRING`, on prod and the eval dataset.
2. **Images:** rebuild and push the **CPR serving image** (`deployment/bandit/build_image.py --push`) and the **traffic job image** (`cloudbuild.traffic.yaml`), then `deploy_traffic_job.sh`.
3. **api:** set the api env `BANDIT_SERVING_IMAGE` to the new tag (`--update-env-vars`, so the rest of the env is kept), and deploy the api with `--no-traffic --tag verify`.
   - Verify: create a fake-mode config locally; on the verify tag, run list-apps and fetch an experiment summary.
   - Then pin to 100% and move the `prev` tag.
4. **web:** deploy, then pin (the web is pinned now too) and move `prev`.

Steps 2 and 3 must happen before any custom experiment is created; old images reject the new key. Default experiments keep working throughout, because the key is omitted.

## Verification
- **Python:** `ruff format --check`, `ruff check`, `ty check`, and `pytest -n 4` (`GOOGLE_CLOUD_PROJECT=test-project`). `requirements.txt` stays JAX-free.
- **Offline:** `uv run python -m bandit.cli simulate --scenario segment_winners --judge-wrong 1 --gap-scale 0.5 --episodes 5`. LinTS should still find the oracle creatives; non-contextual TS should do worse than with defaults.
- **Local stack:** api with `BANDIT_DEPLOY_MODE=fake` + `npm run dev`. Open the Advanced section, move the sliders, and check the preview updates instantly. Deploy, and confirm `experiment.json` (fake store) has snake_case overrides and the experiment page shows "(custom)".
- **In-process traffic:** `python -m bandit_traffic.main --in-process --dry-run --config <json with overrides>` runs with the tuned environment.
- **Frontend:** `npm run lint && npm test && npm run build`.
- **Live** (after rollout): deploy 3 PRS creatives as segment_winners with judge reliability = backwards and a skewed mix, run 5 episodes, check the charts and the custom badge, then Stop. Confirm the endpoint is deleted.

## Conventions
- **Git:** one branch and one PR per section; squash-merge after CI.
  - **Never add Co-Authored-By or any AI attribution.**
  - Never stage `.agents/`, `.claude/`, `skills-lock.json` or `notebook-examples/`.
- **Execution:** PR A first. PR B can run in parallel against the §9 contract. PR C runs in parallel, but its golden test needs PR A's `apply_scenario_overrides`, so C rebases onto A before merging.
- **Review:** screenshots go to the user before anything merges or deploys.
