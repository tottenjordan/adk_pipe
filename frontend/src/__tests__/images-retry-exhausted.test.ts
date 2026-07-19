import { describe, it, expect } from "vitest";
import { imagesRetryExhausted } from "@/lib/utils";

describe("imagesRetryExhausted", () => {
  it("is true when the retry-exhaustion marker is set", () => {
    expect(imagesRetryExhausted({ _images_generated__retry_exhausted: true })).toBe(
      true,
    );
  });

  it("is false when the marker is absent", () => {
    expect(imagesRetryExhausted({})).toBe(false);
    expect(imagesRetryExhausted({ _images_generated: true })).toBe(false);
  });

  it("is false when the marker is present but falsy", () => {
    expect(
      imagesRetryExhausted({ _images_generated__retry_exhausted: false }),
    ).toBe(false);
  });

  it("returns a boolean (never a raw truthy value)", () => {
    // ADK could serialize the marker as a truthy non-bool; the helper must coerce.
    expect(
      imagesRetryExhausted({ _images_generated__retry_exhausted: "true" }),
    ).toBe(true);
  });
});
