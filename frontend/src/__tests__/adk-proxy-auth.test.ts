import { describe, it, expect } from "vitest";
import { backendNeedsAuth } from "@/app/api/adk/[...path]/route";

describe("backendNeedsAuth", () => {
  it("is true for a remote https backend (private Cloud Run)", () => {
    expect(backendNeedsAuth("https://trend-trawler-api-abc.run.app")).toBe(true);
  });
  it("is false for localhost dev backend", () => {
    expect(backendNeedsAuth("http://localhost:8000")).toBe(false);
  });
});
