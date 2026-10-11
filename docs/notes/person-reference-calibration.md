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
  (the photographic group; illustrated and graphic families stay excluded until calibrated —
  four illustrated families were added on 2026-10-10, see below).
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

## Illustrated styles (2026-10-10)

Same script, model, likeness check and the same 2 consented adult photos, now 4 illustrated
families (3D character render, Comic panel, 2D flat / vector cartoon, Collage / mixed-media)
× 2 framings (close, mid) × with/without `ALLOW_ADULT`: 32 renders.

| Measure | Result |
|---|---|
| Errors | 0 / 32 |
| Safety-filter blocks | 0 / 32 |
| Likeness (vision check), overall | 94% |
| 3D character render | 88% |
| Comic panel | 100% |
| 2D flat / vector cartoon | 88% |
| Collage / mixed-media | 100% |
| By framing | close 94%, mid 94% |
| Manual spot check | comic and collage: clear likeness; 3D and flat cartoon: recognisable but looser (stylised) |

The 2 misses: a flat-cartoon mid shot with minimal facial detail, and a 3D close-up with
generic cartoon features.

**Decision:** all four families clear the 70% bar and are added to the default
`PERSON_SAFE_STYLES` (now 7 families). The hard-coded "never in comic" casting rule is gone
(meme, isometric and product-only stay refused; the safe-style list decides comic). When the
safe styles include an illustrated family, the casting rules add one line asking for the
person's distinctive features (face shape, hairline, hair colour, skin tone, facial hair,
glasses) rather than a generic cartoon face.

**Caveats:** likeness is looser in 3D and flat cartoon (the misses above); the image-QA
likeness check and its bounded re-render remain the backstop. Same small, homogeneous sample
as the photographic spike (2 adult men with light skin, one setting), with the same
Gemini-judging-Gemini limitation. Narrow the list with `PERSON_SAFE_STYLES` if QA likeness
failures rise for a style.

## Photo guidance (2026-10-11)

Two personalised swaps on 2026-10-11 looked soft and drifted from their originals; both
used the 240×240 px photo. Registration now asks for one good headshot and enforces a size:

- **Minimum size:** the photo's shorter side must be at least `PERSON_PHOTO_MIN_SIDE` px
  (default 512; 400 `photo_too_small` below it). Below 1024 px the person is flagged
  `low_resolution` and the People page says previews may look softer. Existing records get
  their size read lazily on the next `GET /person-refs/{user}`, so the 240 px photo now
  shows the notice.
- **Guidance shown above the form** (`PHOTO_GUIDANCE` in `frontend/src/lib/person-refs.ts`):
  - one person, a front-facing head-and-shoulders photo, face filling about a third of the frame;
  - even light, neutral background, no sunglasses, hats or heavy filters;
  - at least 1024 px on the shorter side;
  - not a collage, character sheet or group photo, and no text on the image.

**Why not a character turnaround sheet?** The image model takes the person reference as
one picture, so a multi-view sheet (front / side / back of the same person) risks:

- copying the sheet's multi-figure layout or its "FRONT" / "SIDE" labels into the ad;
- diluting the face, since each view is small (a 1024 px sheet with four views gives each
  face a fraction of the pixels a headshot would);
- confusing which figure is "the person".

A single clear front headshot gave the best likeness in the calibration above. The model's
documented multi-image character consistency works best with *separate* reference images,
which v1 deliberately doesn't support (one photo per consent). So the guidance asks for one
headshot and explicitly says "not a collage or character sheet".
