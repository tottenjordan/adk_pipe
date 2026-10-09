# `person_reference/` — calibration spike

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

The grid is 3 photographic style families (`Candid 35mm film photo`,
`Photoreal / editorial`, `Cinematic film still`) × 2 framings (hero close-up, mid
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

Use a clear, front-facing photo (JPEG, PNG or WebP, ≤ 10 MB).

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
(sleep between renders, default 31 for the 2 images/min quota).

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
