import { describe, it, expect } from "vitest";
import { extractItems } from "@/app/run/[sessionId]/run-helpers";

describe("extractItems", () => {
  it("extracts array from object with one key", () => {
    const data = { ad_copies: [{ id: 1 }, { id: 2 }] };
    const result = extractItems(data);
    expect(result).toEqual([{ id: 1 }, { id: 2 }]);
  });

  it("extracts first array found when multiple keys", () => {
    const data = { name: "test", items: [{ x: 1 }], other: "val" };
    const result = extractItems(data);
    expect(result).toEqual([{ x: 1 }]);
  });

  it("returns null for null input", () => {
    expect(extractItems(null)).toBeNull();
  });

  it("returns null for undefined input", () => {
    expect(extractItems(undefined)).toBeNull();
  });

  it("returns null for primitive input", () => {
    expect(extractItems("string")).toBeNull();
    expect(extractItems(42)).toBeNull();
  });

  it("returns null for object with no array values", () => {
    expect(extractItems({ a: "hello", b: 123 })).toBeNull();
  });

  it("returns empty array when the array value is empty", () => {
    expect(extractItems({ items: [] })).toEqual([]);
  });
});
