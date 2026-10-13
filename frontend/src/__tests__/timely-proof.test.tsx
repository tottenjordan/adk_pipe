// @vitest-environment jsdom
import { act, render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { buildProofs, timelyFor, type AdCopy, type VisualConcept } from "@/lib/eval-matching";
import { useWallClock } from "@/lib/use-wall-clock";
import { ProofDetail } from "@/app/results/[sessionId]/proof-detail";
import { ProofGrid, timelyAria } from "@/app/results/[sessionId]/proof-grid";

const concept = {
  concept_name: "Countdown",
  headline: "Game-day ready",
  ad_copy_id: 1,
  uses_trend_state: true,
  timely_reason: "Builds anticipation for tomorrow's game.",
} as VisualConcept;

const situation = {
  phase: "upcoming",
  timely_ok: true,
  valid_until: "2026-10-12T23:59:59-04:00",
};

const VALID_UNTIL_MS = Date.parse("2026-10-12T23:59:59-04:00");
const BEFORE = VALID_UNTIL_MS - 60_000;

const copy = (over: Partial<AdCopy> = {}) => ({ original_id: 1, headline: "Copy", ...over }) as AdCopy;

afterEach(() => {
  vi.useRealTimers();
});

describe("timelyFor", () => {
  it("is the reason and validity for a timely concept", () => {
    expect(timelyFor(concept, situation, [], BEFORE)).toEqual({
      reason: "Builds anticipation for tomorrow's game.",
      validUntil: "Oct 12, 23:59",
      validUntilIso: "2026-10-12T23:59:59-04:00",
      expired: false,
    });
  });

  it("is expired at and after valid_until", () => {
    expect(timelyFor(concept, situation, [], VALID_UNTIL_MS)?.expired).toBe(true);
    expect(timelyFor(concept, situation, [], VALID_UNTIL_MS + 86_400_000)?.expired).toBe(true);
  });

  it("is never expired before the clock is known (now = 0)", () => {
    expect(timelyFor(concept, situation)?.expired).toBe(false);
  });

  it("is absent when the run has no timely moment", () => {
    expect(timelyFor(concept, undefined)).toBeUndefined();
    expect(timelyFor(concept, { ...situation, timely_ok: false })).toBeUndefined();
    expect(timelyFor(concept, { ...situation, valid_until: "" })).toBeUndefined();
  });

  it("is absent for evergreen concepts with an evergreen copy", () => {
    expect(timelyFor({ ...concept, uses_trend_state: false }, situation)).toBeUndefined();
    expect(timelyFor({ ...concept, uses_trend_state: undefined }, situation, [copy()])).toBeUndefined();
  });

  it("honours the paired copy's flag (paired by id, as a string)", () => {
    const evergreen = { ...concept, uses_trend_state: false, timely_reason: "" };
    const t = timelyFor(
      evergreen,
      situation,
      [copy({ original_id: "1" as unknown as number, uses_trend_state: true, timely_reason: "Copy is timely." })],
      VALID_UNTIL_MS,
    );
    expect(t).toMatchObject({ reason: "Copy is timely.", expired: true });
    // a copy that only matches by headline or position never makes a concept timely
    expect(
      timelyFor(evergreen, situation, [copy({ original_id: 7, headline: concept.headline, uses_trend_state: true })]),
    ).toBeUndefined();
  });
});

function renderDetail(c: VisualConcept, trendSituation?: unknown, now = BEFORE) {
  return render(
    <ProofDetail
      proofs={buildProofs([c], [], null, undefined, undefined, trendSituation, now)}
      open
      index={0}
      onIndexChange={() => {}}
      onClose={() => {}}
      imageUrlFor={() => null}
      returnFocusTo={() => null}
    />,
  );
}

describe("proof detail timely row", () => {
  it("shows the timely reason and validity", () => {
    renderDetail(concept, situation);
    expect(screen.getByText("Timely")).toBeInTheDocument();
    expect(
      screen.getByText("Builds anticipation for tomorrow's game. · valid until Oct 12, 23:59"),
    ).toBeInTheDocument();
  });

  it("shows when the moment expired", () => {
    renderDetail(concept, situation, VALID_UNTIL_MS);
    expect(
      screen.getByText("Builds anticipation for tomorrow's game. · Expired on Oct 12, 23:59"),
    ).toBeInTheDocument();
  });

  it("shows nothing for an evergreen concept", () => {
    renderDetail({ ...concept, uses_trend_state: false }, situation);
    expect(screen.queryByText("Timely")).toBeNull();
  });
});

function renderGrid(c: VisualConcept, now: number) {
  return render(
    <ProofGrid
      proofs={buildProofs([c], [], null, undefined, undefined, situation, now)}
      sort="pipeline"
      onSortChange={() => {}}
      imageUrlFor={() => null}
      onOpen={() => {}}
      itemRef={() => null}
    />,
  );
}

describe("contact sheet timely chip", () => {
  it("shows a Timely chip with its validity before the boundary", () => {
    vi.useFakeTimers();
    vi.setSystemTime(VALID_UNTIL_MS - 1000);
    renderGrid(concept, Date.now());
    const chip = screen.getByText("Timely");
    expect(chip).toHaveAttribute("title", "valid until Oct 12, 23:59");
    expect(chip).toHaveTextContent("Timely, valid until Oct 12, 23:59");
    expect(chip.className).toContain("text-mark-pass");
    expect(screen.queryByText("Expired")).toBeNull();
    expect(screen.getByRole("button", { name: "Open creative 1: Game-day ready · timely" })).toBeInTheDocument();
  });

  it("turns Expired at the boundary", () => {
    vi.useFakeTimers();
    vi.setSystemTime(VALID_UNTIL_MS);
    renderGrid(concept, Date.now());
    const chip = screen.getByText("Expired");
    expect(chip).toHaveAttribute("title", "expired on Oct 12, 23:59");
    expect(chip.className).toContain("text-mark-pending");
    expect(screen.queryByText("Timely")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Open creative 1: Game-day ready · timely, expired" }),
    ).toBeInTheDocument();
  });

  it("shows no chip for an evergreen concept", () => {
    renderGrid({ ...concept, uses_trend_state: false }, VALID_UNTIL_MS);
    expect(screen.queryByText("Timely")).toBeNull();
    expect(screen.queryByText("Expired")).toBeNull();
    expect(screen.getByRole("button", { name: "Open creative 1: Game-day ready" })).toBeInTheDocument();
  });

  it("words the aria suffix", () => {
    expect(timelyAria(undefined)).toBe("");
    expect(timelyAria({ reason: "", validUntil: null, validUntilIso: "x", expired: false })).toBe(" · timely");
    expect(timelyAria({ reason: "", validUntil: null, validUntilIso: "x", expired: true })).toBe(
      " · timely, expired",
    );
  });
});

describe("useWallClock", () => {
  it("starts at 0, reads the clock in an effect and ticks every interval", () => {
    vi.useFakeTimers();
    vi.setSystemTime(BEFORE);
    const { result } = renderHook(() => useWallClock(60_000));
    expect(result.current).toBe(0);
    act(() => {
      vi.advanceTimersByTime(0);
    });
    expect(result.current).toBe(BEFORE);
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(result.current).toBe(BEFORE + 60_000);
    expect(result.current).toBeGreaterThanOrEqual(VALID_UNTIL_MS); // crossed the boundary
  });
});
