# Person reference calibration (2026-10-09)

Gate for the person-reference plan (`docs/plans/2026-10-09-person-reference.md`, PR 0).
Script: `experiments/person_reference/calibrate.py`; summary: `experiments/person_reference/results/summary.md`
(renders and the per-render CSV stay gitignored because they show real people).

## Setup

- Model: `gemini-nano-banana-2.1` (image), `gemini-3.8-flash` (likeness check, same model as image QA).
- 2 consented adult photos (one 600×600, one only 240×240), 3 photographic styles
  (Candid 35mm film photo, Photoreal / editorial, Cinematic film still) × 2 framings (close, mid),
  each rendered with and without `ImageConfig(person_generation="ALLOW_ADULT")`: 24 renders, 4:5.

## Results

| Measure | Result |
|---|---|
| Errors | 0 / 24 |
| `ALLOW_ADULT` accepted by Nano Banana 2.1 | yes (no API error, no change in block rate) |
| Safety-filter blocks (false positives on ordinary photos) | 0 / 24 |
| Likeness (vision check) | 100% in every style and framing |
| Manual spot check | 2 renders (one per person, incl. a mid shot from the 240 px photo) clearly the same person |

## Decisions

- **Go.** Likeness and block rate are well inside the go rule (likeness ≥ 60%, blocks ≤ 20%).
- `PERSON_SAFE_STYLES` = Candid 35mm film photo, Photoreal / editorial, Cinematic film still
  (the photographic group; illustrated and graphic families stay excluded until calibrated).
- `MAX_CAST_CONCEPTS` = 2 (unchanged: set diversity, not likeness, is the limit).
- Close-up framing is **not** required (mid shots held likeness), but the face must stay clearly visible.
- Send `person_generation="ALLOW_ADULT"` on person renders.

## Caveats

- Small sample: 2 people, both adult men with light skin, one setting (a café). Likeness may be
  lower for other faces, group scenes, profile views or busy compositions; keep the image-QA
  likeness check and re-render in the product (PR 2) and watch its failure rate.
- The likeness verdict comes from a Gemini model judging a Gemini render; the manual spot check
  agreed, but it is not an independent face-recognition measure (deliberately: no biometrics).
- Celebrity/minor filter behaviour was not exercised (no such photos by design).
