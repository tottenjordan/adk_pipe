// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TrendSituationLine } from "@/components/trend-situation-line";
import {
  daysFromAsOf,
  describeTrendSituation,
  isSensitiveSituation,
  parseTrendSituation,
  trendSituationText,
} from "@/lib/trend-situation";

const AS_OF = "2026-10-11T10:00:00-04:00";

const state = (over: Record<string, unknown> = {}) => ({
  trend_situation: {
    phase: "upcoming",
    event_name: "Texas vs Oklahoma",
    event_start: "2026-10-12",
    outcome: "",
    source_ids: ["src-1", "src-2"],
    as_of: AS_OF,
    timely_ok: true,
    valid_until: "2026-10-12T23:59:59-04:00",
    abstain_reason: "",
    ...over,
  },
});

describe("parseTrendSituation", () => {
  it("is null when absent or malformed", () => {
    expect(parseTrendSituation({})).toBeNull();
    expect(parseTrendSituation(null)).toBeNull();
    expect(parseTrendSituation({ trend_situation: "x" })).toBeNull();
    expect(parseTrendSituation({ trend_situation: { phase: "soon" } })).toBeNull();
  });

  it("keeps only string source ids and exactly-true timely_ok", () => {
    const s = parseTrendSituation(state({ source_ids: ["src-1", 3], timely_ok: "true" }));
    expect(s?.sourceIds).toEqual(["src-1"]);
    expect(s?.timelyOk).toBe(false);
  });
});

describe("daysFromAsOf", () => {
  it("counts days in the run's timezone", () => {
    expect(daysFromAsOf("2026-10-12", AS_OF)).toBe(1);
    expect(daysFromAsOf("2026-10-10", AS_OF)).toBe(-1);
    expect(daysFromAsOf("2026-10-11T20:00:00-04:00", AS_OF)).toBe(0);
    // 03:00 UTC on the 12th is still the 11th in New York.
    expect(daysFromAsOf("2026-10-12T03:00:00Z", AS_OF)).toBe(0);
    expect(daysFromAsOf("", AS_OF)).toBeNull();
    expect(daysFromAsOf("next week", AS_OF)).toBeNull();
  });
});

describe("describeTrendSituation", () => {
  it("labels a game tomorrow", () => {
    expect(describeTrendSituation(state())).toEqual({
      label: "Texas vs Oklahoma tomorrow",
      timely: true,
      sources: 2,
      validUntil: "2026-10-12 23:59",
    });
  });

  it("labels a loss yesterday", () => {
    const view = describeTrendSituation(
      state({
        phase: "just_happened",
        event_start: "2026-10-10",
        outcome: "loss",
        valid_until: "2026-10-12T23:59:59-04:00",
      }),
    );
    expect(view?.label).toBe("Lost yesterday — Texas vs Oklahoma");
  });

  it("explains an abstention", () => {
    expect(
      describeTrendSituation(
        state({ phase: "unknown", timely_ok: false, abstain_reason: "no source dates the event" }),
      ),
    ).toEqual({ label: "Not timely: no source dates the event", timely: false, sources: 2, validUntil: null });
  });

  it("labels a non-timely known phase with its reason", () => {
    expect(
      describeTrendSituation(state({ phase: "ongoing", timely_ok: false, abstain_reason: "no single dated event" }))
        ?.label,
    ).toBe("Ongoing · not timely: no single dated event");
  });

  it("shows nothing when the feature is off or absent", () => {
    expect(describeTrendSituation(state({ phase: "unknown", timely_ok: false, abstain_reason: "disabled" }))).toBeNull();
    expect(describeTrendSituation({})).toBeNull();
  });
});

describe("isSensitiveSituation", () => {
  it("is true only for the sensitive phase", () => {
    expect(isSensitiveSituation(state({ phase: "sensitive", timely_ok: false }))).toBe(true);
    expect(isSensitiveSituation(state())).toBe(false);
    expect(isSensitiveSituation({})).toBe(false);
  });
});

describe("trendSituationText", () => {
  it("joins label, sources count and validity", () => {
    expect(trendSituationText(state())).toBe("Texas vs Oklahoma tomorrow · 2 sources · valid until 2026-10-12 23:59");
    expect(trendSituationText(state({ source_ids: ["src-1"] }))).toContain("· 1 source ·");
    expect(trendSituationText(state({ source_ids: [], timely_ok: false, phase: "unknown", abstain_reason: "x" }))).toBe(
      "Not timely: x",
    );
  });
});

describe("TrendSituationLine", () => {
  it("renders a muted Current moment line", () => {
    render(<TrendSituationLine state={state()} />);
    expect(screen.getByText("Current moment:")).toBeTruthy();
    expect(screen.getByText(/Texas vs Oklahoma tomorrow · 2 sources/)).toBeTruthy();
  });

  it("prefixes a sensitive moment with the Sensitive chip", () => {
    const sensitive = state({ phase: "sensitive", timely_ok: false, abstain_reason: "a sensitive moment" });
    render(<TrendSituationLine state={sensitive} />);
    const line = screen.getByText("Current moment:").closest("p");
    const chip = screen.getByText("Sensitive");
    expect(line).toContainElement(chip);
    expect(chip.className).toContain("text-mark-pending");
  });

  it("shows no chip for other phases", () => {
    render(<TrendSituationLine state={state()} />);
    expect(screen.queryByText("Sensitive")).toBeNull();
  });

  it("renders nothing without a situation", () => {
    const { container } = render(<TrendSituationLine state={{}} />);
    expect(container.innerHTML).toBe("");
  });
});
