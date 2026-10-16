# Trend state awareness in creatives: research and recommendations

**Status:** Ideas 1–4 implemented in #318–#321: date awareness and trend context (#318), a validated trend situation (#319, plus the schema enum fix #321), and the timely slot with its tone playbook (#320). Ideas 5–9 are still open. **Date:** 2026-10-11 (status updated 2026-10-12).

**Question:** how can the creative workflow take a trend's *current state* into account, and use it in only 1–2 of the 4 creatives in a slate? For example, for "Texas Football": they lost yesterday, so a creative references the loss tastefully; the game is tomorrow, so it builds anticipation; it's mid-season with no game near, so it stays evergreen.

**Method:**
- **Web research:** a deep-research run over 6 angles: 22 sources fetched, 103 claims extracted, and 25 checked by 3-vote adversarial verification (18 confirmed, 7 refuted).
- **Codebase:** two thorough explorations of how a trend and time flow through `trend_scout` → CRF / frontend → `creative_agent` → `creative_eval` → persistence.

**Labels used below:**
- **Verified:** a sourced finding that survived verification.
- **Recommendation:** our design. It is not a sourced fact.

---

## 1. Executive summary

1. **No agent knows what day it is.** No prompt in `trend_scout`, `creative_agent` or `creative_eval` gets the current date. With no date, a model can't place "yesterday" or "tomorrow", and falls back on its training data. That is exactly the failure retrieval-plus-date fixes: on FreshQA, adding search evidence lifted GPT-4 from 12% to 77% on fast-changing questions. **This is the cheapest and highest-value fix** (Verified F1).
2. **Almost nothing about the trend reaches the creatives.** `trend_scout` researches each trend and records its Google Trends `refresh_date`, but only the bare term string reaches `creative_agent`. Both the CRF batch path and the frontend's trend cards drop the research, the dates and the detection time (§3). `creative_agent` re-researches the trend with no notion of "now" or of event timing.
3. **Recommended design:** a date-aware, search-grounded **"trend situation" step** in the research fan-out. It produces a dated, cited `TrendSituation` (phase, event, outcome, sources, confidence, valid until).
   - **Validation in code:** a deterministic validator **forces "unknown"** whenever the outcome or date isn't supported by a cited source, or falls outside a sensible window.
   - **A capped slot:** a **"timely slot"** capped at 1–2 of 4 concepts. It reuses the person-casting pattern we already have (`MAX_CAST_CONCEPTS` plus `enforce_person_casting`).
   - **Tone and safety:** phase-specific tone rules and safety (no salesy push after a loss; silence on tragedies).
   - **Checking and expiry:** a judge gate checks timely claims against the sources, and every timely creative carries a **valid-until** date, so it gets flagged once it's stale.
4. **Keep timely creatives a minority hedge, measured rather than assumed.** Real-time marketing increased sharing in studies, especially for unpredictable events. But timely posts traded originality and craft for topical meaning, and only craft and originality raised responses (Verified F7). So: 1–2 of 4 creatives, the same craft gates, and measure it (bandit arms, ratings).

---

## 2. Research findings

