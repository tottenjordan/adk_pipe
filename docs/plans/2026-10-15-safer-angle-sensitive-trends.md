# Safer Angle for Sensitive Trends Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: `subagent-driven-development`. Run PRs A1, A2, J and F **in parallel**: their files don't overlap and they share only the state contract below. Then do the deploy and live verification (Task 5). First step: copy this plan to `docs/plans/2026-10-15-safer-angle-sensitive-trends.md` and commit it in PR A1. Review each PR with `code-review-and-quality`: tests first, mutation evidence, five axes, severity labels. Merge only after CI is green.

**Goal:** when a user deliberately picks a trend flagged *sensitive* and opts in, research, the brief, ad copy and visuals look for a respectful, positive angle on the trend's subject. Example: Tupac Shakur's legacy of chasing your music dreams, for a Hohner Clavinet D7. The sensitive event itself (the murder or the trial) is never referenced, and nothing implies endorsement.

**Architecture:**
- **Activation:** a per-run opt-in, `safer_angle_opt_in`. It is active only when trend_scout flagged the trend (`trend_scout_context.sensitive`).
- **Rule texts:** at state init, one module derives `safer_angle_active` and four brace-free rule texts, for research, brief, copy and visual. Each is read as an optional `{…?}` token at the end of the relevant prompts, so runs without it get **byte-identical** instructions. This is the `timely_rules` / `person_casting_rules` pattern.
- **Brief:** it gains `trend_bridge.safer_angle`.
- **Deterministic guards:** an endorsement-phrase lexicon in the copy gate, the trend tie required again, and a likeness check in image QA.
- **Judge:** a new blocking gate, `safer_angle_respected`.
- **Frontend:** a checkbox in the existing amber alert, and a results-page "Names a real person" review chip.

**Tech stack:** ADK 2.10 agents (`creative_agent`, `interactive_creative`), `creative_eval` judge, Next.js 16 frontend, pytest + Vitest.

---

## Context

