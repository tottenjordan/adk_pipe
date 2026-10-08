import { describe, it, expect } from "vitest";
import {
  buildDisplayFields,
  formatRatingLearning,
  RATING_LEARNING_FIELD,
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

  it("uses a def's value() resolver over the key lookup", () => {
    const defs: DisplayFieldDef[] = [
      { label: "Both", key: "both", value: (st) => `${st.a}+${st.b}` },
    ];
    expect(buildDisplayFields({ a: "x", b: "y" }, defs)).toEqual([
      { label: "Both", key: "both", value: "x+y" },
    ]);
  });

  it("returns [] when no field has a value", () => {
    const defs: DisplayFieldDef[] = [{ label: "Brand", key: "brand" }];
    expect(buildDisplayFields({}, defs)).toEqual([]);
  });
});

describe("VISUAL_DIRECTION_FIELDS", () => {
  it("maps the visual-intent keys (references merged into one row)", () => {
    expect(VISUAL_DIRECTION_FIELDS.map((f) => f.key)).toEqual([
      "visual_intent",
      "brand_colors",
      "visual_style_preference",
      "visual_avoid",
      "visual_aspect_ratio",
      "reference_images",
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
      { label: "Art direction", key: "visual_intent", value: "moody film noir" },
      { label: "Aspect ratio", key: "visual_aspect_ratio", value: "1:1" },
    ]);
  });

  it("shows every reference image (legacy + list) as one row", () => {
    const fields = buildDisplayFields(
      {
        reference_image_uri: "gs://b/p.png",
        reference_image_role: "product",
        reference_images: [
          { uri: "gs://b/p.png", role: "product" },
          { uri: "gs://b/s.png", role: "style" },
        ],
      },
      VISUAL_DIRECTION_FIELDS,
    );
    expect(fields).toEqual([
      {
        label: "Reference images",
        key: "reference_images",
        value: "product: gs://b/p.png; style: gs://b/s.png",
      },
    ]);
  });
});

describe("rating learning field", () => {
  it("is hidden unless the run opted in", () => {
    expect(buildDisplayFields({}, [RATING_LEARNING_FIELD])).toEqual([]);
    expect(buildDisplayFields({ learn_from_ratings: false }, [RATING_LEARNING_FIELD])).toEqual([]);
  });

  it("summarises what was learned", () => {
    expect(formatRatingLearning({ learn_from_ratings: true })).toBe("On");
    expect(
      formatRatingLearning({
        learn_from_ratings: true,
        rating_signals_applied: { ratings: 23, applied: true },
      }),
    ).toBe("On: learned from 23 ratings");
    expect(
      formatRatingLearning({
        learn_from_ratings: true,
        rating_signals_applied: { ratings: 3, applied: false, reason: "not_enough_ratings" },
      }),
    ).toBe("On: not enough ratings yet (3)");
    expect(
      formatRatingLearning({
        learn_from_ratings: true,
        rating_signals_applied: { applied: false, reason: "unavailable" },
      }),
    ).toBe("On: ratings unavailable");
    expect(
      formatRatingLearning({
        learn_from_ratings: true,
        rating_signals_applied: { ratings: 3, applied: false, reason: "not_enough_ratings", min: 8 },
      }),
    ).toBe("On: not enough ratings yet (3 of 8)");
    expect(
      buildDisplayFields({ learn_from_ratings: true }, [RATING_LEARNING_FIELD]),
    ).toEqual([{ label: "Learn from ratings", key: "learn_from_ratings", value: "On" }]);
  });
});
