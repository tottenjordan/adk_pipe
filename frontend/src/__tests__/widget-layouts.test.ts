import { describe, it, expect } from "vitest";

// Replicate WIDGET_LAYOUTS and related constants from run page.
const WIDGET_LAYOUTS: Record<string, { pairs: [string, string][]; fullWidth: string }> = {
  final_visual_concepts: {
    pairs: [
      ["trend", "trend_reference"],
      ["markets_product", "audience_appeal"],
      ["selection_rationale", "social_caption"],
      ["call_to_action", "concept_summary"],
    ],
    fullWidth: "image_generation_prompt",
  },
  ad_copy_critique: {
    pairs: [
      ["tone_style", "call_to_action"],
      ["trend_connection", "body_text"],
      ["audience_appeal_rationale", "social_caption"],
    ],
    fullWidth: "detailed_performance_rationale",
  },
};

const DEFAULT_LAYOUT = { pairs: [] as [string, string][], fullWidth: "" };

const HIDDEN_FIELDS = new Set(["id", "original_id", "ad_copy_id"]);

const FIELD_LABELS: Record<string, string> = {
  id: "ID",
  original_id: "ID",
  ad_copy_id: "Ad Copy ID",
  tone_style: "Tone / Style",
  headline: "Headline",
  body_text: "Body Text",
  trend_connection: "Trend Connection",
  audience_appeal_rationale: "Audience Appeal",
  audience_appeal: "Audience Appeal",
  social_caption: "Social Caption",
  call_to_action: "Call to Action",
  detailed_performance_rationale: "Performance Rationale",
  selection_rationale: "Selection Rationale",
  concept_name: "Concept Name",
  trend: "Trend",
  trend_reference: "Trend Reference",
  markets_product: "Markets Product",
  concept_summary: "Concept Summary",
  image_generation_prompt: "Image Prompt",
};

describe("WIDGET_LAYOUTS", () => {
  it("has layout config for final_visual_concepts", () => {
    const layout = WIDGET_LAYOUTS["final_visual_concepts"];
    expect(layout).toBeDefined();
    expect(layout.pairs).toHaveLength(4);
    expect(layout.fullWidth).toBe("image_generation_prompt");
  });

  it("has layout config for ad_copy_critique", () => {
    const layout = WIDGET_LAYOUTS["ad_copy_critique"];
    expect(layout).toBeDefined();
    expect(layout.pairs).toHaveLength(3);
    expect(layout.fullWidth).toBe("detailed_performance_rationale");
  });

  it("falls back to default layout for unknown keys", () => {
    const layout = WIDGET_LAYOUTS["unknown_widget"] || DEFAULT_LAYOUT;
    expect(layout.pairs).toEqual([]);
    expect(layout.fullWidth).toBe("");
  });
});

describe("HIDDEN_FIELDS", () => {
  it("hides id, original_id, and ad_copy_id", () => {
    expect(HIDDEN_FIELDS.has("id")).toBe(true);
    expect(HIDDEN_FIELDS.has("original_id")).toBe(true);
    expect(HIDDEN_FIELDS.has("ad_copy_id")).toBe(true);
  });

  it("does not hide visible fields", () => {
    expect(HIDDEN_FIELDS.has("headline")).toBe(false);
    expect(HIDDEN_FIELDS.has("tone_style")).toBe(false);
  });
});

describe("FIELD_LABELS", () => {
  it("maps field keys to human-readable labels", () => {
    expect(FIELD_LABELS["tone_style"]).toBe("Tone / Style");
    expect(FIELD_LABELS["call_to_action"]).toBe("Call to Action");
    expect(FIELD_LABELS["image_generation_prompt"]).toBe("Image Prompt");
  });

  it("all layout pair fields have labels", () => {
    for (const [, layout] of Object.entries(WIDGET_LAYOUTS)) {
      for (const [left, right] of layout.pairs) {
        expect(FIELD_LABELS[left]).toBeDefined();
        expect(FIELD_LABELS[right]).toBeDefined();
      }
      if (layout.fullWidth) {
        expect(FIELD_LABELS[layout.fullWidth]).toBeDefined();
      }
    }
  });
});
