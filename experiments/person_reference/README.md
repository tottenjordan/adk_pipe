# `person_reference/` — calibration and person-swap spikes

Two spikes live here: `calibrate.py` (below) and `swap_compare.py`
([Person swap spike](#person-swap-spike-swap_comparepy)).

PR 0 of [the person reference plan](../../docs/plans/2026-10-09-person-reference.md)
(background: `PERSON_REFERENCE_RESEARCH.md` §5, phase 0). Before any product code
lets agents cast a real person from a photo, this spike measures, on our own image
model (`config.image_gen_model`, `gemini-nano-banana-2.1` @ `global`):

- **Likeness:** does the render still look like the person in the photo, by style
  and framing? A vision verdict on the image-QA model (`config.image_qa_model`):
  `{same_person, reason}`.
- **`person_generation=ALLOW_ADULT`:** does the API accept it for this model, and
  does it change the block rate?
- **Filter false positives:** how often ordinary adult photos get blocked
  (`prompt_feedback.block_reason`, a safety `finish_reason`, or no image).

By default the grid is 3 photographic style families (`Candid 35mm film photo`,
`Photoreal / editorial`, `Cinematic film still`; `--styles` picks others) × 2 framings (hero close-up, mid
shot) × each photo. Every concept is rendered twice (with and without
`ALLOW_ADULT`), so 2 photos give 24 renders, about 12–15 min at the image model's
~2 images/min quota.

## Consent (required)

Use photos only of **adult teammates who agreed in writing** to their photo being
used for this test. Never use photos of minors, public figures, or anyone who
hasn't agreed. Record who agreed and when, outside the repo.

## Upload the photos

Each photo must sit under the bucket's `person-refs/` prefix (the script refuses
anything else):

```bash
BUCKET=$GOOGLE_CLOUD_STORAGE_BUCKET
gcloud storage cp photo.jpg gs://$BUCKET/person-refs/<slug>/
```

Use a clear, front-facing photo (JPEG, PNG or WebP, ≤ 10 MB). The spike doesn't
check the resolution, but the product does: the consent registry
(`runserver/person_refs.py`) refuses a photo whose shorter side is below
`PERSON_PHOTO_MIN_SIDE` (default 512 px, clamped 256–4096) with 400
`photo_too_small`, and flags anything under 1024 px as `low_resolution`. Use a
photo of at least 1024 px so the spike matches what users are told to upload.

## Run

```bash
unset VIRTUAL_ENV
# Preview the grid (no API calls):
PYTHONPATH="$PWD" uv run python -m experiments.person_reference.calibrate \
  --photos gs://$BUCKET/person-refs/<slug-a>/a.jpg gs://$BUCKET/person-refs/<slug-b>/b.jpg \
  --dry-run
# Live run (needs ADC + GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_STORAGE_BUCKET from .env):
PYTHONPATH="$PWD" uv run python -m experiments.person_reference.calibrate \
  --photos gs://$BUCKET/person-refs/<slug-a>/a.jpg gs://$BUCKET/person-refs/<slug-b>/b.jpg
```

Options: `--out DIR` (default `experiments/person_reference/results`), `--pace SECS`
(sleep between renders, default 31 for the 2 images/min quota), `--styles FAMILY ...`
(canonical STYLE_PALETTE family names from `creative_agent/style_shortlist.py`
`ALL_FAMILIES`, quoted; default the 3 photographic families; an unknown name exits 1
and lists the valid ones), `--framings close|mid ...` (default both).

Non-photographic families get their own concept template: the same café coffee
scene with "the person in the person reference image" as the hero, rendered "in the
style of <family>" with the family's `IMAGE_PROMPT_GUIDE` palette descriptor. The
summary lists the styles that were run. To test whether more families could join
`PERSON_SAFE_STYLES` (e.g. the illustrated ones):

```bash
PYTHONPATH="$PWD" uv run python -m experiments.person_reference.calibrate \
  --photos gs://$BUCKET/person-refs/<slug-a>/a.jpg gs://$BUCKET/person-refs/<slug-b>/b.jpg \
  --styles "3D character render" "Comic panel" "2D flat / vector cartoon" "Collage / mixed-media" \
  --out experiments/person_reference/results-illustrated
```

(4 styles × 2 framings × 2 photos × 2 renders = 64 renders, about 33 min at the
default pace; add `--framings close` to halve it.) A family belongs in
`PERSON_SAFE_STYLES` only when its likeness clears the same 70% bar.

If the API rejects `person_generation` (an exception on the first `ALLOW_ADULT`
render), the row records `allow_adult_unsupported`, the remaining `ALLOW_ADULT`
renders are skipped, and the default renders continue.

## Results

Written to `results/` (gitignored, except `summary.md`):

- `results.csv`: one row per render (style, framing, photo URI, `allow_adult`,
  `block_reason`, `error`, image file, `likeness`, `likeness_reason`).
- `summary.json` / `summary.md`: likeness by style and framing, block rate overall
  and with/without `ALLOW_ADULT`, false-positive blocks, person-safe styles and the
  rule-of-thumb decision. `summary.md` has no photo URIs and is the only file to
  commit.
- The renders themselves (`NN_<style>_<framing>_<photo>_<adult|default>.png`). They
  show real people: never commit or share them.

## Go/no-go rule (from the plan)

- **No-go** if likeness is under **60% in every style**, or ordinary photos are
  **blocked more than 20%** of the time. Stop and report.
- Otherwise **go**: `PERSON_SAFE_STYLES` = the families with **≥ 70%** likeness;
  also decide `MAX_CAST_CONCEPTS` (default 2) and whether close-up framing is
  required.

The script's `decision` field applies this rule mechanically; record the final call
and the numbers in `docs/notes/person-reference-calibration.md` (Task 0.2).

## Afterwards

Delete the test photos and the local renders:

```bash
gcloud storage rm --recursive gs://$BUCKET/person-refs/<slug>/
rm -f experiments/person_reference/results/*.png experiments/person_reference/results/*.jpeg
```

## Person swap spike (`swap_compare.py`)

PR 0 of [the faithful person swaps plan](../../docs/plans/2026-10-11-faithful-person-swaps.md).
For each concept × photo × mode × repeat it makes a personalised variant of a
finished concept and compares it with the run's original render:

- **regenerate**: the old variant path, a fresh render from the concept's text prompt
  + the person photo (`render_concept`, `ALLOW_ADULT`, QA off, no person-less
  fallback).
- **edit**: the original render + the person photo with `EDIT_INSTRUCTION`
  ("replace only the person"), aspect ratio from the original's size. The edit
  request now lives in `creative_agent/person_edit.py` and is re-exported here.

Each output gets one comparison vision call on the image-QA model (original +
variant → `same_layout`, `same_text`, `same_graphics`, `same_product`,
`only_person_changed`, `differences`) plus the image check's likeness verdict.

```bash
# Session JSON = the api's GET /apps/{app}/users/{u}/sessions/{id}
PYTHONPATH="$PWD" uv run python -m experiments.person_reference.swap_compare \
  --sessions s1.json s2.json --concepts "Concept A" "Concept B" \
  --photos gs://$BUCKET/person-refs/<slug>/a.jpg gs://$BUCKET/person-refs/<slug>/b.jpg
```

Options: `--modes regenerate edit` (default both), `--n` repeats (default 2),
`--out DIR` (default `experiments/person_reference/results/swap`), `--pace SECS`
(default 31).

Output: `results.csv`, `summary.json` and `summary.md` (rates per mode and photo,
photos labelled `photo1`, `photo2`, … and never a URI) plus the renders. Rule: **go**
when edit fidelity ≥ regenerate + 30% and edit likeness ≥ 80%. The recorded run
(`results/swap/summary.md`) was a go: edit fidelity 75% vs 0% for regenerate,
likeness 83% vs 100%, which is why variants now edit the original render (write-up:
[`docs/notes/person-swap-edit-mode.md`](../../docs/notes/person-swap-edit-mode.md)).
Delete the renders and the CSV afterwards; they show real people.

The pure core (`parse_comparison`, `summarise`, `decision`, `summary_markdown`,
`nearest_aspect_ratio`, `edit_contents`) is tested in `tests/test_swap_compare.py`
(the calibration core in `tests/test_person_calibration.py`); the render loops are
live only.
