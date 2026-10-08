# Improving the creatives, how well they match the brief, and how the brief is built — research findings

> **Status (2026-10-08): implemented.** The recommendations below shipped in PRs #263–#280
> (plan: [docs/plans/2026-10-07-creative-quality.md](../plans/2026-10-07-creative-quality.md)).
> The audit table in §2 describes the pipeline *before* those PRs; for the current design see
> [CLAUDE.md → Agent Composition](../../CLAUDE.md#agent-composition).
>
> | Finding | Shipped in | Notes |
> |---|---|---|
> | F1 brand–trend fit | #264 (+ risk sections #263) | `trend_bridge` `fit_score` / `fit_mode` / bridge / motifs / risks in `CreativeBrief` |
> | F2 brief schema | #264 (brand research in the campaign planner #263) | `CreativeBrief` → `creative_brief` + compact `creative_brief_md` |
> | F3 brief critique | #264 (`brief_gate` every run); #280 (interactive checkpoint 1 reviews the editable brief) | the check is deterministic (`brief_check.py`); no extra LLM critic pass |
> | F4 deterministic inputs | #263 | campaign fields seeded via `createSession` initialState |
> | F5 check + revise on fail | #265 (copy gate), #267 (concept gate); false-positive fixes #268 | one bounded revision round each; eval never triggers regeneration |
> | F6 grounding in facts | #263 (`{brand}` / `{target_audience}` everywhere), #264 (brief block in the creative prompts) | |
> | F7 brand distinctive assets | #264 (`brand.distinctive_assets`, art direction), #267 (`brand_cue` per concept) | the style shortlist stays random (brand history excludes recent styles, #278) |
> | F8 angle diversity | #265 | 3–5 brief angles, drafter spreads copies across them with self-rated typicality (light Verbalized Sampling) |
> | F9 pixel-level image QA | #274 (final image part fix #266) | one vision check + ≤1 targeted re-render; inpainting path not built |
> | F10 Google image guidance | #273 | Subject→Action→Context→Composition→Style order, quoted text, up to 3 role-tagged reference images |
> | F11 judge as gate + calibration | #275 (binary gates vs advisory scores, judges rendered images), #277 (human ratings + Cohen's kappa) | judge stays `gemini-3.1-pro-preview` (no family swap); failed gates do not trigger regeneration |
> | F12 cross-run memory | #278 | `brand_history` note + style-shortlist exclusion; bandit click signals out of scope |
>
> Reliability fixes shipped alongside: #269/#271/#272 (`finalize_pipeline`, a single
> `creative_pipeline` call, root history trim) and #276/#279 (eval tool-use rubric).

*2026-10-07 · For review only: no code has been changed.*

**Method.**
- **Repo audit.** A code map of `creative_agent/`, `creative_eval/` and `interactive_creative/`, with the key claims re-checked by hand.
- **Deep-research workflow.** 5 search angles, 21 sources, 94 claims. 25 claims were checked by 3 independent votes each: 22 confirmed, 3 refuted.
- **Brief practice.** A follow-up search for agency and industry sources (IPA, WARC, BetterBriefs, Google, Ehrenberg-Bass), because the workflow found nothing verified on brief structure.

**Confidence labels:** **[H]** official or primary source, or confirmed directly in our code. **[M]** a single peer-reviewed or primary study, or several preprints that agree. **[L]** a single preprint, a vendor claim or a survey; use it as a direction to explore, not as proof.

---

## 1. Summary

Our pipeline is good at *producing* creatives. It is weak at *steering and checking* them against a brief, for three structural reasons:

1. **There is no brief you could check anything against.** The "strategic brief" is free Markdown, rewritten three times (synthesizers → `merge_planners` → `combined_report_composer`). It never names a proposition, an insight, reasons to believe, mandatories or a tone. So no later agent, critic or judge can ask "does this creative deliver *the brief*?"
2. **The brand is barely in the loop.** `{brand}` reaches only the art director, the concept drafter and the eval judge. It never reaches research, the ad copy drafter or critic, or the visual critic. Nobody researches the brand itself: its voice, positioning or distinctive assets. **[H, code]**
3. **Nothing feeds back.** `creative_eval` runs after rendering, judges *text prompts* rather than rendered pixels, never sees the brief, and is report-only. Failed creatives still ship. Rendered images are never checked by a vision model. **[H, code]**