**Why:**
- **What happens today:** a sensitive trend (death, tragedy, crime, disaster, a real person's serious illness) is handled only by "never reference it". The UI warns that "Creatives will never reference the event, so they may connect only loosely; many brands avoid trends like this."
- **What users want:** users pick a flagged trend only when they think there's still a good reason. The user's example: "tupac shakur" is trending because someone was sentenced for his murder. Nobody should make ads about a murder trial, but a respectful nod to Tupac's legacy (following your music dreams, a tribute played on a Clavinet D7) could be fine.

**Current gaps (from exploration):**
- **Two unconnected sensitivity signals:**
  - **A:** trend_scout's `trend_scout_context.sensitive` / `sensitive_reason`. No creative code reads it; only the brief writer and research planner see it, as raw JSON.
  - **B:** the creative run's own `trend_situation.phase == "sensitive"`, which only the copy gate's trend-tie skip and the brief writer's rule 13 read.
- **No sensitive checks downstream:** no deterministic check requires a sensitive brief to list `risks` or use `light_touch`, and the judge never learns the trend was sensitive.

**Decisions (user, 2026-10-15):**
1. **Naming:** the real person may be named in a respectful tribute, never implying endorsement. Creatives that name them get a visible review note.
2. **Activation:** opt-in checkbox, off by default, shown only for a flagged trend.
3. **No angle found:** always try. The fallback happens only when the safety floor blocks every attempt (below).
4. **Visuals:** they follow the safer angle too.

**Safety floor (non-negotiable, enforced in prompts, code, image QA and the judge):**
- Never reference the sensitive event, its victims, injuries, damage, trial, verdict, sentence or cause of death.
- No jokes, puns or wordplay about the harm. No "RIP" or "rest in peace" sales hooks.
- Never imply endorsement, partnership, licensing, sponsorship or approval by the person or their estate. Never say "official", "signature model", "as used by" or "his/her favourite". Never imply the person used or owned the product.
- Never put words in the person's mouth. No quotes or lyrics.
- No likeness of the real person in images. No estate-owned imagery, logos or album art.
- No urgency or discount pushes tied to the moment.
- If a brief still breaks the floor after its revision round, it falls back to today's behaviour (event never referenced, light touch) with the residual noted.

## State contract (fixed now so all four PRs can run in parallel)

| Key | Type | Written by | Read by |
|---|---|---|---|
| `safer_angle_opt_in` | bool; seeded by the frontend initialState; `setdefault(False)`; only exactly `True` counts | F, A1 | A1 |
| `trend_sensitive_hint` | `"yes"` / `""`: does `trend_scout_context` JSON have `sensitive: true`? | A1 (state init) | A2 (copy-gate skip) |
| `safer_angle_active` | `"yes"` / `""` = opt-in AND hint | A1 (state init) | A2, J, F (display) |
| `safer_angle_sensitive_reason` | str (capped): the hint's `sensitive_reason` | A1 | J (brief block), F |
| `safer_angle_research_rules` / `_brief_rules` / `_copy_rules` / `_visual_rules` | brace-free str, `""` when inactive | A1 (texts in `creative_agent/safer_angle.py`) | A1 prompts / A2 prompts |
| `trend_bridge.safer_angle` | str, default `""`: one line naming the positive association | A1 (schema + brief writer) | A2 prompts (through the brief), J, F |
| `safer_angle_named_copies` | list of ad copy ids whose text names the trend subject | A2 (copy gate) | F (chip), J (context) |
| `generated_images[c].qa.public_figure_likeness` | bool, set only when active | A2 (image QA) | F (optional), J |

**Missing-key behaviour:** a token missing from state resolves to `""` (`{key?}`), and code readers use `.get(...) == "yes"`. Each PR is therefore inert, and safe to merge alone, until A1 seeds the keys.

## PR A1: activation, research and brief (backend, ~75 min)

**Files:**
- **Create:** `creative_agent/safer_angle.py`, `tests/test_safer_angle.py`.
- **Modify:**
  - `creative_agent/callbacks.py` (seed keys)
  - `creative_agent/schemas.py` (`TrendBridge.safer_angle: str = ""`)
  - `creative_agent/prompts.py` (brief writer: `{safer_angle_brief_rules?}` at the end of the last instruction line)
  - `creative_agent/sub_agents/trend_researcher/agent.py` (planner, searcher and synthesizer: `{safer_angle_research_rules?}`)
  - `creative_agent/brief_check.py`
  - `creative_agent/brief_render.py` ("Safer angle" in the Trend fit block)
  - CLAUDE.md (one Configuration bullet + state keys)

**`safer_angle.py`** (pure, fully tested):
- `parse_hint(raw) -> (sensitive: bool, reason: str)`: lenient JSON parse of `trend_scout_context`. Malformed → `(False, "")`.
- `derive_state(state) -> dict`: the contract keys above.
- Rule-text constants:
  - **Research** (planner, searcher, synthesizer): "This trend is sensitive (<reason>). The user asked for a respectful angle. Research the subject's positive legacy, cultural contributions and the values audiences admire (e.g. an artist's music, message, influence), not the event. Add a report section 'Safer angle candidates': 2-3 positive associations, each with why it is respectful and a source. Never research or describe the event's details beyond naming it as a risk."
  - **Brief:** replaces rule 13's "never referenced" for this run. "Set `trend_bridge.safer_angle` to one positive association from the research (≤ 25 words). Build the angles on it. The subject may be named only as a respectful tribute or inspiration. List the event's harms in `trend_bridge.risks` and short terms in `avoid`." Plus the safety-floor lines.
  - **Copy** and **visual:** the safety floor plus how to use `trend_bridge.safer_angle`.
    - Copy: use it to tie at least half of the copies to the trend, naming the subject only in tribute framing.
    - Visual: motifs from the safer angle (era, genre, instruments, settings), never a likeness or estate imagery.

**brief_check** (keyword `safer_angle_active=`):
- **When active:**
  - `safer_angle` must be non-empty.
  - `risks` must be non-empty.
  - `safer_angle` must share no word with the risks or the `avoid` terms (a cheap event-leak guard).
  - `safer_angle` must contain no endorsement phrase (reuse A2's lexicon? No: it's a separate PR, so keep a tiny local check on "official / endorsed / sponsored / partner" and let A2's module be the canonical one later. Note this in the PR).
  - Any failure routes to the existing bounded `brief_reviser_failsoft` round, with residuals going to `creative_brief__issues`.
  - If `safer_angle` is still empty after the round, prompts read an empty value and the run behaves as today. That is the fallback.
- **When inactive:** a non-empty `safer_angle` is flagged (stray). The brief gate clears it.

**Tests (TDD):**
- `parse_hint` table.
- `derive_state`: opt-in without a flag gives inactive; flag without opt-in gives inactive; both give active; `True` vs `"true"`.
- Rule texts are brace-free.
- Every brief and research instruction is byte-identical when inactive: pin the sha256 without the token, like `tests/test_timely_rules.py`.
- brief_check: every new rule, each with a mutation check.
- Render shows "Safer angle".
- `interactive_creative` brief edit (`runserver.async_runs.merge_brief_edit`) accepts the new field and still accepts old briefs without it.

## PR A2: copy, visuals and image QA (backend, ~75 min)

**Files:**
- **Create:** `creative_agent/endorsement.py`, `tests/test_endorsement.py`, `tests/test_safer_angle_guards.py`.
- **Modify:**
  - `creative_agent/prompts.py`: copy drafter, critic and reviser get `{safer_angle_copy_rules?}`; the art director and visual drafter, critic, finalizer and fixer get `{safer_angle_visual_rules?}`, after `{rating_limits?}`.
  - `interactive_creative` reviser prompt (same token).
  - `creative_agent/copy_gate.py` and the `agent.py` gate wiring.
  - `creative_agent/image_qa.py` and its rule list.
  - CLAUDE.md bullet.

**Overlap with A1:** A1 also edits `prompts.py`, in different constants. Whoever merges second rebases. That's expected and small.

**`endorsement.py`:**
- Built like `claims.py`: whole-word, fold-insensitive, space and hyphen interchangeable.
- `endorsement_terms(text)` covers: official/officially, endorse/endorsed/endorsement, sponsor/sponsored, partner/partnered/partnership, approved by, licensed, authorised/authorized, estate, signature model/edition, as used by, his/her/their favourite/favorite, rest in peace, RIP, would have wanted, in his/her words.

**Copy gate** (keyword `safer_angle_active=`):
- **When active:**
  - Flag any copy field with an endorsement term ("implies endorsement or speaks for the real person (…): remove it"). This is deterministic and goes to the reviser.
  - **Re-enable the trend-tie check**, which is skipped today for a sensitive moment, because connecting is the point. Avoid and risk terms still never count as a tie.
  - Record `safer_angle_named_copies`: copies whose text contains the trend phrase or its distinctive words (reuse `trend_tie.py` matching).
- **When inactive:** the trend-tie skip also applies when `trend_sensitive_hint == "yes"`, not only for `phase == "sensitive"`. This closes the two-signal gap for non-opt-in runs and is a small deliberate behaviour change; note it in the PR.

**Image QA:**
- When active, add one structured question: "Does the image show a recognisable likeness of a real public figure (not the cast person)?". Field: `ImageQAResult.public_figure_likeness: bool = False`.
- Rule "real public figure likeness" is **critical**, so it weighs first, like `unsafe`. Correction text: "show the theme through instruments, era and setting, never the real person's likeness."
- `tests/test_render_parity.py` must stay green; requests are unchanged when inactive.

**Tests:**
- The endorsement lexicon table, including false positives ("partner up with your bandmate"? Decide and pin the documented behaviour).
- Copy gate active and inactive, each with mutation checks.
- The hint-based skip.
- Named-copies detection.
- Byte-identical copy and visual instructions when inactive (sha pins).
- The image QA rule, only when active.

## PR J: judge gate (creative_eval + mirrored labels, ~45 min)

**Files:**
- `creative_eval/dimensions.py`: gate `safer_angle_respected` (label "Respectful reference") on both ad copy and visual; `JUDGE_VERSION = "2026-10-15"`.
- `creative_eval/prompts.py`
- `creative_eval/evaluate.py` (`normalize_gates`)
- `creative_eval/brief.py`: the brief block adds "Safer angle: …" and "Sensitive event (never referenced): <reason>" when active.
- `frontend/src/lib/eval-dimensions.ts` (mirror)
- The drift test
- Test pins: `tests/test_eval_timely_gate.py`, `tests/test_eval_label_text.py`
- CLAUDE.md Evaluation paragraph

**Gate:**
- **Judged only when** `safer_angle_active == "yes"`. Otherwise it passes in code with note "no safer angle", whatever the judge says.
- **Passes when:**
  - the creative never references the sensitive event, victims or harm;
  - any naming of the real person is a respectful tribute or inspiration with no endorsement, partnership, use of the product or quoted words;
  - for visuals: no likeness of the real person and no estate-owned imagery.
- **Omitted for an active run:** fails with note "not checked" plus a report warning (the `timely_claim_supported` pattern).
- **`trend_connected` wording:** for a safer-angle run, a clear tie through the brief's safer angle counts.

**Tests:** normalize matrix (active/inactive × judged/omitted), brief block lines, label drift, version pins. Mutation: the inactive pass-in-code and the omitted → fail paths.

## PR F: frontend (~60 min)

**Files:**
- `frontend/src/app/page.tsx` (alert + checkbox)
- `components/sensitive-chip.tsx` (advice text)
- `lib/types.ts` (`saferAngle?: boolean`)
- `lib/initial-state.ts`: `safer_angle_opt_in: true` only when checked **and** the context is sensitive.
- `lib/run-history.ts` (Duplicate brief restore)
- `lib/utils.ts` + the run and results pages' `CAMPAIGN_FIELD_DEFS` (metadata "Safer angle: on")
- New `components/safer-angle-line.tsx` on the run and results pages: "Safer angle: <trend_bridge.safer_angle>", plus the reason. Nothing when inactive.
- The results proof grid and proof detail: a mark-pending **"Names a real person"** chip for creatives whose paired copy id is in `safer_angle_named_copies`, with the detail note "Respectful tribute only. Check name and likeness rights before using this live." (`lib/eval-matching.ts` helper).
- The interactive checkpoint-1 brief edit form shows and edits `trend_bridge.safer_angle` (`lib/brief-edit.ts`).
- Tests (Vitest) and the CLAUDE.md Pages bullet.

**Checkbox:**
- Inside the amber alert: "Look for a respectful angle". Help text: "Builds on the positive side of the trend, like a person's legacy. The event itself is never referenced, and a named person appears only as a respectful tribute, never as an endorsement."
- Off by default, and cleared when the trend is edited, along with the context.
- When it's checked, the advice line reads: "Creatives will look for a respectful angle; the event is never referenced."

**Tests:** checkbox visibility, clearing, and initialState seeding (only when sensitive); Duplicate restore; metadata; safer-angle line; chip and note; brief edit field.

## Task 5: deploy and live verification (after all four merge, ~45 min)

1. **Wait for the A/B:** don't deploy until the temperature A/B has finished and its analysis is done. Pro quota is shared, and the arm revisions run their own image, so they aren't affected either way.
2. **Deploy:**
   - Engines via Cloud Build (`_SMOKE=true`): creative and interactive only, since trend_scout is unchanged. Then `engine_handoff apply --update-worker`, which now moves the worker's traffic pin itself.
   - api (idle check, `--no-traffic`, pin traffic).
   - web (pin traffic; the share service redeploys with the web image digest).
3. **Live runs** (Hohner Clavinet D7, trend "tupac shakur" carried from a trend card, or a crafted `trendContext` with `sensitive: true`, `sensitive_reason: "death of a public figure"`):
   - **Opt-in off:** behaves as today; no safer angle; byte-identical prompts.
   - **Opt-in on:**
     - `trend_bridge.safer_angle` is set to a positive legacy line.
     - `risks` names the murder or trial.
     - Copies tie to Tupac's legacy, and any naming is tribute-only.
     - No endorsement terms, no event words, no likeness in the renders.
     - `safer_angle_respected` passes, or a failure is explained.
     - The "Names a real person" chip shows.
     - **Hurricane-type case:** a second opt-in run on a disaster-type flagged trend, to check the floor holds under "always try".
4. **Wrap-up:** update memory, and the CLAUDE.md Data Flow / Pages sections where needed.

## Verification

- **Per PR:**
  - Unit tests plus mutation checks.
  - The byte-identical sha pins prove that runs without the opt-in are unchanged, except for the deliberate hint-based copy-gate skip in A2.
  - Gates: ruff format and check, ty, and pytest in a clean env (`env -u GOOGLE_GENAI_USE_ENTERPRISE -u GOOGLE_GENAI_USE_VERTEXAI GOOGLE_CLOUD_PROJECT=test-project`).
  - Frontend: `npm run lint`, `npm test` and `npm run build` for F and J.
  - CI must be green before merge.
- **Integration:** after all four merge, run the full suite on `main` (pytest + Vitest).
- **Live:** Task 5's runs and checklist.

**Estimated wall clock:** about 75 min for the four parallel PRs, about 30 min of reviews and merges, then about 45 min to deploy and verify after the A/B. Roughly 2.5 h of active time.
