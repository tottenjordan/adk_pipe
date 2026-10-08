# Judge calibration: human ratings vs the LLM judge

*2026-10-07, branch `feat/creative-ratings`.*

`creative_eval` scores every ad copy and visual with an LLM judge
(`gemini-3.1-pro-preview`, passing threshold 0.7). Its pass rate is only useful if
it agrees with people. The results page now collects human ratings so that agreement
can be measured instead of assumed.

## What is collected

In the results page's proof-detail dialog, **Your rating** has one control for the
ad copy and one for the visual: a pass/fail verdict (required), an optional 1–5 score
and an optional note. Each save upserts one row in BigQuery `creative_ratings`
(`runserver/ratings.py`; schema in deployment/README.md → Creative ratings), keyed
per (run, creative, user), so re-rating overwrites. The row also snapshots the judge's
verdict for the same creative: `judge_overall`, `judge_passed`, `judge_gates_passed`
(the judge's binary eval gates, when the report has them) and `judge_model` (plus `judge_source`, see the limitation below).

## Protocol

1. **Rate blind first.** Decide pass/fail before reading the judge's scores and
   reasoning (keep "Show reasoning" closed). Use the same bar the judge is meant to
   apply: would you ship this creative for this brief?
2. **Aim for about 50 ratings across at least 5 runs**, mixing brands and trends. Rate
   both the ad copy and the visual of each proof; they are reported separately
   (`by_kind`).
3. **Add a score when you can** (1 = unusable, 3 = acceptable, 5 = excellent). Spearman's
   rho against the judge's overall score needs at least 5 paired scores.
4. **Use notes for the reason**, especially when you disagree with the judge. They are
   the input for prompt or rubric fixes.
5. **Read the result.** `/runs` shows "Judge agreement" once 20 ratings are paired with a
   judge verdict (before that, how many more to rate). For the full report:

   ```bash
   set -a && source .env && set +a
   uv run python scripts/eval_calibration.py                    # everyone's ratings
   uv run python scripts/eval_calibration.py --user you@example.com
   uv run python scripts/eval_calibration.py --csv export.csv --json
   ```

## Known limitation: seedable judge fields

Session state is client-seedable (createSession `initialState` passes through the
proxy), so a user could plant a fake `creative_evaluation_report` and skew the judge
side of their own ratings. Mitigations:

- The api reads the judge verdict from the run's own GCS report first, and only from
  `gs://$GOOGLE_CLOUD_STORAGE_BUCKET/.../creative_eval_report.json` (≤ 5 MB); any other
  `eval_report_gcs_uri` is ignored. Each row records where its judge fields came from in
  `judge_source` (`gcs` | `state` | `none`).
- `scripts/eval_calibration.py` counts only `judge_source = 'gcs'` rows by default
  (`--include-all` adds the rest), so an all-users report can't be skewed through state.
- Still possible: pointing `eval_report_gcs_uri` at another run's real report in the
  same bucket, or rating dishonestly. The per-user `/runs` line covers only your own
  ratings, so it can only mislead yourself.

## Learned runs (rating-driven learning)

A run whose user ticked "Learn from past ratings for this brand" can be steered by
earlier ratings: a guidance note in the brief and creative prompts, style steering,
and stricter deterministic checks (`rating_strictness`). Such runs are tagged in
their eval report: `learning_used: true` plus `learning_flags` (`guidance`, `styles`
and each strictness flag, e.g. `product_not_visible`). Runs without learning, or
where it wasn't applied (not enough ratings, ratings unavailable), have
`learning_used: false`.

**Calibrate on non-learned runs first.** Learned runs are shaped by the same human
verdicts you're comparing against, so agreement on them is partly circular: the
pipeline was nudged towards what raters already liked, and the stricter checks
remove the failure modes raters flagged. Establish the judge's agreement on
`learning_used: false` runs, then compare learned runs separately.

The calibration report doesn't split by `learning_used` yet (deferred): rating rows
snapshot the judge verdict but not the learning tag, so the split needs a
`creative_ratings` column (or a join to the run's GCS report) first. Until then,
filter by session: the learning status is on the run's results page ("Learning from
ratings") and in its `creative_eval_report.json`.

## Reading the numbers

- **Agreement** is the share of creatives where the judge and the human gave the same
  verdict. It looks good whenever most creatives pass, so read it next to kappa.
- **Cohen's kappa** corrects agreement for chance: about 0.6 or more is substantial,
  0.4–0.6 moderate, under 0.2 means the judge's verdict tells you little. Kappa is
  reported as "not defined" when either side gave every creative the same verdict.
  Rate some weak creatives too, otherwise it can't be computed.
- **Spearman's rho** (judge overall score vs human score) shows whether the judge at
  least ranks creatives the way people do, even if its threshold is off.
- Since the eval gates (#275), `judge_passed` means score ≥ 0.7 **and** every blocking
  gate passed. `judge_gates_passed` (gates only; recorded only for reports that carry
  gates) is reported separately. If the gates agree with humans better than the
  overall verdict, the binary checks are carrying the signal.

If kappa stays low after ~50 ratings, look at the disagreements (notes plus the judge's
reasoning) before changing the threshold. A shifted threshold fixes a bias, but not a
rubric that measures the wrong thing.
