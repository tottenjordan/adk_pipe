# Signature-Item Compromise Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: `subagent-driven-development`.
> - **Three PRs in parallel**, one implementer each: S1, S2 and S3 below. Their files don't overlap and none depends on another.
> - **Then** one deploy (Task 2) and monitoring (Task 3).
> - **Plan copy:** PR S1 copies this plan to `docs/plans/2026-10-15-signature-item-compromise.md`.
> - Review with `code-review-and-quality`, and merge only after CI is green.
> - **Critical path:** about 30 min for the PRs in parallel, then about 25 min to deploy.

## PR split (parallel; files don't overlap)

| PR | Branch | Files (exclusive) | Change |
|---|---|---|---|
| **S1: casting guardrail** | `feat/cast-signature-item` | `creative_agent/prompts.py` (`PERSON_IDENTITY_GUARDRAILS` only), `tests/test_visual_concept_prompts.py`, CLAUDE.md (the Person casting bullet's guardrail phrase), the plan copy | Task 1 Step 3, first bullet |
| **S2: image QA definition** | `feat/qa-signature-item` | `creative_agent/image_qa.py` (Field description, cast-only instruction, correction text), `tests/test_image_qa_person.py`, the image-QA prompt tests, `tests/fixtures/render_parity*` (only if the golden records that text), `docs/notes/creative-quality-gates.md` | Task 1 Step 3, second bullet |
| **S3: safer-angle visual rule** | `feat/safer-angle-signature-props` | `creative_agent/safer_angle.py` (`visual_rules` only), `tests/test_safer_angle.py` | Task 1 Step 3, third bullet |

- **Each PR on its own:** write the failing tests (Task 1 Step 1, the part for its own surface), run them, implement, run the gates in a clean env, do its own mutation check, then open the PR.
- **Safe to merge in any order:** S1 alone allows the item in prompts but QA would still flag it; S2 alone only loosens QA. Deploy only after all three merge.
- **After the third merge:** run the full suite on `main`, then Task 2.

**Goal:** let creatives use a trend person's signature item, such as Tupac's paisley bandana, as a respectful tribute nod. The hard lines on likeness, impersonation, endorsement and IP stay as they are.

**Architecture:** text-only rule changes in three places, kept consistent:
- the casting guardrail the agents read;
- image QA's "person shown as a public figure" definition and correction, so QA doesn't force re-renders of what the prompt now allows;
- the safer-angle visual rule, which explicitly allows signature objects as motifs.

There are no code-path changes. Monitoring uses the existing logs and state.

**Tech stack:** prompt constants in `creative_agent/prompts.py` and `creative_agent/safer_angle.py`, the image QA schema and instruction in `creative_agent/image_qa.py`, pytest.

---

## Context

**What the user saw:** in their Tupac / Clavinet run, the "forward-tied paisley bandana" motif was a great creative idea. They asked to remove the "forbid signature attire" rules where it's safe.

**Where those rules live today:**
- `PERSON_IDENTITY_GUARDRAILS` (`creative_agent/prompts.py:878-890`) says "no costume, signature attire, uniform or vestments of the real person", for **cast concepts only**, meaning concepts that use the user's consented person.
- Image QA flags `person_as_public_figure` whenever a cast person wears "that figure's costume, signature attire, uniform or vestments". This appears in three places in `creative_agent/image_qa.py`:
  - the schema Field description at :181-188;
  - the cast-only instruction line at :533-539;
  - the correction text at :323-326.
- **Uncast concepts** have no attire rule. The drafter is told to use "specific signature imagery of the trend" (`prompts.py:1205`), which is how the bandana appeared.

**Why not remove everything:**
- **No model API failures:** every image step is fail-soft, so these rules can't cause them. The costs are quieter.
- **Safety filter:** person renders use `BLOCK_PROMINENT_PEOPLE`, so a consented person recreated in a celebrity's full look can be blocked. The render then falls back to one without the person.
- **Re-renders:** QA's public-figure rule triggers re-renders.
- **Judge:** its person and respectful-reference gates can fail the creative.
- **Legal:** dressing a real person *as* a famous person reads as impersonation.

**Compromise (approved by the user):**
1. **Uncast concepts:** signature objects or attire may appear as props or styling cues, e.g. a paisley bandana tied on a mic stand, draped over the instrument, or worn by an anonymous figure who does not resemble the real person.
2. **Cast concepts:** the consented person may wear or hold **one** signature item as a tribute nod, styled as themselves. Still banned:
   - a full recreation of the real person's look (outfit together with their pose, setting or styling);
   - features copied from them;
   - anything implying they *are* that person or are endorsed by them.
3. **Unchanged hard lines:**
   - no likeness of the real person (safety floor, image QA `real public figure likeness`, judge);
   - never presented as the real person;
   - no implied endorsement (endorsement lexicon);
   - no estate-owned imagery;
   - no team logos, wordmarks, mascots or player names (timely rules).

## Task 1: rule-text changes (one PR, ~30 min)

**Files:**
- Modify: `creative_agent/prompts.py` (`PERSON_IDENTITY_GUARDRAILS`)
- Modify: `creative_agent/image_qa.py` (the Field description, the cast-only instruction line, the correction text)
- Modify: `creative_agent/safer_angle.py` (`visual_rules`)
- Test: `tests/test_visual_concept_prompts.py` (~:176-191), `tests/test_image_qa_person.py`, `tests/test_safer_angle.py`, plus any image-QA prompt test
- Docs: CLAUDE.md (the Person casting bullet's guardrail phrase), `docs/notes/creative-quality-gates.md` (if it describes the public-figure rule)

**Step 1: write the failing tests**
- **Casting guardrails text:**
  - contains "may wear or hold one signature item";
  - contains "never a full recreation of the real person's look";
  - still contains "never a likeness of the trending real person" and "implying they are that person or are endorsed by them";
  - no longer contains "no costume, signature attire, uniform or vestments".
- **Image QA (cast instruction and Field description):** `person_as_public_figure` is true only for a full recreation of the person's look (outfit together with their pose, setting or styling) or a pose, setting or caption implying the hero is that figure. "A single signature item worn as a tribute nod is not" appears in the text.
- **QA correction text** for "person shown as a public figure": "keep at most one signature item as a tribute nod; no full recreation of the real person's look".
- **Safer-angle visual rule:**
  - allows signature objects or attire as props or styling cues;
  - still says "never a likeness of the real person" and "never estate-owned imagery";
  - the floor phrases are intact (existing test).

**Step 2: run the tests and check they fail**
`GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/test_visual_concept_prompts.py tests/test_image_qa_person.py tests/test_safer_angle.py -q`

**Step 3: implement**
- **`PERSON_IDENTITY_GUARDRAILS`:** replace "no costume, signature attire, uniform or vestments of the real person, and no pose, setting or caption implying they are that person or are endorsed by them" with:
  > they may wear or hold one signature item of the real person (e.g. a bandana) as a tribute nod, styled as themselves, but never a full recreation of the real person's look (outfit together with their pose, setting or styling), no features copied from them, and no pose, setting or caption implying they are that person or are endorsed by them

  Keep the rest unchanged. It must stay brace-free.
- **image_qa.py:**
  - **Field description and cast-only instruction:** redefine `person_as_public_figure` as "the cast person is presented as a specific real public figure: a full recreation of that figure's look (outfit together with their pose, setting or styling) or a pose, setting or caption implying the hero is that figure; a single signature item worn as a tribute nod, and ordinary fan clothing in team colours, are not".
  - **Correction text:** "show the person as themselves, not as a public figure: at most one signature item as a tribute nod, no full recreation of the real person's look and no pose, setting or caption implying they are that figure."
- **`safer_angle.visual_rules`:** after the motifs bullet, add one bullet:
  > signature objects or attire of the real person (e.g. a bandana) may appear as props or styling cues, on the set, the instrument or an anonymous figure who does not resemble them; never a likeness.

**Step 4: check that defaults outside the change are unchanged**
- **Byte-identical sha pins** (`tests/test_timely_rules.py` and the safer-angle pins) must stay green. `PERSON_CASTING_RULES` and the safer-angle texts reach prompts through `{…?}` state tokens, which the pins strip.
- **`tests/test_render_parity.py` (golden):** the QA response schema's Field description changes. If the golden records the schema or QA request text, re-record **only** that text deliberately and say so in the PR body. Non-cast render requests must be otherwise identical.
- Run the full gates: `uv run ruff format --check . && uv run ruff check . && uv run ty check && env -u GOOGLE_GENAI_USE_ENTERPRISE -u GOOGLE_GENAI_USE_VERTEXAI GOOGLE_CLOUD_PROJECT=test-project uv run pytest tests/ -q -n 8`.
- **Mutation check:** restore the old "no costume, signature attire…" phrase, and a guardrail test must fail. Restore, then confirm `git diff --exit-code` is clean.

**Step 5: commit and open a PR**
- PR title: `feat(creative): allow one signature item as a tribute nod; keep likeness and impersonation lines`.
- No attribution, and no merge before CI passes.

## Task 2: deploy (~25 min)

1. Engines via Cloud Build, creative and interactive (`_SMOKE=true`). Then `engine_handoff apply --update-worker`, which moves the worker's traffic pin itself.
2. api: idle check, `--no-traffic`, then pin traffic.
3. Web and share don't change.
4. Delete the previous engines after a clean smoke test.

## Task 3: monitoring (no new code)

These signals already exist:
- **QA public-figure failures:** the log line `Image QA failed for '<concept>' ([... 'person shown as a public figure' ...]); re-rendering` (`creative_agent/image_tools.py:763`), plus `generated_images[c].qa.person_as_public_figure`.
- **Safety-filter blocks:** `person_reference_rejected` and the `person_reference__issues` run note ("Person reference" on the results page).
- **Judge:** a `person_depicted_respectfully` / `safer_angle_respected` failure in the eval report.

**Query to run after deploy, and again after the next ~5 cast runs:**
```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="trend-trawler-api" AND (textPayload:"person shown as a public figure" OR textPayload:"person_reference_rejected" OR textPayload:"Person reference")' --freshness=7d --limit 50 --format='value(timestamp,textPayload)'
```

- **Baseline:** compare against the 7 days before deploy.
- **Rollback trigger:** if cast concepts get blocked by the safety filter or flagged "shown as a public figure" noticeably more often (rough guide: more than 1 in 4 cast concepts), revert this PR's guardrail text, which is a one-commit revert.
- Record the result in memory.

## Verification

- **Unit tests:** the new rule texts are present; the hard-line phrases (likeness, impersonation, endorsement, estate imagery) are still present; the byte-identical pins are green; any change to the render-parity golden is deliberate and explained.
- **Live (optional, user-driven):** a creative run with a consented person and the Tupac trend with the safer angle on.
  - A cast concept may show the person with a paisley bandana, styled as themselves.
  - QA doesn't flag "public figure", and the judge's person gate passes.
  - An uncast concept may show the bandana as a prop.
  - No likeness of Tupac.

**Estimated wall clock:** about 30 min for the PR, about 25 min to deploy, and monitoring passively over the next few days.
