import { describe, it, expect } from "vitest";
import {
  buildDisplayFields,
  VISUAL_DIRECTION_FIELDS,
  type DisplayFieldDef,
} from "@/lib/utils";

describe("buildDisplayFields", () => {
  it("drops entries whose resolved value is empty/unset", () => {
    const defs: DisplayFieldDef[] = [
      { label: "Brand", key: "brand" },
      { label: "Product", key: "target_product" },
      { label: "Audience", key: "target_audience" },
    ];
    const state = { brand: "PRS", target_product: "", target_audience: null };
    const fields = buildDisplayFields(state, defs);
    expect(fields).toEqual([{ label: "Brand", key: "brand", value: "PRS" }]);
  });

  it("falls back to altKey when the primary key is absent", () => {
    const defs: DisplayFieldDef[] = [
      { label: "Trend", key: "target_search_trends", altKey: "target_search_trend" },
    ];
    const state = { target_search_trend: "Powerball" };
    const fields = buildDisplayFields(state, defs);
    expect(fields).toEqual([
      { label: "Trend", key: "target_search_trends", value: "Powerball" },
    ]);
  });

  it("flattens array/object values via formatStateValue", () => {
    const defs: DisplayFieldDef[] = [{ label: "Trend", key: "target_search_trends" }];
    const state = { target_search_trends: ["a", "b"] };
    const fields = buildDisplayFields(state, defs);
    expect(fields).toEqual([
      { label: "Trend", key: "target_search_trends", value: "a, b" },
    ]);
  });

  it("returns [] when no field has a value", () => {
    const defs: DisplayFieldDef[] = [{ label: "Brand", key: "brand" }];
    expect(buildDisplayFields({}, defs)).toEqual([]);
  });
});

describe("VISUAL_DIRECTION_FIELDS", () => {
  it("maps the seven visual-intent keys", () => {
    expect(VISUAL_DIRECTION_FIELDS.map((f) => f.key)).toEqual([
      "visual_intent",
      "brand_colors",
      "visual_style_preference",
      "visual_avoid",
      "visual_aspect_ratio",
      "reference_image_uri",
      "reference_image_role",
    ]);
  });

  it("returns only the set visual-direction fields, labelled", () => {
    const state = {
      visual_intent: "moody film noir",
      brand_colors: "",
      visual_style_preference: "",
      visual_avoid: "",
      visual_aspect_ratio: "1:1",
      reference_image_uri: "",
      reference_image_role: "",
    };
    const fields = buildDisplayFields(state, VISUAL_DIRECTION_FIELDS);
    expect(fields).toEqual([
      { label: "Art Direction", key: "visual_intent", value: "moody film noir" },
      { label: "Aspect Ratio", key: "visual_aspect_ratio", value: "1:1" },
    ]);
  });
});
