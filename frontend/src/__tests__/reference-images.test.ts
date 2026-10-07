import { describe, it, expect } from "vitest";
import {
  MAX_REFERENCE_IMAGES,
  extraReferencesFromState,
  formatReferenceImages,
  referenceImagesFromForm,
  resolveReferenceImages,
} from "@/lib/reference-images";

describe("referenceImagesFromForm", () => {
  it("returns [] with no references", () => {
    expect(referenceImagesFromForm({})).toEqual([]);
  });

  it("row 1 is the legacy field pair, then the extra rows, trimmed", () => {
    expect(
      referenceImagesFromForm({
        referenceImageUri: " gs://b/p.png ",
        referenceImageRole: "product",
        extraReferenceImages: [
          { uri: "https://x/s.jpg", role: "style" },
          { uri: "gs://b/l.png", role: "logo" },
        ],
      }),
    ).toEqual([
      { uri: "gs://b/p.png", role: "product" },
      { uri: "https://x/s.jpg", role: "style" },
      { uri: "gs://b/l.png", role: "logo" },
    ]);
  });

  it("defaults a missing role to product and drops blank rows", () => {
    expect(
      referenceImagesFromForm({
        referenceImageUri: "",
        extraReferenceImages: [
          { uri: "   ", role: "style" },
          { uri: "gs://b/a.png", role: "" },
        ],
      }),
    ).toEqual([{ uri: "gs://b/a.png", role: "product" }]);
  });

  it("dedupes by uri and caps at MAX_REFERENCE_IMAGES", () => {
    expect(MAX_REFERENCE_IMAGES).toBe(3);
    const refs = referenceImagesFromForm({
      referenceImageUri: "gs://b/1.png",
      referenceImageRole: "product",
      extraReferenceImages: [
        { uri: "gs://b/1.png", role: "style" },
        { uri: "gs://b/2.png", role: "style" },
        { uri: "gs://b/3.png", role: "logo" },
        { uri: "gs://b/4.png", role: "logo" },
      ],
    });
    expect(refs.map((r) => r.uri)).toEqual(["gs://b/1.png", "gs://b/2.png", "gs://b/3.png"]);
  });
});

describe("resolveReferenceImages", () => {
  it("folds the legacy keys in first and dedupes against the list", () => {
    expect(
      resolveReferenceImages({
        reference_image_uri: "gs://b/p.png",
        reference_image_role: "product",
        reference_images: [
          { uri: "gs://b/p.png", role: "product" },
          { uri: "gs://b/s.png", role: "style" },
        ],
      }),
    ).toEqual([
      { uri: "gs://b/p.png", role: "product" },
      { uri: "gs://b/s.png", role: "style" },
    ]);
  });

  it("treats a legacy-only run's empty role as product", () => {
    expect(resolveReferenceImages({ reference_image_uri: "gs://b/x.png", reference_image_role: "" })).toEqual([
      { uri: "gs://b/x.png", role: "product" },
    ]);
  });

  it("parses a JSON string and skips invalid entries and roles", () => {
    expect(
      resolveReferenceImages({
        reference_images: JSON.stringify([
          { uri: "gs://b/a.png", role: "banana" },
          { role: "logo" },
          "gs://b/bare.png",
          { uri: "gs://b/ok.png", role: "logo" },
        ]),
      }),
    ).toEqual([{ uri: "gs://b/ok.png", role: "logo" }]);
    expect(resolveReferenceImages({ reference_images: "[oops" })).toEqual([]);
  });
});

describe("formatReferenceImages", () => {
  it("lists role: uri pairs in order", () => {
    expect(
      formatReferenceImages({
        reference_image_uri: "gs://b/p.png",
        reference_image_role: "product",
        reference_images: [{ uri: "gs://b/s.png", role: "style" }],
      }),
    ).toBe("product: gs://b/p.png; style: gs://b/s.png");
  });

  it("is empty with no references", () => {
    expect(formatReferenceImages({ reference_image_uri: "", reference_images: [] })).toBe("");
  });
});

describe("extraReferencesFromState", () => {
  it("returns the references after row 1 (the legacy pair)", () => {
    expect(
      extraReferencesFromState({
        reference_image_uri: "gs://b/p.png",
        reference_image_role: "product",
        reference_images: [
          { uri: "gs://b/p.png", role: "product" },
          { uri: "gs://b/s.png", role: "style" },
        ],
      }),
    ).toEqual([{ uri: "gs://b/s.png", role: "style" }]);
  });

  it("returns [] for a legacy-only run", () => {
    expect(extraReferencesFromState({ reference_image_uri: "gs://b/p.png" })).toEqual([]);
  });
});
