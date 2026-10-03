import { describe, expect, it } from "vitest";
import { EXPLAIN_KEY, parseView, readExplainPref, urlForView, writeExplainPref } from "@/lib/experiment-view";

function memoryStorage() {
  const m = new Map<string, string>();
  return {
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k),
    map: m,
  };
}

describe("view switch", () => {
  it("defaults to the overview", () => {
    expect(parseView(null)).toBe("overview");
    expect(parseView("nonsense")).toBe("overview");
    expect(parseView("analysis")).toBe("analysis");
  });
  it("persists the analysis view in the URL and drops it for the default", () => {
    expect(urlForView("http://x/experiments/abc?foo=1", "analysis")).toBe("/experiments/abc?foo=1&view=analysis");
    expect(urlForView("http://x/experiments/abc?view=analysis&foo=1", "overview")).toBe("/experiments/abc?foo=1");
  });
});

describe("explain preference", () => {
  it("round-trips through storage under tt:explain", () => {
    const s = memoryStorage();
    expect(readExplainPref(s)).toBe(false);
    expect(writeExplainPref(s, true)).toBe(true);
    expect(s.map.get(EXPLAIN_KEY)).toBe("1");
    expect(EXPLAIN_KEY).toBe("tt:explain");
    expect(readExplainPref(s)).toBe(true);
    writeExplainPref(s, false);
    expect(s.map.has(EXPLAIN_KEY)).toBe(false);
    expect(readExplainPref(s)).toBe(false);
  });
  it("reads as off when storage is missing or throws", () => {
    expect(readExplainPref(null)).toBe(false);
    expect(writeExplainPref(undefined, true)).toBe(false);
    const broken = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => {
        throw new Error("denied");
      },
    };
    expect(readExplainPref(broken)).toBe(false);
    expect(writeExplainPref(broken, true)).toBe(false);
  });
});
