# Image Diversity Fixes 1–3 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` (or `subagent-driven-development`) skill to implement this plan task-by-task.

**Goal:** Make creative runs produce visibly different image sets from run to run, and cut the "one or two bad images per set". Three changes: cap in-image text, pick a random style shortlist in code plus an across-set composition rule, and de-template the prompt guide (also fixing the Educational mapping and requiring a trend motif in every concept).

**Context (why):** The 2026-10-06 investigation looked at 564 concepts from 141 runs.
- **No prompt edits since 2026-07-18.** Since #123 switched the prompt-writing worker model to `gemini-3.8-flash` (2026-09-28), the model applies `IMAGE_PROMPT_GUIDE` literally:
  - **In-image text:** quoted in-image text went from 57% to 96% of prompts, and "text across the top" from 24% to 68%.
  - **Centred heroes:** from 18% to 46%.
  - **Copied openers:** template openers are copied verbatim, e.g. "A cut-paper collage…" in 57% of recent runs.
- **The bad images have three causes:** garbled or leaked text (e.g. "2:1 isometric" printed on the image, cut-off headlines, gibberish small print), Educational→isometric/blueprint diagrams with no trend link, and product-anatomy errors.
- **Image model is not the cause:** the same problems appear on both image models. Google also advises against tuning temperature or seed (Nano Banana 2.1 rejects them), so the fix has to go into the prompt text and the pipeline design.

**Architecture:**
- **Prompt text:** all changes live in `creative_agent/prompts.py`: the `IMAGE_PROMPT_GUIDE` guide plus the drafter, critic and finalizer instructions.
- **Style shortlist:** a small pure helper `creative_agent/style_shortlist.py` picks 6 distinct style families per session (stratified: 2 photographic, 3 illustrated, 1 graphic). `creative_agent/callbacks._set_initial_states` seeds them once into session state as `style_shortlist`, and the visual agents read it through an optional `{style_shortlist?}` token.
- **Who uses it:** `interactive_creative` reuses the same callbacks and prompts, so it gets the change automatically.

**Tech Stack:** Python 3.13, Google ADK prompt templating (`{key?}` optional state tokens), pytest, uv, ruff, ty.

**Conventions:**
- Branch `feat/image-diversity` from `origin/main`, using TDD per task with a commit per task.
- Conventional commits, and **never** add a `Co-Authored-By` line or any AI attribution.
- Don't stage `.agents/`, `.claude/` or `skills-lock.json`.
- `IMAGE_PROMPT_GUIDE` must never contain `{` or `}`; `tests/test_visual_intent_prompts.py` guards this.
- Gate: `uv run ruff format . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 4`.
- The first commit copies this plan to `docs/plans/2026-10-06-image-diversity.md`.

---

### Task 0: Branch + plan doc

**Step 1:**
```bash
git fetch origin && git worktree add -b feat/image-diversity /tmp/imgdiv origin/main && cd /tmp/imgdiv
cp /home/user/.claude/plans/comprehensive-codebase-review-swift-treasure.md docs/plans/2026-10-06-image-diversity.md
git add docs/plans/2026-10-06-image-diversity.md && git commit -m "docs(plans): image diversity fixes plan"
```

---

### Task 1: Style-family registry + shortlist picker (fix 2, code half)

**Files:**
- Create: `creative_agent/style_shortlist.py`
- Test: `tests/test_style_shortlist.py`

