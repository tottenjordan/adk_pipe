// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LearningSummary } from "@/components/learning-summary";
import { describeRatingLearning, learningSummaryText } from "@/lib/rating-learning";
import { STRICTNESS_LABELS } from "@/lib/rating-reasons";

const applied = (over: Record<string, unknown> = {}) => ({
  learn_from_ratings: true,
  rating_signals_applied: {
    ratings: 23,
    applied: true,
    effects: ["checks", "guidance", "styles"],
    signals: "Your team's ratings for PRS (23): ...",
    strictness: ["product_not_visible", "text_problem"],
    styles_excluded: ["Isometric miniature world"],
    styles_preferred: ["Candid 35mm film photo", "Comic panel"],
    ...over,
  },
});

describe("describeRatingLearning", () => {
  it("is off unless the run opted in", () => {
    expect(describeRatingLearning({})).toEqual({ status: "off" });
    expect(describeRatingLearning({ learn_from_ratings: "true" })).toEqual({ status: "off" });
  });

  it("is pending before the learning step recorded anything", () => {
    expect(describeRatingLearning({ learn_from_ratings: true })).toEqual({ status: "pending" });
  });

  it("parses an applied record, keeping only known strictness flags", () => {
    expect(
      describeRatingLearning(applied({ strictness: ["weak_cta", "bogus", 3] })),
    ).toEqual({
      status: "applied",
      ratings: 23,
      guidance: true,
      preferred: ["Candid 35mm film photo", "Comic panel"],
      avoided: ["Isometric miniature world"],
      strictness: ["weak_cta"],
    });
  });

  it("maps the not-applied reasons", () => {
    const state = (rec: unknown) => ({ learn_from_ratings: true, rating_signals_applied: rec });
    expect(describeRatingLearning(state({ ratings: 3, applied: false, reason: "not_enough_ratings" }))).toEqual({
      status: "not_enough",
      ratings: 3,
      min: null,
    });
    expect(
      describeRatingLearning(state({ ratings: 3, applied: false, reason: "not_enough_ratings", min: 8 })),
    ).toEqual({ status: "not_enough", ratings: 3, min: 8 });
    expect(describeRatingLearning(state({ applied: false, reason: "unavailable" }))).toEqual({
      status: "unavailable",
    });
    expect(describeRatingLearning(state({ ratings: 9, applied: false, reason: "no_effects" }))).toEqual({
      status: "no_effects",
      ratings: 9,
    });
    expect(describeRatingLearning(state("garbage"))).toEqual({ status: "pending" });
  });
});

describe("learningSummaryText", () => {
  it("names what was learned with human labels", () => {
    expect(learningSummaryText(applied())).toBe(
      "Learned from 23 team ratings: preferred Candid 35mm film photo, Comic panel; " +
        "avoided Isometric miniature world; stricter checks: " +
        `${STRICTNESS_LABELS.product_not_visible}, ${STRICTNESS_LABELS.text_problem}.`,
    );
  });

  it("falls back to guidance when nothing else changed", () => {
    expect(
      learningSummaryText(applied({ ratings: 1, strictness: [], styles_excluded: [], styles_preferred: [] })),
    ).toBe("Learned from 1 team rating: guidance for the brief, copy and art direction.");
  });

  it("shows progress towards the minimum", () => {
    expect(
      learningSummaryText({
        learn_from_ratings: true,
        rating_signals_applied: { ratings: 3, applied: false, reason: "not_enough_ratings", min: 8 },
      }),
    ).toBe("Not enough ratings yet (3 of 8).");
  });

  it("says when there are not enough ratings yet (older sessions without a min)", () => {
    expect(
      learningSummaryText({
        learn_from_ratings: true,
        rating_signals_applied: { ratings: 3, applied: false, reason: "not_enough_ratings" },
      }),
    ).toBe("Not enough ratings yet (3).");
  });

  it("is null when off, pending or with nothing applied", () => {
    expect(learningSummaryText({})).toBeNull();
    expect(learningSummaryText({ learn_from_ratings: true })).toBeNull();
    expect(
      learningSummaryText({
        learn_from_ratings: true,
        rating_signals_applied: { ratings: 9, applied: false, reason: "no_effects" },
      }),
    ).toBeNull();
    // Recorded but the run did not opt in (e.g. a seeded state): still off.
    expect(learningSummaryText({ rating_signals_applied: applied().rating_signals_applied })).toBeNull();
  });
});

describe("<LearningSummary>", () => {
  it("renders the applied line", () => {
    render(<LearningSummary state={applied()} />);
    expect(screen.getByText("Learning from ratings")).toBeTruthy();
    expect(screen.getByText(/^Learned from 23 team ratings: preferred Candid 35mm film photo/)).toBeTruthy();
  });

  it("renders the not-enough line", () => {
    render(
      <LearningSummary
        state={{
          learn_from_ratings: true,
          rating_signals_applied: { ratings: 3, applied: false, reason: "not_enough_ratings", min: 8 },
        }}
      />,
    );
    expect(screen.getByText("Not enough ratings yet (3 of 8).")).toBeTruthy();
  });

  it("renders nothing when learning is off", () => {
    const { container } = render(<LearningSummary state={{ brand: "PRS" }} />);
    expect(container.innerHTML).toBe("");
  });
});

describe("STRICTNESS_LABELS", () => {
  it("names what off_brief tightens", () => {
    expect(STRICTNESS_LABELS.off_brief).toBe("reason to believe and trend bridge must pass");
  });
});
