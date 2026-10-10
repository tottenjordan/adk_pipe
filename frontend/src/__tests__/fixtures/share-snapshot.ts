import type { ShareSnapshotV1 } from "@/lib/share-snapshot";

export const SHARE_TOKEN = "Zq3_kL9-pX2mN7vB4cR8tA";

const creative = (i: number, ext: "jpg" | "png" = "jpg") => ({
  index: i,
  image: `${i}.${ext}`,
  aspect_ratio: i === 3 ? "9:16" : "4:5",
  alt: `A red electric guitar on a stage, concept ${i}`,
  visual_style: "risograph print",
  headline: `Headline ${i}: play it loud`,
  body: `Body copy ${i}. The SE CE24 has the tone you need for every stage.`,
  caption: `Caption ${i} — tonight's the night the whole crowd sings along with you, and the SE CE24 is ready for it. #livemusic`,
  cta: `Find your tone ${i}`,
  tone: "Playful",
});

/** A 4-creative slate snapshot (v1 contract), eval off. */
export function slateSnapshot(overrides: Partial<ShareSnapshotV1> = {}): ShareSnapshotV1 {
  return {
    version: 1,
    token: SHARE_TOKEN,
    created_at: "2026-10-09T00:00:00Z",
    scope: "slate",
    brand: "PRS Guitars",
    product: "SE CE24 Electric Guitar",
    trend: "Powerball jackpot",
    include_eval: false,
    creatives: [0, 1, 2, 3].map((i) => creative(i)),
    ...overrides,
  };
}

/** A slate made before share images were re-encoded: `<i>.png` objects. */
export function legacyPngSnapshot(): ShareSnapshotV1 {
  return slateSnapshot({ creatives: [0, 1, 2, 3].map((i) => creative(i, "png")) });
}

/** A single-creative snapshot with eval on. */
export function singleEvalSnapshot(): ShareSnapshotV1 {
  return slateSnapshot({
    scope: "creative",
    include_eval: true,
    creatives: [
      {
        ...creative(0),
        eval: {
          passed: false,
          score: 0.83,
          checks: [
            { gate: "product_visible", label: "Product visible", passed: true, advisory: false },
            { gate: "text_correct", label: "In-image text correct", passed: false, advisory: false },
            { gate: "brand_cue_present", label: "Brand cue present", passed: false, advisory: true },
          ],
        },
      },
    ],
  });
}
