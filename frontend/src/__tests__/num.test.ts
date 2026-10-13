import { describe, expect, it } from "vitest";
import { finite, joinList, round4 } from "@/lib/num";

describe("num helpers", () => {
  it("finite accepts only finite numbers", () => {
    expect(finite(0)).toBe(true);
    expect(finite(-1.5)).toBe(true);
    for (const v of [NaN, Infinity, -Infinity, "1", null, undefined, {}]) expect(finite(v)).toBe(false);
  });

  it("round4 rounds to 4 decimals", () => {
    expect(round4(0.123456)).toBe(0.1235);
    expect(round4(1)).toBe(1);
    expect(round4(-0.00004)).toBe(-0);
  });

  it("joinList joins with commas and a final 'and'", () => {
    expect(joinList([])).toBe("");
    expect(joinList(["a"])).toBe("a");
    expect(joinList(["a", "b"])).toBe("a and b");
    expect(joinList(["a", "b", "c"])).toBe("a, b and c");
    expect(joinList([5, 20, 50].map(String))).toBe("5, 20 and 50");
  });
});
