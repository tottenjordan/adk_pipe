import { describe, it, expect } from "vitest";
import { buildConceptEdits } from "@/app/run/[sessionId]/run-helpers";

const originals = [
  {
    concept_name: "c0",
    image_generation_prompt: "p0",
    aspect_ratio: "9:16",
    visual_style: "photoreal",
  },
  {
    concept_name: "c1",
    image_generation_prompt: "p1",
    aspect_ratio: "1:1",
    visual_style: "cartoon",
  },
];

describe("buildConceptEdits", () => {
  it("returns empty array when nothing changed", () => {
    const drafts = originals.map((c) => ({
      image_generation_prompt: c.image_generation_prompt,
      aspect_ratio: c.aspect_ratio,
      visual_style: c.visual_style,
      revision_note: "",
    }));
    expect(buildConceptEdits(originals, drafts)).toEqual([]);
  });

  it("captures only the changed field with its index", () => {
    const drafts = [
      {
        image_generation_prompt: "p0",
        aspect_ratio: "9:16",
        visual_style: "photoreal",
        revision_note: "",
      },
      {
        image_generation_prompt: "NEW",
        aspect_ratio: "1:1",
        visual_style: "cartoon",
        revision_note: "",
      },
    ];
    expect(buildConceptEdits(originals, drafts)).toEqual([
      { index: 1, image_generation_prompt: "NEW" },
    ]);
  });

  it("captures a revision note trimmed, without unchanged fields", () => {
    const drafts = [
      {
        image_generation_prompt: "p0",
        aspect_ratio: "9:16",
        visual_style: "photoreal",
        revision_note: "  make it brighter  ",
      },
      {
        image_generation_prompt: "p1",
        aspect_ratio: "1:1",
        visual_style: "cartoon",
        revision_note: "",
      },
    ];
    expect(buildConceptEdits(originals, drafts)).toEqual([
      { index: 0, revision_note: "make it brighter" },
    ]);
  });

  it("combines multiple changed fields and a note on one concept", () => {
    const drafts = [
      {
        image_generation_prompt: "p0",
        aspect_ratio: "9:16",
        visual_style: "photoreal",
        revision_note: "",
      },
      {
        image_generation_prompt: "P1B",
        aspect_ratio: "16:9",
        visual_style: "cartoon",
        revision_note: "add neon",
      },
    ];
    expect(buildConceptEdits(originals, drafts)).toEqual([
      {
        index: 1,
        image_generation_prompt: "P1B",
        aspect_ratio: "16:9",
        revision_note: "add neon",
      },
    ]);
  });

  it("handles empty originals safely", () => {
    expect(buildConceptEdits([], [])).toEqual([]);
  });
});