The research points to five fixes, in priority order:

- **A. A structured brief**, with an explicit **brand–trend fit test** at the front.
- **B. Brief-grounded generation**, with a **check-and-revise-on-fail** loop.
- **C. Deliberate diversity** of *angles*, not only of visual styles.
- **D. Pixel-level image QA**, plus product-fidelity measures.
- **E. Narrowing the judge's role** to a compliance gate, calibrated against humans.

---

## 2. What we have today (code audit)

| Stage | Today | Gap |
|---|---|---|
| Inputs | Core fields (brand, audience, product, selling points, trend) arrive as a **chat message**; the root LLM must `memorize` each one (`frontend/src/app/page.tsx:161`, `creative_agent/prompts.py:647`). Visual-intent keys are seeded deterministically via initialState. | Core inputs depend on the LLM copying them faithfully. The message says `target_search_trend` but the state key is `target_search_trends`. |
| Campaign research | Planner (5 queries) → searcher → synthesizer: audience insights, competitive landscape, "Strategic Opportunities", gaps. Uses `gemini-3.5-flash`. | The planner never sees `brand`. No research into brand voice, positioning or past campaigns. |
| Trend research | Planner → searcher → synthesizer: overview, entities, "Marketing Opportunity". | **Risk Assessment is commented out** (`trend_researcher/agent.py`, and the composer's "Final Risk Assessment & Constraints" at `creative_agent/agent.py:167`). No score for how well the product fits the trend, and no way to say "weak fit, take a looser angle". |
| Brief | `merge_planners` writes "Strategic Brief" Markdown (Big Idea, fundamentals, 3 directives). `combined_report_composer` (Pro) writes the final report with "5 Actionable Creative Briefing Points". | Free text with no schema. The **critique/refinement pass runs only when research came back degraded**; a healthy run's brief is never critiqued. |
| Ad copy | Flash drafter (temperature 1.5) writes 10 copies across 6 fixed tones → flash critic picks 4 and adds CTAs. | One draft-then-pick pass, no scores, no rewriting. Drafter and critic never see `brand`, and the drafter sees `target_audience` only through the report. Nothing checks which selling points each copy covers. The CTA is never critiqued. |
| Visual concepts | Art director → drafter (4 families from a **random** `style_shortlist`) → critic → finalizer → `concept_guard` appends the motif or product if missing. | The critic gets no brand, audience, report or ad copy. The style shortlist ignores brand and tone. `concept_guard` checks strings, not pixels. |
| Rendering | `gemini-nano-banana-2.1`, 2K, one sample per concept, optional reference image (product, logo or style role). | No best-of-N, no vision-model QA, no re-render on failure. |
| Eval | `gemini-3.1-pro-preview`, pointwise 1–10 on 6 dimensions per creative type, pass at ≥ 0.7. | Judged against the 5 raw input strings, **not the brief**. Visuals are judged on the prompt, not the image. Report-only (`max_retries: 3` is unused). Same model family as the Pro producers. |
| Learning across runs | Eval rows go to BigQuery `creative_evals`. | Write-only: no reuse of past winners or failures. |
| Interactive mode | 3 checkpoints. | Feedback only flows forward: copy or research feedback never re-runs that stage. No checkpoint after rendering. |

---

## 3. Findings and recommendations

### 3.1 Building the brief

**F1. Test brand–trend fit, and allow "no".** **[M]** In a peer-reviewed study of real-time marketing, fit between the brand and the moment predicted engagement. For unplanned, reactive moments it was the *only* significant predictor (β≈0.68), ahead of how much consumers identify with the brand ([Santos et al. 2023, *Psychology & Marketing*](https://onlinelibrary.wiley.com/doi/full/10.1002/mar.21756); 3 brands, n=421, self-reported engagement). Our trends are reactive by definition.
- **Recommendation:** add a **brand–trend fit step** before drafting. Score the fit (for example 1–5) and name the *bridge*: which trait of the brand or product genuinely connects to which facet of the trend.
- **Fit modes:** choose one of `direct` (the product is part of the trend), `cultural` (shared audience value or mood) or `light-touch` (borrow the tone, don't force the product into the trend). Low fit should trigger `light-touch` rather than a forced tie-in.
- **Risk:** restore the commented-out risk/brand-safety section. Trends about real people already get special handling in the visual prompts.

**F2. Give the brief a schema.** **[H for which elements are canonical; L for the effect on LLM output]**
- **Canonical elements:** objective, audience plus insight, one key thought or proposition, supporting evidence (reasons to believe), tone, and mandatories.
  - The IPA client brief: "The key to effective briefing is to provide a simple insight that can be dramatised memorably", and "a single-minded and measurable objective is usually a pre-requisite for success" ([IPA/ISBA *The Client Brief*](https://www.acaweb.ca/en/wp-content/uploads/sites/2/2016/09/THE-CLIENT-BRIEF-FINAL-Eng-July-18-06.pdf)).
  - WARC's list matches; it adds brand values/tone and legal mandatories ([WARC](https://www.warc.com/newsandopinion/opinion/warc-from-home-creative-briefs--what-they-are-and-why-they-still-matter/en-gb/3527); paywalled, seen via search snippets).
- **Insight:** Mark Pollard defines it as "an unspoken human truth that sheds new light on the problem", written as a tension ("___ but ___"). He warns that AI produces polished insights where "the problem could belong to any brand" ([markpollard.net](https://www.markpollard.net/)). That is exactly the failure an LLM brief writer is prone to.
- **Why it matters:** in the BetterBriefs 2021 survey (~1,700 respondents, IPA), 80% of marketers rated their briefs good but only 10% of agencies agreed; 89%/86% said good work is hard without a good brief ([IPA](https://ipa.co.uk/news/betterbriefs)). These are perception surveys, not causal evidence.
- **Recommendation:** have the composer (or a new `brief_writer`) output a Pydantic `CreativeBrief` *alongside* the readable report:

  ```text
  objective, audience { who, insight ("X but Y" tension) },
  single_minded_proposition (one sentence, no "and"),
  reasons_to_believe [ {claim, source_id} ]   # from selling points + cited research
  brand { voice/tone, distinctive_assets, do_not }  # palette, logo, characters
  trend_bridge { fit_score, fit_mode, bridge, motifs[], risks[] }
  mandatories [ ... ]   # e.g. product shown, selling point X, legal lines
  avoid [ ... ]         # visual_avoid + risk items
  desired_response (think / feel / do)
  angles [ 3–5 distinct creative routes, each tied to the proposition ]
  ```

  Every later agent then reads named fields (`{creative_brief.single_minded_proposition}` etc.) instead of a 2,000-word narrative.
- **Brand research:** add brand research to the campaign planner (pass `{brand}`): voice, positioning, recent campaigns and **distinctive assets**.

**F3. Critique the brief, not only degraded research.** **[L, inferred]**
- **Recommendation:** run a cheap brief check on every run, as code plus one LLM pass:
  - the proposition is a single sentence;
  - the insight contains a tension and is brand-specific (Pollard's "could this belong to any brand?" test);
  - each reason to believe cites a source;
  - fit_score ≥ the threshold, or the light-touch mode is used.
- **Interactive mode:** checkpoint 1 should review the **brief** (proposition, angles, fit), which is the right place for human leverage. It currently reviews the long report.

**F4. Make the core inputs deterministic.** **[H, code]**
- **Recommendation:** seed brand, audience, product, selling points and trend through `createSession` initialState, the way the visual keys already are, and keep the chat message only as a human-readable echo. That removes the reliance on `memorize` and the `target_search_trend(s)` naming mismatch.
- **Pitfall:** note the known gotcha that `load_session_state` currently blanks these keys, so the callback needs a matching change.

### 3.2 Making creatives match the brief

**F5. Check against the brief, then revise only what fails.** **[L]**
- **Evidence:** Walmart's generate → evaluate → refine loop, feeding each failure's specific feedback back, raised the share of copies passing every check by **16–36 points** across 3 campaigns ([arXiv 2504.10391](https://arxiv.org/html/2504.10391)).
  - **Caveat:** it was measured by the same automated evaluators. That paper's claimed +38–45% click-through rate over human-written copy was **refuted** by verification, so don't cite it.
- **Recommendation:** replace "critic picks 4 of 10" with:
  1. A per-copy **checklist** derived from the brief: delivers the proposition; names the product; uses ≥1 reason to believe; uses the trend bridge (not just a trend word); matches the tone; meets each mandatory; avoids the `avoid` list. Use code checks where possible (product name present, length) and an LLM for the rest.
  2. **Revise failures** with the failed items quoted, at most 1–2 rounds, then pick the final 4.
  3. Critique the **CTA** too.
- **Visual critic:** apply the same pattern, and give it the brief and the paired ad copy (today it gets neither).

**F6. Ground the drafters in concrete facts, not the research narrative.** **[L]**
- **Evidence:** Amazon found that grounding generation in concrete product context cut human-flagged relevance failures from **10% to 1%** ([arXiv 2506.17863](https://arxiv.org/html/2506.17863v1)). There, relevance meant matching the landing page.
- **Recommendation:** pass the structured fields (proposition, reasons to believe, product facts, mandatories, brand voice) directly to the drafters and critics, including `{brand}` and `{target_audience}`, instead of relying on them to dig the facts out of `combined_final_cited_report`.

**F7. Brand presence and distinctive assets.** **[H for the guidance; M/L for the evidence numbers]**
- **Google's video ABCD framework:** "Brand early, often, and richly … draw on all your branding assets" ([Google Ads Help](https://support.google.com/google-ads/answer/14783551)). Kantar's validation is correlational, based on predicted sales: +30% short-term ([Kantar](https://www.kantar.com/industries/technology-and-telecoms/validating-googles-abcd-framework-with-the-power-of-artificial-intelligence)).
- **Still images:** ABCD is video-only, but Google's display guidance says: make the product the focus, use real scenes with natural shadows, no collages, and **don't overlay logos or text** (supply the logo separately) ([Google Ads Help 9823397](https://support.google.com/google-ads/answer/9823397)).
- **Ehrenberg-Bass:** "the more attention-grabbing the creative, the harder the branding has to work". Distinctive assets (palette, characters, shapes) carry branding; generic category cues should never brand on their own ([marketingscience.info](https://marketingscience.info/brands-of-distinction/)).
- **Recommendation:** add `brand.distinctive_assets` to the brief, fed by research plus the `brand_colors` and logo/reference inputs.
  - The art director must place at least one asset in each concept.
  - Bias the **style shortlist** toward families compatible with brand and tone, rather than purely random, while keeping it stratified.
  - Our in-image-text cap (≤2 of 4) already leans toward Google's no-overlay guidance; keep in-image text rare and purposeful.

### 3.3 Creative quality and diversity

**F8. Homogenization is a model-family problem; vary the angles at prompt level.** **[M]**
- **Same ideas across models:** LLM creative answers are far more similar to each other than human answers, across 22 models ([Wenger & Kenett 2025](https://arxiv.org/abs/2501.19361)). Swapping models or quota buckets won't diversify output.
- **Templates make it worse:** heavy structural templates make outputs converge, even at high temperature, while light "steer" prompts diversify better ([arXiv 2505.18949](https://arxiv.org/html/2505.18949v1)). That warns against over-templating *within a generation prompt*; it fits with a structured *brief*, as long as each angle is a short steer.
- **Verbalized Sampling** (ask for k candidates with probabilities) gave 1.6–2.1× diversity on creative writing ([arXiv 2510.01171](https://arxiv.org/html/2510.01171v1)) **[L]**.
- **A live ad A/B test** of diversity-aware headline generation: **+4.0% advertiser value, +1.4% CTR** vs a sampling + supervised fine-tuning baseline ([DIVER, arXiv 2508.18739](https://arxiv.org/html/2508.18739)) **[L]**.
- **Recommendation:** have the brief produce **3–5 distinct angles** (different insight framings or audience tensions), and have the drafter generate copies *per angle*: different routes, not 10 variations on one idea. Try Verbalized Sampling in the drafter and art director, followed by a relevance filter (F5). `style_shortlist` already does this for visual style; extend the idea to message.

**F9. Pixel-level image QA and product fidelity.** **[L evidence, H gap]**
- **Evidence:** plain text-to-image generation rarely keeps real products faithful: **7.5%** of baseline two-product furniture ads were rated high quality. Masked inpainting that keeps the product's pixels fixed is the proven alternative ([arXiv 2603.13745](https://arxiv.org/html/2603.13745v1); small formative study).
- **Recommendation:** after `generate_image`, run a **vision-model check** (Gemini Flash, structured output) on the rendered image:
  - product visible and recognisable;
  - trend motif present;
  - any in-image text matches the quoted copy exactly and is legible, with no gibberish;
  - brand colours or assets present;
  - no obvious artefacts or safety issues.
- **On failure**, re-render once with the specific failures added to the prompt. Mind the image quota: 2 RPM was measured on the old model; re-measure for nano-banana-2.1.
- **Optional:** when `reference_image_role=product`, try a background-inpainting path. Also feed the image check's verdict into the eval's visual scores, so the judge scores **pixels**, not prompts.

**F10. Follow Google's image-prompting guidance more closely.** **[M, official vendor guidance, not benchmarked]** Google's Gemini image docs (updated 2026-10-06) and the Nano Banana guide recommend ([docs](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/gemini-image-generation-best-practices), [blog](https://cloud.google.com/blog/products/ai-machine-learning/ultimate-prompting-guide-for-nano-banana)):
- **Context:** state the image's **purpose and brand/audience context**.
- **Formula:** Subject + Action + Location/context + Composition + Style.
- **With references:** reference images + relationship instruction + new scenario. Up to 14 references are allowed, which suits product or character consistency (verify for nano-banana-2.1).
- **In-image text:** put the exact words **in quotes**, describe the typography, and settle the text before the image.
- **Recommendation:**
  - Audit `IMAGE_PROMPT_GUIDE` against these. It's style-first, but has no explicit purpose/context line or reference-image formula.
  - Pass the final headline, quoted, into prompts that carry text.
  - Re-check our claim that "flash-image cannot do true style transfer" against multi-reference support.
  - Consider allowing **several reference images** (product + logo + style) instead of one.

### 3.4 Evaluation

**F11. Use the judge as a compliance gate, not a ranker; calibrate it.** **[M]**
- **Weak agreement with experts:** in the Creativity Benchmark (678 ad professionals, 11k pairwise comparisons), judge–human rank correlation (Spearman ρ) was 0.04–0.63, mostly ≤ 0.35. The authors say judges "should not be used to select or rank" creative work ([arXiv 2509.09702](https://arxiv.org/html/2509.09702v2)).
- **Consistent but biased:** a judge with 0.988 test-retest reliability still had 0.125 position bias ([arXiv 2606.19544](https://arxiv.org/html/2606.19544v1)).
- **Good at accept/reject:** as a binary compliance check against concrete rules, an LLM judge agreed with humans ~90% of the time (raw agreement, ~150k ad copies), and its errors leaned strict ([arXiv 2506.17863](https://arxiv.org/html/2506.17863v1)) **[L]**.
- **Recommendations:**
  1. Pass the **structured brief** to `creative_eval`, and add brief-specific checks: proposition delivered, reasons to believe used, mandatories met, trend bridge used, brand assets present.
  2. Split eval output into **gates** (binary, trustworthy) and **quality scores** (advisory). Don't use scores alone to choose creatives.
  3. Build a **small human-labelled calibration set** (for example 50 creatives rated by the team) and track judge–human agreement as kappa. Run position-swap / judge-swap checks if pairwise ranking is ever added.
  4. Consider a judge from a different family or tier than the producers. The critics are flash and the root/composer are Pro; the judge is also Pro (the earlier 2.5-pro A/B showed judges differ systematically).
  5. Optionally let failed **gates** trigger one regeneration (F5) before the final report. The unused `max_retries` hook already exists.

**F12. Learn across runs.** **[L, inferred]**
- **Recommendation:** read back `creative_evals` / `trend_creatives` for the same brand:
  - recent high-scoring angles and styles become positive references;
  - repeated gate failures become `avoid` items;
  - styles used in the last N runs are down-weighted.

  This also helps the variety complaint that motivated #257/#258. Longer term, the bandit experiments' click results are a real-world signal that could rank angles (out of scope here).

---

## 4. Suggested sequencing (for discussion; nothing implemented)

| # | Change | Effort | Evidence | Expected effect |
|---|---|---|---|---|
| 1 | Pass `{brand}`/`{target_audience}` to research, copy drafter, copy critic, visual critic; give the visual critic the paired copy | S | H (code gap) | Fewer off-brand or generic creatives |
| 2 | Seed core inputs via initialState (F4) | S | H | Removes an LLM-dependence failure mode |
| 3 | Restore risk section; add brand–trend fit score + fit mode + bridge (F1) | S–M | M | Fewer forced tie-ins |
| 4 | Structured `CreativeBrief` schema + brand research + angles (F2, F7, F8) | M | H/L | Brief everyone can check against; angle diversity |
| 5 | Brief-checklist critic with revise-on-fail for copy and concepts (F5, F6) | M | L | Higher compliance with the brief |
| 6 | Vision-model image QA + one targeted re-render (F9) | M | L/H | Catches bad renders (the "one or two are always bad" complaint) |
| 7 | Eval gets the brief; gates vs scores; human calibration set (F11) | M | M | Trustworthy pass/fail; honest quality signal |
| 8 | Prompt-guide audit vs Google guidance; multi-reference (F10) | S–M | M | Better product and text fidelity |
| 9 | Cross-run memory (F12); Verbalized Sampling trial (F8) | M | L | Variety across runs |

Items 4–7 should be checked with the existing eval CI (`adk eval` + efficiency gate) plus a before/after human review of ~10 runs, since the judge alone is not a valid ranker (F11).

---

## 5. Caveats and open questions

- **Thin evidence.** Most of it is single 2025–2026 preprints. Only the brand–moment fit study is peer-reviewed, and it is small.
- **Circular metrics.** The quality-loop gains are measured by the same evaluators that gave the feedback.
- **Diversity results come from other domains:** creativity tests, poems, stories and text headlines, not Gemini ad images.
- **The image guidance is vendor advice.** Limits such as 14 references need confirming for `gemini-nano-banana-2.1`.
- **Brief guidance is mostly practitioner sources.** There's no causal study linking brief quality to measured effectiveness, and the BetterBriefs figures are perception surveys. "Think/feel/do" and "true, relevant, distinctive" have no authoritative primary source.
- **Refuted, don't cite:** the Walmart method details, the +38–45% CTR over human copy, and "raw agreement overstates kappa by 34–41 points".
- **Open questions:**
  - Does a structured brief measurably raise human-rated relevance? An A/B test of brief variants would answer it.
  - How well does our judge agree with the team on a labelled set?
  - Does Verbalized Sampling keep its gains on Gemini 3.x ad copy without hurting relevance?
  - Is multi-reference conditioning enough for product fidelity, or is inpainting needed?

## Sources

- Santos, Gonçalves & Teles (2023), *Psychology & Marketing* — https://onlinelibrary.wiley.com/doi/full/10.1002/mar.21756
- IPA/ISBA, *The Client Brief* — https://www.acaweb.ca/en/wp-content/uploads/sites/2/2016/09/THE-CLIENT-BRIEF-FINAL-Eng-July-18-06.pdf
- IPA, BetterBriefs — https://ipa.co.uk/news/betterbriefs
- WARC on creative briefs — https://www.warc.com/newsandopinion/opinion/warc-from-home-creative-briefs--what-they-are-and-why-they-still-matter/en-gb/3527
- Mark Pollard — https://www.markpollard.net/
- Google ABCD — https://support.google.com/google-ads/answer/14783551 ; Kantar validation — https://www.kantar.com/industries/technology-and-telecoms/validating-googles-abcd-framework-with-the-power-of-artificial-intelligence
- Google responsive display ad guidance — https://support.google.com/google-ads/answer/9823397
- Ehrenberg-Bass, distinctive assets — https://marketingscience.info/brands-of-distinction/
- Walmart refine loop — https://arxiv.org/html/2504.10391
- Amazon ad generation + AutoEval — https://arxiv.org/html/2506.17863v1
- Wenger & Kenett, LLM homogenization — https://arxiv.org/abs/2501.19361
- The Price of Format — https://arxiv.org/html/2505.18949v1
- Verbalized Sampling — https://arxiv.org/html/2510.01171v1
- DIVER (RedNote) — https://arxiv.org/html/2508.18739
- Creativity Benchmark — https://arxiv.org/html/2509.09702v2
- Reliability without Validity — https://arxiv.org/html/2606.19544v1
- Gemini image best practices — https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/gemini-image-generation-best-practices ; Nano Banana guide — https://cloud.google.com/blog/products/ai-machine-learning/ultimate-prompting-guide-for-nano-banana
- Product-preserving ad generation — https://arxiv.org/html/2603.13745v1
