// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { buildProofs, timelyFor, type VisualConcept } from "@/lib/eval-matching";
import { ProofDetail } from "@/app/results/[sessionId]/proof-detail";

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

describe("timelyFor", () => {
  it("is the reason and validity for a timely concept", () => {
    expect(timelyFor(concept, situation)).toEqual({
      reason: "Builds anticipation for tomorrow's game.",
      validUntil: "2026-10-12 23:59",
    });
  });

  it("omits the validity when the run has none", () => {
    expect(timelyFor(concept, undefined)).toEqual({
      reason: "Builds anticipation for tomorrow's game.",
      validUntil: null,
    });
  });

  it("is absent for evergreen concepts", () => {
    expect(timelyFor({ ...concept, uses_trend_state: false }, situation)).toBeUndefined();
    expect(timelyFor({ ...concept, uses_trend_state: undefined }, situation)).toBeUndefined();
  });
});

function renderDetail(c: VisualConcept, trendSituation?: unknown) {
  return render(
    <ProofDetail
      proofs={buildProofs([c], [], null, undefined, undefined, trendSituation)}
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
      screen.getByText("Builds anticipation for tomorrow's game. · valid until 2026-10-12 23:59"),
    ).toBeInTheDocument();
  });

  it("shows nothing for an evergreen concept", () => {
    renderDetail({ ...concept, uses_trend_state: false }, situation);
    expect(screen.queryByText("Timely")).toBeNull();
  });
});