| # | Finding | Confidence | Sources |
|---|---|---|---|
| F1 | **Search grounding plus date awareness** is the right base. LLMs without retrieval do badly on fast-changing facts at every model size. On FreshQA, GPT-4 with search evidence gained +49 points Strict, reaching 77.1% on fast-changing questions versus 12.0% without. 2023 numbers on a QA benchmark; the direction transfers, the exact figures don't. | High | [FreshLLMs](https://arxiv.org/abs/2310.03214), [Vertex grounding docs](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/grounding/grounding-with-google-search) |
| F2 | **Grounded Gemini responses return citation metadata.** `groundingChunks` gives uri, title and domain. `groundingSupports` maps text segments to chunks, with confidence scores. Code can therefore **reject an outcome or date sentence that has no supporting source**. This proves attribution, not truth; there are no publish dates, and the metadata can be missing. | High | [Vertex grounding docs](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/grounding/grounding-with-google-search) |
| F3 | **Let the state step abstain ("unknown") rather than invent an outcome**, triggered in code, not by the model's own judgement. Models are poorly calibrated about when to abstain. | Medium | [Abstention survey (TACL)](https://arxiv.org/abs/2407.18418) |
| F4 | **Industry practice is to version creative by event state:** pre-event countdown, in-event triggers, and post-event outcome variants, driven by deterministic data feeds where available. Pre-approved templates per scenario (win / loss / record). Vendor and agency sources; the specific claims about speed were refuted. | Medium | [Sportradar](https://sportradar.com/content-hub/blog/how-does-sports-moments-marketing-work-the-technology-behind-real-time-fan-engagement/), [Digiday on Genius Sports](https://digiday.com/media-buying/genius-sports-opens-up-real-time-live-sports-targeting-to-brands/), [Now We Collide](https://www.nowwecollide.com.au/news/how-sports-marketing-wins-in-real-time) |
| F5 | **Tone follows the state:** hype before; celebration after a win; empathy and loyalty after a loss, with no salesy push. **Never commercialise tragedy** (the Gap / Hurricane Sandy 2012 backlash is the canonical case). | Medium | [Now We Collide](https://www.nowwecollide.com.au/news/how-sports-marketing-wins-in-real-time), [Sportradar](https://sportradar.com/content-hub/blog/how-does-sports-moments-marketing-work-the-technology-behind-real-time-fan-engagement/), [newsjacking.org](https://www.newsjacking.org/newsjacking/when-newsjacking-goes-wrong-real-lessons-from-brands-who-took-it-too-far/) |
| F6 | **Time-bound creative needs an explicit expiry.** Ad platforms build this in: Google Ads countdown customizers stop serving after the end date. | High | [Google Ads countdown help](https://support.google.com/google-ads/answer/6193743?hl=en) |
| F7 | **Timeliness is not a free lift.** Real-time marketing got more word of mouth, especially for *unpredictable* events (n=1,500 tweets). But timely posts traded originality and craft for meaning, and only craft and originality drove responses (516 posts plus an experiment). Observational, older, social engagement data. | Medium | [Willemsen et al. 2018](https://www.tandfonline.com/doi/full/10.1080/02650487.2018.1485214), [Mazerant et al. 2021](https://www.sciencedirect.com/science/article/pii/S1094996820301055) |
| F8 | Google Trends "Trending Now" RSS items link news articles, whose headlines often separate a result from an upcoming game. The feed is undocumented, has no per-article dates and empty snippets, and our pipeline uses the BigQuery public dataset instead. **At most a hint, never the source of truth.** | Low | [dev.to write-up + direct fetch](https://dev.to/egrarobo/pytrends-trendingsearches-returns-404-get-google-trends-trending-now-from-the-rss-feed-2026-59ma) |

**Refuted** (don't rely on these):
- JCDecaux "+17% response / +16% sales from contextual messaging" (0–3);
- "highlight conversions happen within 24 h; recall −27%/day" (0–3). **No reliable decay figure exists**, so validity windows must be configurable knobs;
- "Sportradar detects events within seconds from deterministic feeds" (0–3);
- the CJR claims that Gemini cites fabricated URLs more than half the time (0–3 as stated). Date and outcome errors remain a real risk anyway;
- "RSS pubDate marks when a trend started" (0–3).

**Not verified:** the legal and IP limits (team marks, NCAA/NFL IP, player NIL, implied sponsorship). The safeguards below are prudent defaults, not sourced findings. Get legal review before running timely sports creative at scale.

---

## 3. What the code does today

> **Note (2026-10-12):** this section describes the code **before #318**. Since then every agent seeds per-run date state (`agent_common/clock.py`), creative runs get `trend_detected_on` + `trend_scout_context`, `trend_situation_pipeline` validates the trend's current moment, and the timely slot caps dated creatives (see CLAUDE.md → Configuration).

### Trend detection (`trend_scout`)
- `get_daily_gtrends` (`trend_scout/tools.py:62-130`) reads `bigquery-public-data.google_trends.top_terms` at `MAX(refresh_date)`. It keeps only `term, rank, refresh_date`, with no rising terms, scores or region. Only the term list goes to state (`raw_gtrends`, :121); the `refresh_date` is shown to the model once and then dropped.
- The trend-research searcher is told to note "dates" (`trend_scout/prompts.py:12-34`) but **gets no current date**, and skips "game, match, or score lookup" trends (:31). The synthesizer's JSON has no date or event-timing field.
- The BigQuery row in `target_trends_crf` stores `refresh_date`, `trawler_date` (`datetime.now()`, :353) and `entry_timestamp`, but **none of the research text**, which survives only in a GCS session-state JSON.

### Handoff to creatives
- **Batch path:** the CRF worker selects `*`, but builds the run from only the term and the campaign fields (`cloud_functions/creative_fanout/main.py:680-689`, :343-353). `refresh_date`, `trawler_date` and the GCS research pointer are dropped. The delay between detection and generation is unbounded (oldest rows first, capped at `CRF_MAX_ROWS_PER_RUN`).
- **Frontend path:** the trend cards (`frontend/src/components/trend-cards.tsx:67-76`) pass only `trend.term`.
- **Result:** `creative_agent` re-researches the trend from scratch.

### Creative research and brief (`creative_agent`)
- **No date anywhere:** there is no `{current_date}` token; the only `datetime.now` is in GCS folder names (`agent_common/state.py:69`) and BigQuery timestamps.
- **Research prompts:** the trend planner asks "Why is the trend happening now? (e.g., recent event…)" (`creative_agent/sub_agents/trend_researcher/agent.py:69`), and the searcher should "preserve … dates". But the synthesizer has **no event-timeline section**, and the merge, refinement and composer steps don't mention time.
- **Sources:** `collect_research_sources_callback` (`creative_agent/callbacks.py:396-457`) captures grounding chunks and supports (title, url, domain, supported claims, confidence) but **no publish dates**.
- **Brief:** `TrendBridge` (`creative_agent/schemas.py:364-383`) is `fit_score, fit_mode, bridge, motifs, risks`, with **no state or timing**. `brief_check` only checks fit consistency and motifs.

### Copy, visuals and judging
- **Copy and visuals:** per concept, `trend_connection` / `trend_reference` / `trend_motif` / `angle_id`. The critic's rule "4 copies covering ≥3 angles" (`prompts.py:479-482`) shows we already shape a slate's mix.
- **A reusable subset pattern** (person casting):
  - the cap `MAX_CAST_CONCEPTS` (`creative_agent/config.py:85,176,344`);
  - rules seeded into state **only when relevant** (`callbacks.py:129-132`), so other runs get byte-identical prompts;
  - per-concept `casts_person_reference` plus a reason;
  - deterministic `enforce_person_casting` (`concept_guard.py:363-415`) inside `ensure_trend_and_product_callback`.
- **Judge:** gates `trend_risks_respected` and `trend_motif_visible`, dimensions `trend_authenticity` and `trend_visual_connection`. It gets no date and no sources (`creative_eval/agent.py`, `brief.py:76-119`).

### Human review and persistence
- **Human review:** interactive checkpoint 1 (an editable structured brief) is the natural place to approve a timely angle (`interactive_creative/review_tools.py`).
- **Persistence:** `trend_creatives` / `creative_evals` keep a `datetime` and the trend string. Share snapshots keep `created_at` and the trend. Bandit features have `topic_matches_trend` and no time features. **No freshness or expiry concept exists for creatives.**

---

## 4. Recommendations, best ideas first

### Idea 1: give every agent "now" (foundation; small, high value)
- **The date:** at state init (`callbacks._set_initial_states` / `agent_common.state.seed_initial_state`), seed `current_date` (ISO), `current_datetime` and `current_timezone`. Use a config default (e.g. `America/New_York`), or the brand's or trend's geography if known.
- **Prompts:** reference them in the trend planner, searcher and synthesizer, the brief writer, the drafter and critic, and the judge. Example: "Today is {current_date} ({current_timezone}). Resolve 'yesterday/tomorrow' relative to this date."
- **Trend data:** forward the trend's `refresh_date` (detection date) and trend_scout's research (hook, context, cultural angle) into the creative run. The CRF can forward it as `trend_detected_on` / `trend_scout_context`; the frontend trend cards can pass it in `initialState`.
- **Agent Engine caveat:** the engine pickles the agent at deploy time, so the date must be computed per run at state init, never at import.

### Idea 2: a dated, cited `TrendSituation`, computed once per run with a deterministic validator
- **Where:** a new branch in `combined_research_pipeline`'s START fan-out, beside the planners and `load_brand_history`. It is a `RetryUntilKeyNode`-wrapped `trend_situation_searcher → synthesizer` (google_search, date-aware), followed by a **no-LLM validator node**.

**Schema:**
```
TrendSituation {
  as_of: ISO datetime + tz,
  phase: upcoming | live | just_happened | ongoing | evergreen | sensitive | unknown,
  event_name: str, event_start: ISO | null, event_end: ISO | null,
  outcome: win | loss | draw | other | null,   # just_happened only
  outcome_summary: ≤ 1 factual sentence,
  audience_sentiment: positive | negative | mixed | neutral,
  sources: [{url, title, published | null}],
  confidence: high | medium | low,
  valid_until: ISO,
  timely_ok: bool,
  tone_rule: str          # derived in code from phase/outcome (Idea 4)
}
```

**Validator, in code** (Verified F2/F3: abstain by rule, not by the model's judgement):
- It parses the dates and requires `event_start` / `event_end` within ±N days of `current_date` (knob, default 7).
- An outcome needs **≥1 cited source**, ideally 2 agreeing, whose grounding support covers the outcome sentence. Our existing `sources` / `url_to_short_id` capture provides this.
- When the URL carries a date, `published` must be ≥ `event_start`.
- `valid_until` must be in the future.
- **Otherwise it forces `phase=unknown`, `timely_ok=false`.** Unknown means no timely concept, which is today's behaviour, so the feature fails safe.

**Non-sports trends use the same taxonomy:**

| Trend | Phase |
|---|---|
| TV finale tomorrow | `upcoming` |
| Product launch yesterday | `just_happened` (outcome `other`) |
| Award show tonight | `live` |
| Hurricane, injury, scandal or death | `sensitive` |

**Deterministic sources (optional, later):** a provider-agnostic adapter interface (`resolve_event(trend) -> {start, end, outcome}`) for sports schedule and score APIs. When present it overrides the LLM's dates and outcome (Verified F4). The Google Trends RSS news headlines may seed the searcher as hints only (F8).

### Idea 3: a capped "timely slot" (1–2 of 4), reusing the person-casting pattern
- **Config:** `MAX_TIMELY_CONCEPTS` (default 1, range 0–2; 0 = off), plus the kill switch `TREND_STATE_ENABLED`. Optionally a per-run opt-in like `learn_from_ratings` ("Allow a timely creative").
- **Rules only when needed:** seed `timely_rules` into state **only** when `timely_ok`. The art director, drafter, critic and finalizer read `{timely_rules?}`, so evergreen runs get byte-identical prompts.
- **Per concept:** `uses_trend_state: bool` plus `timely_reason` on `FinalAdCopy` and `VisualConceptFinal`. The timely visual is paired with the timely copy (`ad_copy_id`).
- **Deterministic enforcement** (new `concept_guard.enforce_timely_cap`, alongside `enforce_person_casting`, and the copy-gate equivalent):
  - clears flags beyond the cap;
  - clears all flags when `timely_ok` is false;
  - flags **dated or outcome words in evergreen concepts** (a small lexicon like `claims.py`: won, lost, beat, defeat, tonight, tomorrow, yesterday, this weekend, score, final, game day…) for a fix round. An evergreen creative must never state an outcome or a date it can't support.
- **Brief:** add `TrendBridge.timely_angle` (the allowed reference, tone and validity) plus a `brief_check` that the brief never states an outcome or date missing from `trend_situation`. Render a one-line "Current moment" into `creative_brief_md` so every downstream prompt sees it.

### Idea 4: a phase-specific tone and safety playbook (Verified F5)
Derived in code into `tone_rule`, injected only into the timely concept:

| Phase | Do | Don't |
|---|---|---|
| `upcoming` (≤ N days) | anticipation, rituals, countdown energy ("game-day ready") | predict the score; imply the brand backs a team |
| `live` | **default: no timely concept** (the state changes in minutes); an optional short window only with a deterministic source | — |
| `just_happened` + win | celebration, pride, "bragging rights" | gloat at rivals; mock the losing side |
| `just_happened` + loss | empathy, loyalty, "we're still with you", comfort | urgency or discount pushes, jokes at the team's expense, the word "lose" in a headline |
| `ongoing` / `evergreen` | season-long fandom, rituals | dated claims |
| `sensitive` (injury, scandal, tragedy, disaster) | **no timely concept** (silence) | anything reactive |

**Audience perspective:** a Texas loss is a win for the rival's fans.
- Phrase timely references from the brand's *target audience's* perspective.
- When the audience is mixed or unknown, use neutral "big game" framing rather than a side.
- This extends the existing `trend_bridge.risks` and the `trend_risks_respected` gate.

**IP:** no team logos, wordmarks, mascots or player likenesses, and no implied sponsorship. Use generic colours, motifs and fan rituals. This extends today's "iconography, never likeness" rule for real-person trends. Get legal review before scaling.

### Idea 5: validity windows and freshness (Verified F6)
- **Windows** (all knobs, since no reliable decay data survived verification):
  - `upcoming`: valid until `event_start`;
  - `just_happened`: valid until `event_end` + 48 h, configurable to 24–72 h.
- **Where the date lives:** `valid_until` on the timely concept, on `generated_images[c]` and on the eval report.
- **UI:** the results page and HTML gallery show a "Timely · valid until …" chip, which turns to "Expired" afterwards.
- **Shares:** a share link created after expiry warns, or leaves the timely creative out, like the existing `person_not_shareable`.
- **Experiments:** bandit deploys after expiry warn.
- **Batch (CRF) runs:** always **re-derive the situation at run time**; never reuse trend_scout's detection-time state, because runs can lag by days.
- **"Refresh timely creative" action (later):** re-run the situation and regenerate only the timely concept pair when the state changes, e.g. a pre-game anticipation creative becomes an aftermath one. Store it versioned (pre / post), not overwritten.

### Idea 6: the judge checks timely claims
- **New gate `timely_claim_supported`:** applies only to designated creatives; it passes in code for the others, like the person gate. The judge sees `trend_situation` (`as_of`, phase, outcome, sources) and checks two things: the reference matches the phase and outcome, and the tone rule holds (no celebrating a loss, no offers after a loss).
- **Evergreen creatives:** the deterministic lexicon (Idea 3) already guards them.
- **Versioning:** bump `JUDGE_VERSION` and mirror the gate in `frontend/src/lib/eval-dimensions.ts`.
- **Same craft gates:** keep `no_visual_defects`, `trend_motif_visible` and the copy gates unchanged for timely creatives (Verified F7: craft and originality drive response).

### Idea 7: humans approve reactive claims
- **Interactive mode:** checkpoint 1 shows `trend_situation` (phase, outcome, sources, valid until) as editable. The user can correct the phase or outcome, or **turn timely off**, before any copy exists.
- **trend_scout:** the trend picker and trend cards can show a phase badge ("Game tomorrow", "Lost yesterday", "Ongoing"). This helps choose trends, and can be passed into the creative run as a hint, which the run still re-verifies.

### Idea 8: measure whether timely creatives win (Verified F7, open question)
- **Bandit:** tag arms with `timely: true/false` in the experiment snapshot, so the existing bandit experiments compare timely and evergreen arms. A context feature such as `days_from_event` could follow later.
- **Ratings:** add a "timely" filter to the rating calibration and the run notes, and compare judge and human ratings for timely versus evergreen creatives.
- **Expectation, per the studies:** surprises such as a big upset are likelier to lift than scheduled anticipation. Treat this as a hypothesis.

### Idea 9 (advanced): contingency variants for scheduled events
For a game tomorrow, a batch run could pre-generate an anticipation creative **and** "win" and "loss" aftermath versions of the timely concept. After the event, a refresh picks the matching variant, which then goes live once a human approves it.
- **Benefit:** it removes the creative-generation latency after the event.
- **Cost:** +2 renders, against the 2 images/min quota.
- **When:** consider it only after Ideas 1–6 prove value. It is industry practice (Verified F4: pre-approved templates per scenario).

---

## 5. Phased implementation

| Phase | Scope | Changes creatives? | Exit criterion |
|---|---|---|---|
| **0. Plumbing** | `current_date`/tz in state and prompts (Idea 1); forward `refresh_date` and trend_scout context via the CRF and frontend | Slightly: better research recency | prompts show the date; tests prove the CRF forwards fields |
| **1. Observe** | `TrendSituation` branch + validator (Idea 2), stored in state and shown read-only on the results page ("Current moment: lost yesterday · 2 sources · valid until …") | No | on ~20 real trends, the phase and outcome match a human check ≥ 90%; unknown is used when unsure |
| **2. Timely slot** | `MAX_TIMELY_CONCEPTS`, rules, per-concept flag, enforcement + lexicon, tone playbook, `TrendBridge.timely_angle` (Ideas 3–4) | Yes, at most 1 (default) of 4 | the timely concept matches its phase; evergreen concepts free of date and outcome words |
| **3. Guard** | judge gate, validity windows + chips, share and experiment expiry warnings, CRF run-time re-derive (Ideas 5–6) | No | expired creatives flagged; judge catches injected false outcomes in tests |
| **4. Human review and measurement** | checkpoint-1 editing, trend-card badges, bandit `timely` tag, rating filter (Ideas 7–8); optional sports-data adapter and contingency variants (Ideas 2, 9) | — | first timely-vs-evergreen readout |

**Knobs to add:** `TREND_STATE_ENABLED` (kill switch), `MAX_TIMELY_CONCEPTS` (0–2), `TREND_EVENT_WINDOW_DAYS`, `TIMELY_VALID_HOURS_AFTER`, `TREND_TIMEZONE`, and an optional per-run "allow timely creative" toggle. Follow the `creative_agent/config.py` parser pattern and `deploy_agent.py` `ENV_VAR_DICT`, with tests.

---

## 6. Risks and open questions
1. **Wrong outcomes or dates** are the main risk (a "loss" that was a win). Mitigations: grounding-backed validation, abstention, the judge gate, human review in interactive mode, a default of 1 timely concept, and a kill switch.
2. **Staleness:** batch runs and shares can outlive the moment, hence valid-until, re-deriving on each run, and expiry chips.
3. **Validity windows** have no reliable decay data. Start conservative (anticipation until kickoff; aftermath 48 h) and tune with measurement.
4. **Mixed or rival audiences** and **sensitive outcomes** (injuries, scandals): a neutral framing default plus the `sensitive` phase means silence.
5. **Legal and IP** (team marks, NIL, implied sponsorship) wasn't verified, so it needs legal review. Default: no marks, no likenesses, no sponsorship language.
6. **Lift is unproven for paid ads.** The studies measure social sharing. Run Phase 4's measurement before increasing the timely share.
7. **Deterministic data sources:** is a sports-data API worth adding for sports trends, or is grounding plus the validator enough? Decide after Phase 1's accuracy readout.
