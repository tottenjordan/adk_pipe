import { describe, it, expect } from "vitest";
import {
  MAX_REFERENCE_IMAGES,
  formatReferenceImages,
  invalidReferenceUris,
  isReferenceUri,
  referenceImagesFromForm,
  referenceRowsFromState,
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

describe("referenceRowsFromState", () => {
  it("row 1 is the legacy pair, the rest are extras", () => {
    expect(
      referenceRowsFromState({
        reference_image_uri: "gs://b/p.png",
        reference_image_role: "product",
        reference_images: [
          { uri: "gs://b/p.png", role: "product" },
          { uri: "gs://b/s.png", role: "style" },
        ],
      }),
    ).toEqual({
      first: { uri: "gs://b/p.png", role: "product" },
      extras: [{ uri: "gs://b/s.png", role: "style" }],
    });
  });

  it("a legacy-only run has no extras", () => {
    expect(referenceRowsFromState({ reference_image_uri: "gs://b/p.png" })).toEqual({
      first: { uri: "gs://b/p.png", role: "product" },
      extras: [],
    });
  });

  it("moves the first listed reference into row 1 when the legacy pair is empty", () => {
    expect(
      referenceRowsFromState({
        reference_images: [
          { uri: "gs://b/1.png", role: "product" },
          { uri: "gs://b/2.png", role: "style" },
          { uri: "gs://b/3.png", role: "logo" },
        ],
      }),
    ).toEqual({
      first: { uri: "gs://b/1.png", role: "product" },
      extras: [
        { uri: "gs://b/2.png", role: "style" },
        { uri: "gs://b/3.png", role: "logo" },
      ],
    });
  });

  it("has no row 1 without references", () => {
    expect(referenceRowsFromState({})).toEqual({ first: null, extras: [] });
  });
});

describe("isReferenceUri", () => {
  it.each(["gs://bucket/obj.png", "gs://b/dir/x.jpg", "https://x.com/a.png", "http://x/a", " gs://b/o "])(
    "accepts %s",
    (uri) => {
      expect(isReferenceUri(uri)).toBe(true);
    },
  );

  it.each(["", "gs://bucket", "gs://bucket/", "gs:///obj", "ftp://x/a", "x.com/a.png", "https://", "gs://b/a b.png", "file:///etc/passwd"])(
    "rejects %s",
    (uri) => {
      expect(isReferenceUri(uri)).toBe(false);
    },
  );
});

describe("invalidReferenceUris", () => {
  it("lists the non-blank rows that are not gs:// or http(s) URIs", () => {
    expect(
      invalidReferenceUris({
        referenceImageUri: "bucket/p.png",
        extraReferenceImages: [
          { uri: "", role: "" },
          { uri: "https://x/s.jpg", role: "style" },
          { uri: "nope", role: "logo" },
        ],
      }),
    ).toEqual(["bucket/p.png", "nope"]);
  });

  it("is empty when every row is valid or blank", () => {
    expect(invalidReferenceUris({ referenceImageUri: "  ", extraReferenceImages: [] })).toEqual([]);
  });
});

describe("reference roles (only the legacy pair defaults to product)", () => {
  it("skips listed entries without a valid role", () => {
    expect(
      resolveReferenceImages({
        reference_images: [
          { uri: "gs://b/a.png" },
          { uri: "gs://b/b.png", role: "" },
          { uri: "gs://b/c.png", role: "logo" },
        ],
      }),
    ).toEqual([{ uri: "gs://b/c.png", role: "logo" }]);
  });

  it("the form's empty role still means product (rows are emitted with explicit roles)", () => {
    expect(
      referenceImagesFromForm({
        referenceImageUri: "gs://b/p.png",
        referenceImageRole: "",
        extraReferenceImages: [{ uri: "gs://b/x.png", role: "" }],
      }),
    ).toEqual([
      { uri: "gs://b/p.png", role: "product" },
      { uri: "gs://b/x.png", role: "product" },
    ]);
  });

  it("never emits invalid URIs from the form", () => {
    expect(
      referenceImagesFromForm({
        referenceImageUri: "not a uri",
        extraReferenceImages: [
          { uri: "bucket/x.png", role: "style" },
          { uri: "https://x/s.jpg", role: "style" },
        ],
      }),
    ).toEqual([{ uri: "https://x/s.jpg", role: "style" }]);
  });
});
