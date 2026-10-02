import { describe, expect, it } from "vitest";
import { isNavItemCurrent } from "@/components/main-nav";

describe("isNavItemCurrent", () => {
  it("matches the home route exactly", () => {
    expect(isNavItemCurrent("/", "/")).toBe(true);
    expect(isNavItemCurrent("/runs", "/")).toBe(false);
  });
  it("matches nested routes by prefix on whole segments", () => {
    expect(isNavItemCurrent("/experiments", "/experiments")).toBe(true);
    expect(isNavItemCurrent("/experiments/exp-123", "/experiments")).toBe(true);
    expect(isNavItemCurrent("/runs", "/runs")).toBe(true);
    expect(isNavItemCurrent("/run/abc", "/runs")).toBe(false);
    expect(isNavItemCurrent("/runsx", "/runs")).toBe(false);
    expect(isNavItemCurrent(null, "/runs")).toBe(false);
  });
});