**Step 1: Write the failing tests**
```python
"""Per-session style shortlist: stratified, distinct, reproducible with a seeded RNG."""

import random

from creative_agent import prompts
from creative_agent.style_shortlist import (
    SHORTLIST_QUOTA,
    STYLE_GROUPS,
    format_shortlist,
    pick_style_shortlist,
)


def test_every_family_appears_in_the_guide():
    for families in STYLE_GROUPS.values():
        for family in families:
            assert family in prompts.IMAGE_PROMPT_GUIDE, family


def test_groups_are_disjoint_and_cover_quota():
    all_families = [f for fs in STYLE_GROUPS.values() for f in fs]
    assert len(all_families) == len(set(all_families))
    for group, n in SHORTLIST_QUOTA.items():
        assert len(STYLE_GROUPS[group]) >= n


def test_shortlist_is_six_distinct_and_stratified():
    picks = pick_style_shortlist(random.Random(7))
    assert len(picks) == sum(SHORTLIST_QUOTA.values()) == 6
    assert len(set(picks)) == 6
    for group, n in SHORTLIST_QUOTA.items():
        assert sum(p in STYLE_GROUPS[group] for p in picks) == n


def test_reproducible_with_seed_and_varies_across_seeds():
    assert pick_style_shortlist(random.Random(3)) == pick_style_shortlist(
        random.Random(3)
    )
    distinct = {
        tuple(sorted(pick_style_shortlist(random.Random(s)))) for s in range(30)
    }
    assert len(distinct) >= 15


def test_format_has_no_braces_and_lists_each_family():
    picks = pick_style_shortlist(random.Random(1))
    text = format_shortlist(picks)
    assert "{" not in text and "}" not in text
    for p in picks:
        assert p in text
```

**Step 2:** Run `GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/test_style_shortlist.py -v`. Expected: FAIL (`ModuleNotFoundError: creative_agent.style_shortlist`).

