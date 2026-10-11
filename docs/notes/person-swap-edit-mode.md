# Person swaps: edit vs regenerate (spike, 2026-10-11)

PR 0 of [the faithful person swaps plan](../plans/2026-10-11-faithful-person-swaps.md).
Script: `experiments/person_reference/swap_compare.py` (tests: `tests/test_swap_compare.py`).

## Setup

- **Concepts:** three cast creatives from two finished `creative_agent` runs: "The Replay
  Booth Defense" (photoreal), "The Unnecessary Roughness Flag" (3D character render) and
  "Tailgate Sideline Pit Stop" (candid 35mm photo).
- **Photos:** two consented test photos. photo1 is 600 px and is the person the three
  originals were already cast with. photo2 is 240 px, a different person.
- **Modes:** N=2 per (concept, photo, mode), 24 renders on `gemini-nano-banana-2.1`, all
  with `ALLOW_ADULT`, no QA re-render, paced at 31 s:
  - **regenerate:** today's variant path (`variant_concept` → `render_concept`, the
    concept prompt plus the person photo);
  - **edit:** the prototype edit request below (the original render plus the person
    photo), with the aspect ratio read from the original's size.
- **Scoring:** one comparison vision call per output on the image-QA model
  (`gemini-3.8-flash`, temperature 0) given the original and the variant. It returns
  `same_layout`, `same_text`, `same_graphics`, `same_product`, `only_person_changed` and
  `differences`. Fidelity means all four `same_*` flags are true. Likeness is the image
  check's own `person_likeness` (`image_qa.inspect_image(..., person_image=)`).

### Prototype edit request (reuse in PR 1)

Contents, in this order:

1. Text: `EDIT_INSTRUCTION`, then (only under `unwanted_logo` strictness)
   `no_other_logos_line(brand)`, then `REFERENCE_IGNORE_TEXT_LINE`, joined by blank lines.
2. Text: `Image 1 (original ad):`
3. The original render (inline bytes).
4. Text: `Image 2 (person):`
5. The person photo (inline bytes).

`EDIT_INSTRUCTION`:

> Edit image 1. Replace only the main person with the person in image 2: keep their face
> shape, skin tone, hair, eye colour, apparent age and distinguishing features. Keep
> everything else in image 1 exactly as it is: the composition and framing, camera angle,
> lighting, colours, background, every prop and its count, all arrows and graphic elements
> and their direction, every word of text, logos and the product. Match image 1's art
> style. Do not add, remove or move anything else.

The request uses `ImageConfig(aspect_ratio=<nearest allowed to the original>,
image_size=config.image_size, person_generation="ALLOW_ADULT")` through
`image_tools._render_person_image`. No block happened in 12 edits.

## Results

| Mode | Renders | Errors | Fidelity | Only person changed | Likeness |
|---|---|---|---|---|---|
| regenerate | 12 | 0 | 0% | 0% | 100% |
| edit | 12 | 0 | 75% | 58% | 83% |

| Mode / photo | Renders | Fidelity | Only person changed | Likeness |
|---|---|---|---|---|
| regenerate / photo1 (600 px, same person as the original) | 6 | 0% | 0% | 100% |
| regenerate / photo2 (240 px, a real swap) | 6 | 0% | 0% | 100% |
| edit / photo1 | 6 | 67% | 33% | 100% |
| edit / photo2 | 6 | 83% | 83% | 67% |

On real swaps (photo2), edit was faithful on 5 of 6 renders: 2/2 on Replay Booth, 2/2 on
Tailgate and 1/2 on Roughness Flag. Both photo2 likeness misses were on the 3D-character
Roughness Flag concept, flagged "person likeness lost", and one of them also "person
distorted". Likeness held on both photographic concepts.

Each edit took about 23 s on average and each regenerate about 20 s, so the cost is about
the same.

**Reading photo1:** the originals already show photo1's person, so an edit with photo1 is
close to an identity edit. That is why only 33% count as "only the person changed": the
person didn't change at all. The fidelity misses on photo1 were both on Replay Booth, where
the small fine print on the serum label was redrawn differently. The arrows, the
"DEFENSIVE SHIELD" label, the headline and the brand line were all kept.

## Observed differences

- **Replay Booth, regenerate:** the scene is re-rolled in every render. The layout is
  mirrored (person on the right), or the camera moves to a straight-on mirror shot. The
  "DEFENSIVE SHIELD" arrow label is lost, the jawline arrows become a playbook diagram or a
  graph, and the brand line moves under the bottle. The edit kept the side profile, both
  jawline arrows, the labelled arrow pointing at the bottle, the headline and the
  "ESTĒE LAUDER / ADVANCED NIGHT REPAIR" footer, and swapped only the face and hair.
- **Roughness Flag, regenerate:** the two bottles swap sides, and the yellow penalty flags
  move to the left and change form (flags on sticks, tied bases, a flag inside the blue
  bottle). The blue bottle loses its neon glow. The edit kept the bottle positions, the
  glow and the two weighted flags on the right. One edit added a wall sconce and sparkles
  to the bottle glow.
- **Tailgate Sideline Pit Stop, regenerate:** adds an Instagram story header, drops or
  changes the "Send Message" bar, and changes the pose (looking at the camera) and the
  parked vehicles. All 4 edits were judged faithful.

## Resolution (240 vs 600 px)

This spike can't separate the effect of resolution from the effect of a real swap. photo1
(600 px) is the original person, so its edits barely change the face. The real swaps all
used the 240 px photo2. With photo2, regenerate was judged a likeness on 6/6 renders, but
edit only on 4/6. Both misses were in the stylised 3D concept, where the edit has to fit a
new face into an existing cartoon head and art style.

A plausible reading: a small reference gives the edit too little facial detail to override
the original's stylised face. In a fresh render, the model draws the face from the photo
alone. Either way, this supports PR 2's minimum resolution, and the likeness rule with its
bounded re-render should stay on for edits. Re-measure with a ≥ 1024 px second photo after
PR 2.

## Decision

Rule (plan Task 0.1): go when edit fidelity ≥ regenerate fidelity + 0.3 **and** edit
likeness ≥ 0.8.

- Fidelity: edit 75% vs regenerate 0% (+75 points).
- Likeness: edit 83% overall.

**Decision: go.**

**Caveat:** on real swaps alone, edit likeness is 67%, below 0.8, and both misses are on a
stylised (3D) concept with the 240 px photo. PR 1's QA keeps the likeness rule, and its
re-render repeats the edit, so a likeness miss gets one corrected attempt. A fresh
re-render would lose the composition, so it is not used.
