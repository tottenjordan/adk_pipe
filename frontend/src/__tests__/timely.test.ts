import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  formatValidUntil,
  isExpiredAt,
  isTimely,
  pairedCopy,
  parseValidUntil,
  timelyStatus,
} from "@/lib/timely";

// The shared golden table (also read by tests/test_timely_expiry.py): the TS and
// Python expiry rules must agree case for case.
interface Golden {
  case: string;
  state: Record<string, unknown>;
  concept_name: string;
  now: string;
  timely: boolean;
  expired: boolean;
}
const GOLDEN: Golden[] = JSON.parse(
  readFileSync(resolve(__dirname, "../../../tests/fixtures/timely_expiry.json"), "utf8"),
);

describe("timely expiry golden table", () => {
  it("has cases", () => {
    expect(GOLDEN.length).toBeGreaterThanOrEqual(10);
  });

  it.each(GOLDEN.map((g) => [g.case, g] as const))("%s", (_name, g) => {
    const now = Date.parse(g.now);
    const status = timelyStatus(g.state, g.concept_name, now);
    expect({ timely: status.timely, expired: status.expired }).toEqual({
      timely: g.timely,
      expired: g.expired,
    });
    expect(isTimely(g.state, g.concept_name)).toBe(g.timely);
    expect(status.label).toBe(g.timely ? (g.expired ? "Expired" : "Timely") : "");
  });

  it("reads state values kept as JSON strings", () => {
    for (const g of GOLDEN) {
      const state = {
        ...g.state,
        final_visual_concepts: JSON.stringify(g.state.final_visual_concepts),
        ad_copy_critique: JSON.stringify(g.state.ad_copy_critique),
      };
      const status = timelyStatus(state, g.concept_name, Date.parse(g.now));
      expect([g.case, status.timely, status.expired]).toEqual([g.case, g.timely, g.expired]);
    }
  });
});

describe("timely helpers", () => {
  it("pairs a copy by id only, comparing as strings", () => {
    const copies = [{ original_id: 2 }, { original_id: "3" }];
    expect(pairedCopy(copies, { ad_copy_id: "2" })).toBe(copies[0]);
    expect(pairedCopy(copies, { ad_copy_id: 3 })).toBe(copies[1]);
    expect(pairedCopy(copies, { ad_copy_id: 9 })).toBeUndefined();
    expect(pairedCopy(copies, {})).toBeUndefined();
  });

  it("parses only ISO datetimes with an offset", () => {
    expect(parseValidUntil("2026-10-14T23:59:59-04:00")).toBe(Date.parse("2026-10-15T03:59:59Z"));
    expect(parseValidUntil("2026-10-14")).toBeNull();
    expect(parseValidUntil("2026-10-14T23:59:59")).toBeNull();
    expect(parseValidUntil("next tuesday")).toBeNull();
    expect(parseValidUntil("")).toBeNull();
  });

  it("expires at the boundary, never when unparseable", () => {
    const at = Date.parse("2026-10-14T23:59:59-04:00");
    expect(isExpiredAt("2026-10-14T23:59:59-04:00", at - 1)).toBe(false);
    expect(isExpiredAt("2026-10-14T23:59:59-04:00", at)).toBe(true);
    expect(isExpiredAt("garbage", Number.MAX_SAFE_INTEGER)).toBe(false);
  });

  it("formats valid_until in its own offset", () => {
    expect(formatValidUntil("2026-10-14T23:59:59-04:00")).toBe("Oct 14, 23:59");
    expect(formatValidUntil("2026-01-02T08:05:00Z")).toBe("Jan 2, 08:05");
    expect(formatValidUntil("next tuesday")).toBeNull();
    expect(formatValidUntil("2026-13-02T08:05:00Z")).toBeNull();
  });
});