**Step 3: Implement** `creative_agent/style_shortlist.py`:
```python
"""Per-session style shortlist for the visual concept agents.

The prompt-writing model applies IMAGE_PROMPT_GUIDE's tone→style mapping so
literally that runs converge on the same few looks. Seeding each session with a
random, stratified shortlist of style families (drafted from in code, not by the
model) forces cross-run variety while the drafter still matches styles to tone.
Family names must match the IMAGE_PROMPT_GUIDE <STYLE_PALETTE> entries exactly.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

STYLE_GROUPS: dict[str, tuple[str, ...]] = {
    "photographic": (
        "Photoreal / editorial",
        "Cinematic film still",
        "Candid 35mm film photo",
    ),
    "illustrated": (
        "3D character render",
        "2D flat / vector cartoon",
        "Anime / manga",
        "Comic panel",
        "Watercolor / gouache",
        "Collage / mixed-media",
        "Retro / vaporwave",
    ),
    "graphic": (
        "Meme aesthetic",
        "Diecut sticker",
        "Isometric miniature world",
        "Minimalist negative-space",
    ),
}
SHORTLIST_QUOTA: dict[str, int] = {"photographic": 2, "illustrated": 3, "graphic": 1}


def pick_style_shortlist(rng: random.Random | None = None) -> list[str]:
    """Return 6 distinct style families: 2 photographic, 3 illustrated, 1 graphic."""
    rng = rng or random.Random()
    picks: list[str] = []
    for group, n in SHORTLIST_QUOTA.items():
        picks.extend(rng.sample(STYLE_GROUPS[group], n))
    rng.shuffle(picks)
    return picks


def format_shortlist(picks: Sequence[str]) -> str:
    """Render the shortlist for the {style_shortlist?} prompt token (brace-free)."""
    return "; ".join(picks)
```
(Task 3 renames "Isometric" to "Isometric miniature world" and adds "Candid 35mm film photo" to the palette. Until then `test_every_family_appears_in_the_guide` fails, so do Task 3's palette edit before running the full gate, or temporarily mark that test `xfail` and remove the mark in Task 3. Preferred: implement Task 1 Step 3, then make the Task 3 Step 3 palette edits, then run.)

**Step 4:** Run the test file. Expected: PASS once the palette contains the family names.

**Step 5:** `git add creative_agent/style_shortlist.py tests/test_style_shortlist.py && git commit -m "feat(creative_agent): stratified per-session style shortlist picker"`

---

### Task 2: Seed `style_shortlist` once per session (fix 2, wiring)

**Files:**
- Modify: `creative_agent/callbacks.py:61-76` (inside `_set_initial_states`, after the visual-intent `setdefault` loop)
- Test: `tests/test_callbacks.py` (add tests)

**Step 1: Failing tests**, appended to `tests/test_callbacks.py`, mirroring how existing tests there call `_set_initial_states` with a plain dict target:
```python
def test_style_shortlist_seeded_once(monkeypatch):
    from creative_agent import callbacks
    from creative_agent.style_shortlist import STYLE_GROUPS

    state: dict = {}
    callbacks._set_initial_states({}, state)
    shortlist = state["style_shortlist"]
    families = [f for fs in STYLE_GROUPS.values() for f in fs]
    assert sum(f in shortlist for f in families) == 6


def test_style_shortlist_not_clobbered():
    from creative_agent import callbacks

    state: dict = {"style_shortlist": "Comic panel; Diecut sticker"}
    callbacks._set_initial_states({}, state)
    assert state["style_shortlist"] == "Comic panel; Diecut sticker"
```
(Adapt the `source`/`state_init` handling to how the existing `_set_initial_states` tests in that file seed. If `seed_initial_state` returns early when already initialised, assert on a fresh dict.)

**Step 2:** Run `GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/test_callbacks.py -k style_shortlist -v`. Expected: FAIL (`KeyError: 'style_shortlist'`).

**Step 3: Implement** in `creative_agent/callbacks.py` (add `from creative_agent.style_shortlist import format_shortlist, pick_style_shortlist` at the top):
```python
    # Per-session random style shortlist (image diversity): the visual agents
    # pick their 4 styles from it, so runs don't converge on the same looks.
    # setdefault: a caller/test-provided shortlist (or a resumed session) wins.
    target.setdefault("style_shortlist", format_shortlist(pick_style_shortlist()))
```

**Step 4:** Run the tests. Expected: PASS.

**Step 5:** `git add creative_agent/callbacks.py tests/test_callbacks.py && git commit -m "feat(creative_agent): seed a per-session style shortlist"`

---

### Task 3: De-template the guide, cap in-image text, fix Educational (fixes 1 + 3)

**Files:**
- Modify: `creative_agent/prompts.py:10-69` (`IMAGE_PROMPT_GUIDE`)
- Test: `tests/test_image_prompt_guide.py` (new)

**Step 1: Failing tests:**
```python
"""IMAGE_PROMPT_GUIDE content rules (image diversity fixes 1 + 3)."""

import re

from creative_agent import prompts

G = prompts.IMAGE_PROMPT_GUIDE


def test_no_braces():
    assert "{" not in G and "}" not in G


def test_in_image_text_is_capped():
    assert "at most 2 of the 4 concepts" in G
    assert "6 words" in G
    assert "no small print" in G.lower()


def test_no_fill_in_templates_or_jargon():
    assert not re.search(r"of \[subject\]", G)  # palette is descriptors, not templates
    assert "2:1 iso grid" not in G
    assert "Do NOT copy" in G  # anti-verbatim-opener rule


def test_educational_maps_away_from_diagrams():
    line = next(l for l in G.splitlines() if l.startswith("- Educational"))
    assert "isometric" not in line.lower() and "blueprint" not in line.lower()
    assert "product hero" in line.lower()


def test_tone_mapping_is_a_preference_and_shortlist_aware():
    assert "this is the selection rule, not a suggestion" not in G
    assert "style shortlist" in G.lower()


def test_trend_motif_required():
    assert "trend motif" in G.lower()
```

**Step 2:** Run `GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/test_image_prompt_guide.py -v`. Expected: several FAIL.

**Step 3: Edit `IMAGE_PROMPT_GUIDE`.** Keep the section tags, the brace-free rule and the comment header. Make these edits:

- **CORE_PRINCIPLES, style bullet:** "NAME THE STYLE EXPLICITLY at the start, in your own words tied to this scene. Do NOT copy the palette wording as an opener."
- **CORE_PRINCIPLES, in-image text bullet** (replace the "The model renders LEGIBLE IN-IMAGE TEXT…" bullet):
  "In-image text is OPTIONAL and the most common way an image fails. Use it in at most 2 of the 4 concepts in a set. When used: one headline OR call-to-action only, 6 words or fewer, exact words in quotes, a named font vibe and a placement that differs from the other text concept. Never put words across the top by default. No small print, labels, captions on both top and bottom, setlists, spec callouts, UI screens full of text, or style/technical terms (e.g. never print the style name). Concepts without text should leave clean negative space for the platform's own caption."
- **New CORE_PRINCIPLES bullet:** "Every concept must show at least one concrete trend motif (an object, symbol, setting or gesture from the trend or the visual_direction motifs) so the image reads as on-trend without any text."
- **STYLE_PALETTE intro:** "Pick ONE primary style family per concept, from the run's style shortlist when one is given. These are descriptors, not templates: combine the cues with your own scene; never copy them as a sentence."
- **Convert each palette entry** from a fill-in template to `Family — when-to-use. Cues: …`. Keep the family names exactly as in `STYLE_GROUPS`. Add `Candid 35mm film photo` and rename `Isometric` to `Isometric miniature world`. For example:
  - `Photoreal / editorial — aspirational, premium, trust. Cues: real camera and lens, studio or natural light, shallow depth of field, a restrained palette.`
  - `Candid 35mm film photo — emotional, authentic, human. Cues: natural light, film grain, imperfect framing, real moments.`
  - `Isometric miniature world — playful systems, "a tiny world". Cues: tilted bird's-eye miniature diorama, soft shadows, tidy palette. A whimsical scene, NOT a technical diagram.`
  - Meme aesthetic: keep the sub-forms but note "one caption of at most 6 words (counts toward the text cap)".
  - Comic panel: "an optional speech bubble of at most 6 words".
- **TONE_TO_STYLE_MAPPING intro:** "Prefer these mappings when choosing among the families on the run's style shortlist (a preference, not a rule; the shortlist wins)." Remove "this is the selection rule, not a suggestion".
- **Educational line:** `- Educational / how-it-works tone -> a product hero with one short labelled callout, or a minimalist frame with one line of text; never a full technical diagram, blueprint, cutaway or exploded view.`
- **BUILDING_BLOCKS "In-image text & branding" bullet:** "only in at most 2 concepts per set (see the text rule); otherwise none."
- **Leave alone:** ASPECT_RATIO.

**Step 4:** Run `tests/test_image_prompt_guide.py tests/test_style_shortlist.py tests/test_visual_intent_prompts.py`. Expected: PASS.

**Step 5:** `git add creative_agent/prompts.py tests/test_image_prompt_guide.py && git commit -m "feat(creative_agent): de-template the image guide, cap in-image text, fix Educational mapping"`

---

### Task 4: Shortlist + across-set composition rule in the drafter, critic and finalizer (fix 2, prompt half)

**Files:**
- Modify: `creative_agent/prompts.py`:
  - `VISUAL_CONCEPT_DRAFTER_INSTR` (step 3 at ~line 400 and its CONTEXT block)
  - `VISUAL_CONCEPT_CRITIC_INSTR` (~486–533)
  - `VISUAL_CONCEPT_FINALIZER_INSTR` (~541–585)
- Test: `tests/test_visual_intent_prompts.py` (add a test class)

**Step 1: Failing tests:**
```python
class TestImageDiversityRules:
    def test_shortlist_token_in_concept_agents(self):
        for instr in (
            prompts.VISUAL_CONCEPT_DRAFTER_INSTR,
            prompts.VISUAL_CONCEPT_CRITIC_INSTR,
            prompts.VISUAL_CONCEPT_FINALIZER_INSTR,
        ):
            assert "{style_shortlist?}" in instr

    def test_composition_rule(self):
        for instr in (
            prompts.VISUAL_CONCEPT_DRAFTER_INSTR,
            prompts.VISUAL_CONCEPT_FINALIZER_INSTR,
        ):
            assert "at most ONE centred" in instr
            assert "camera distance" in instr

    def test_finalizer_enforces_diversity(self):
        assert (
            "MUST"
            in prompts.VISUAL_CONCEPT_FINALIZER_INSTR.split("Style Diversity")[1][:400]
        )

    def test_text_cap_checked_by_critic_and_finalizer(self):
        for instr in (
            prompts.VISUAL_CONCEPT_CRITIC_INSTR,
            prompts.VISUAL_CONCEPT_FINALIZER_INSTR,
        ):
            assert "at most 2" in instr
```

**Step 2:** Run `GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/test_visual_intent_prompts.py -k Diversity -v`. Expected: FAIL.

**Step 3: Edit the three instructions.**

- **Drafter step 3:** "Choose 4 DIFFERENT `visual_style` families from <style_shortlist> (when non-empty), matching each ad copy's tone via the guide's mapping preference. When <user_style_preference> is non-empty it overrides the shortlist."
- **Drafter, new step 4 "Composition variety (across the set)":** "Give each concept a different hero placement and camera distance: choose from centred hero, off-centre rule-of-thirds, small subject in a wide environment, extreme close-up detail, top-down flat lay, over-the-shoulder POV, environmental portrait. At most ONE centred product hero per set. In-image text in at most 2 concepts (see the guide), never both placed at the top. Every concept shows a trend motif." Renumber the steps that follow.
- **Drafter CONTEXT:** add
  ```
  <style_shortlist>
  This run's style shortlist (choose 4 distinct families from it). When empty, use the guide's palette.
  {style_shortlist?}
  </style_shortlist>
  ```
- **Critic:** add a criterion "**Set-level checks:** families come from <style_shortlist> and are all different; at most ONE centred hero; text in at most 2 concepts, 6 words or fewer, no small print or style terms; every concept has a trend motif. Fix violations by rewriting the weakest concept." Add the same `<style_shortlist>` CONTEXT block.
- **Finalizer step 3** (replace) "**Style & Composition Diversity (MUST):**":
  "The final set MUST have 4 different `visual_style` families (from <style_shortlist> when non-empty), at most ONE centred hero, varied camera distance, and in-image text in at most 2 concepts. If a rule is violated, re-style or re-compose the weakest concept (judged by its `critique_summary`) and update its prompt. Exception: when <user_style_preference> is non-empty, keep that family and vary lighting and composition instead. Otherwise carry `visual_style` and `aspect_ratio` through unchanged."
  Add the same `<style_shortlist>` CONTEXT block.

**Step 4:** Run `tests/test_visual_intent_prompts.py tests/test_interactive_prompts.py`. Expected: PASS.

**Step 5:** `git add creative_agent/prompts.py tests/test_visual_intent_prompts.py && git commit -m "feat(creative_agent): shortlist + across-set composition rules in the visual agents"`

---

### Task 4b: Guarantee every image prompt references the trend AND the product (code-enforced)

**Why:** Each concept already carries `trend_visual_link`, a sentence *describing* the trend link (`creative_agent/schemas.py:121`). But nothing checks that the trend reaches `image_generation_prompt`, the only text the image model sees. A concept can have a convincing trend sentence while its prompt renders a plain product diagram (e.g. the Oct 6 "Anatomy of the Zero-Compromise Coil Split" pickup diagram). There's also an earlier period when creatives had nothing to do with the trend or the campaign subject. Prompt wording alone (fix 3's "trend motif" rule) is a request; this task makes it a guarantee:
1. **New `trend_motif` field:** a short concrete *visual* noun phrase drawn from the trend (e.g. "a ballot box", "wedding confetti and a bouquet"), separate from the explanatory sentence.
2. **Prompt rule:** the drafter must write that motif verbatim into the image prompt, and the critic and finalizer must keep it. Drop the drafter's "*subtly* reference the trend" wording, which licenses a near-zero link.
3. **Deterministic repair in code:** an `after_agent_callback` on the finalizer (and on interactive's `visual_concept_reviser`, which also writes `final_visual_concepts`) checks each final concept. If `trend_motif` doesn't appear in `image_generation_prompt`, or the prompt never names `{target_product}`, it appends a short scene sentence. It logs a warning so the miss is visible:
   - "The scene visibly includes {motif}."
   - "The {target_product} is clearly visible and recognizable."

   The image model then always receives both the trend and the product, even when the LLM drifts.

**Files:**
- Modify: `creative_agent/schemas.py`: add `trend_motif: str = Field(default="", description="A short concrete VISUAL element from the trend (noun phrase, ≤8 words) that MUST appear verbatim in image_generation_prompt.")` to the draft (~line 121), critique (~158) and final (~212) concept models. Default `""` keeps old session states valid.
- Modify: `creative_agent/prompts.py`:
  - Drafter step 2: replace "Leverage or subtly reference the trending topic" with "Visibly reference the trend {target_search_trends}: pick a concrete `trend_motif` (from the visual_direction motifs when present) and write it verbatim into the prompt."
  - Critic and finalizer: "keep `trend_motif` and its verbatim presence in the prompt".
- Create: `creative_agent/concept_guard.py`, a pure function `ensure_trend_and_product(concepts: list[dict], target_product: str) -> tuple[list[dict], list[str]]` that returns the repaired concepts plus the warnings. Wire it in `creative_agent/callbacks.py` as `ensure_trend_and_product_callback(callback_context)`: read `final_visual_concepts` from state (dict or JSON str), repair, and write it back.
- Modify: `creative_agent/agent.py` (finalizer agent around `output_key="final_visual_concepts"`, ~line 483) and `interactive_creative/agent.py` (`visual_concept_reviser`): add `after_agent_callback=callbacks.ensure_trend_and_product_callback`. Compose with any existing after-callback as a list.
- Test: `tests/test_concept_guard.py`

**Step 1: Failing tests** (`tests/test_concept_guard.py`):
```python
from creative_agent.concept_guard import ensure_trend_and_product

PRODUCT = "SE CE24 Electric Guitar"


def _c(prompt, motif="a ballot box"):
    return {
        "concept_name": "x",
        "trend_motif": motif,
        "image_generation_prompt": prompt,
    }


def test_untouched_when_motif_and_product_present():
    c = _c("A cut-paper collage of an SE CE24 Electric Guitar beside a ballot box.")
    out, warns = ensure_trend_and_product([c], PRODUCT)
    assert (
        out[0]["image_generation_prompt"] == c["image_generation_prompt"]
        and warns == []
    )


def test_appends_missing_motif():
    out, warns = ensure_trend_and_product(
        [_c("A studio photo of an SE CE24 Electric Guitar.")], PRODUCT
    )
    assert "a ballot box" in out[0]["image_generation_prompt"]
    assert any("trend_motif" in w for w in warns)


def test_appends_missing_product():
    out, warns = ensure_trend_and_product(
        [_c("A watercolor of a ballot box in a plaza.")], PRODUCT
    )
    assert PRODUCT in out[0]["image_generation_prompt"]
    assert any("product" in w for w in warns)


def test_case_insensitive_and_empty_motif_only_warns():
    out, _ = ensure_trend_and_product(
        [_c("an se ce24 electric guitar and A BALLOT BOX")], PRODUCT
    )
    assert out[0]["image_generation_prompt"].count("ballot") == 1
    out, warns = ensure_trend_and_product(
        [_c("an SE CE24 Electric Guitar", motif="")], PRODUCT
    )
    assert any("empty trend_motif" in w for w in warns)
```
Also add `test_trend_motif_field_on_concept_models` to `tests/test_image_prompt_guide.py`, asserting that the three schema classes expose `trend_motif`. Also add a prompt test that `"subtly reference"` no longer appears in the drafter and that `"trend_motif"` appears in the drafter, critic and finalizer.

**Step 2:** Run `GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/test_concept_guard.py -v`. Expected: FAIL (module missing).

**Step 3: Implement** `creative_agent/concept_guard.py`:
```python
"""Deterministic last-line guard: every final image prompt names the trend motif
and the product, so the image model can't render a trend-less / product-less ad."""

from __future__ import annotations

import copy


def ensure_trend_and_product(
    concepts: list[dict], target_product: str
) -> tuple[list[dict], list[str]]:
    out, warns = [], []
    for c in concepts:
        c = copy.deepcopy(c)
        prompt = str(c.get("image_generation_prompt", "")).rstrip()
        low = prompt.lower()
        motif = str(c.get("trend_motif", "")).strip()
        name = c.get("concept_name", "?")
        if not motif:
            warns.append(f"{name}: empty trend_motif")
        elif motif.lower() not in low:
            prompt += f" The scene visibly includes {motif}."
            warns.append(f"{name}: trend_motif missing from prompt, appended")
        if target_product and target_product.lower() not in low:
            prompt += f" The {target_product} is clearly visible and recognizable."
            warns.append(f"{name}: product missing from prompt, appended")
        c["image_generation_prompt"] = prompt
        out.append(c)
    return out, warns
```
Then add the callback wrapper in `creative_agent/callbacks.py`. It parses the state value (a dict with `visual_concepts`, or a JSON string of one), calls the guard, writes the same shape back, and logs each warning with `logging.warning`. Wire it on the finalizer and the interactive reviser. Add a small test in `tests/test_callbacks.py` that the callback rewrites the state for both dict and JSON-string shapes.

**Step 4:** Run `tests/test_concept_guard.py tests/test_callbacks.py tests/test_image_prompt_guide.py tests/test_pipeline_structure.py`. Expected: PASS. The pipeline-structure tests may assert callback wiring; update them if needed.

**Step 5:** `git add -A creative_agent interactive_creative tests && git commit -m "feat(creative_agent): guarantee every image prompt shows the trend motif and the product"`

**Limit (honest):** this guarantees the trend and the product are *in the prompt*. It can't prove the rendered pixels show them. Checking the pixels is follow-up fix 5, a vision-model check per image. The `creative_evals` trend-connection score still flags weak links, but it is text-only.

---

### Task 5: Docs, full gate, PR

**Files:**
- Modify:
  - `CLAUDE.md`: in the "Image-generation prompt guidance…" paragraph add one sentence: per-session `style_shortlist` (`creative_agent/style_shortlist.py`, seeded in `callbacks._set_initial_states`), the 2-of-4 / 6-word text cap, descriptors not templates, and the across-set composition rule.
  - `tests/README.md`: list the new test files.

**Step 1:** Edit the docs.

**Step 2:** Run `uv run ruff format . && uv run ruff check . && uv run ty check && GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 4`. Expected: all pass, about 1521 plus the new tests. `requirements.txt` stays unchanged.

**Step 3:** Commit, push `feat/image-diversity`, and open a PR. If no checks appear, dispatch `python-ci.yml` manually.

---

## Verification (after merge + deploy)
1. **Unit:** the gate above, in CI.
2. **Live:** deploy the api (the visual agents run in-process for frontend runs). Run 3–4 creative runs on a couple of different briefs, then re-run the measurement scripts from the investigation (`/tmp/imgq_scripts/stats.py`, `ngrams.py`, `headline.py`; regenerate from the run galleries if `/tmp` was cleared). Compare against the Oct 5–6 baseline:
   - **Quoted in-image text** 96% → ≤50%
   - **Text "across the top"** 68% → ≤25%
   - **Centred hero** 46% → ≤25%
   - **Most common template opener** in 57% of runs → ≤15%
   - **Styles across runs:** the 4 chosen families should differ visibly between runs.
3. **Visual spot check:** view the PNGs from those runs. Expect no garbled small print, no style jargon printed on images, no Educational blueprints, and a trend motif in every image.
   - Also grep the api logs for the guard's `trend_motif missing` / `product missing` warnings. A few are fine, since they mean the guard repaired a prompt. A high rate means the drafter wording needs tightening.
   - Check that `trend_motif` appears verbatim in 100% of the final prompts in the gallery HTML.
4. **Then:** redeploy the creative_agent and interactive_creative engines, with a smoke test, so the batch path gets the change.
5. **If the bad-image rate is still high:** plan follow-up fix 5, a pixel-level check that re-renders the worst image.
